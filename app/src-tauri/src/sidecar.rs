//! Sidecar bridge: spawn `python -m lucid --sidecar` and pipe NDJSON.
//!
//! - Frontend → Rust: invoke commands `sidecar_start_task / sidecar_cancel /
//!   sidecar_get_status / sidecar_ping`.
//! - Rust → Frontend: each line of sidecar stdout is forwarded as a Tauri
//!   event named `lucid://event`.
//! - Crash recovery: if the child exits unexpectedly we emit
//!   `lucid://sidecar` with `{kind:"exit", code}` and respawn after 1s.

use std::collections::HashMap;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::Arc;

use once_cell::sync::OnceCell;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{oneshot, Mutex};

pub const EVENT_LUCID: &str = "lucid://event";
pub const EVENT_SIDECAR: &str = "lucid://sidecar";

/// Tracks a running sidecar process & its inflight RPC requests.
pub struct Sidecar {
    stdin: Mutex<Option<ChildStdin>>,
    next_id: AtomicU64,
    pending: Mutex<HashMap<u64, oneshot::Sender<Result<Value, String>>>>,
    /// PID of the currently spawned child (0 == none). Stored separately from
    /// the `Child` handle (which lives inside the spawn future) so the
    /// shutdown path can force-kill the whole process tree via `taskkill`
    /// even if the supervise task is blocked.
    child_pid: AtomicU32,
    /// Set when the app is on its way out — `supervise()` checks this between
    /// respawn attempts so we don't immediately relaunch the sidecar after
    /// killing it during shutdown.
    shutting_down: AtomicBool,
}

impl Sidecar {
    fn new() -> Self {
        Self {
            stdin: Mutex::new(None),
            next_id: AtomicU64::new(1),
            pending: Mutex::new(HashMap::new()),
            child_pid: AtomicU32::new(0),
            shutting_down: AtomicBool::new(false),
        }
    }

    /// Send a JSON-RPC request to the sidecar and wait for its response.
    ///
    /// Uses the default 120s timeout — appropriate for RPCs that may legitimately
    /// take a long time (network handshakes, long file reads, model setup).
    /// For RPCs that should return effectively instantly (cancel, thread switch,
    /// status queries) prefer ``request_with_timeout`` so a stalled IPC pipe
    /// surfaces as an error in seconds rather than two minutes.
    pub async fn request(&self, method: &str, params: Value) -> Result<Value, String> {
        self.request_with_timeout(method, params, std::time::Duration::from_secs(120)).await
    }

    /// Same as ``request`` but with a caller-chosen timeout.
    pub async fn request_with_timeout(
        &self,
        method: &str,
        params: Value,
        timeout: std::time::Duration,
    ) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);
        let payload = json!({"id": id, "method": method, "params": params});
        let line = format!("{}\n", payload);
        {
            let mut guard = self.stdin.lock().await;
            let stdin = guard.as_mut().ok_or_else(|| "sidecar not started".to_string())?;
            stdin
                .write_all(line.as_bytes())
                .await
                .map_err(|e| format!("write stdin: {e}"))?;
            stdin.flush().await.ok();
        }
        match tokio::time::timeout(timeout, rx).await {
            Ok(Ok(res)) => res,
            Ok(Err(_)) => Err("sidecar response channel closed".into()),
            Err(_) => {
                self.pending.lock().await.remove(&id);
                Err(format!(
                    "sidecar request timed out after {}s (method={method}); the IPC pipe may be stuck — try restarting the app",
                    timeout.as_secs()
                ))
            }
        }
    }
}

/// Default short timeout for RPCs that touch in-memory state or read tiny files
/// and should return effectively instantly. Long enough to absorb a brief
/// hiccup, short enough that users get feedback in seconds rather than minutes
/// when the stdout IPC pipe is wedged.
const FAST_RPC_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(8);

static INSTANCE: OnceCell<Arc<Sidecar>> = OnceCell::new();

pub fn instance() -> Arc<Sidecar> {
    INSTANCE
        .get_or_init(|| Arc::new(Sidecar::new()))
        .clone()
}

/// Resolve (and lazily seed) the user-writable config path. Priority:
///   1. `LUCID_CONFIG` env (explicit override).
///   2. `~/.lucid/config.toml` (per-user, e.g. `C:\Users\<name>\.lucid\config.toml`).
///      If missing, copy from the bundled default at `<resource_dir>/config.toml`.
///   3. Bundled default at `<resource_dir>/config.toml` (read-only fallback).
///   4. `<cwd>/config.toml` (dev mode).
pub fn ensure_user_config(app: &AppHandle) -> std::path::PathBuf {
    if let Ok(p) = std::env::var("LUCID_CONFIG") {
        return std::path::PathBuf::from(p);
    }
    {
        let dir = lucid_home();
        let target = dir.join("config.toml");
        if !target.exists() {
            if let Err(e) = std::fs::create_dir_all(&dir) {
                log::warn!("create_dir_all {}: {e}", dir.display());
            }
            // Try bundled defaults via Tauri resource resolver, then fall back to
            // resource_dir/config.toml directly.
            let bundled = app
                .path()
                .resolve("config.toml", tauri::path::BaseDirectory::Resource)
                .ok()
                .or_else(|| app.path().resource_dir().ok().map(|d| d.join("config.toml")))
                .map(strip_verbatim);
            if let Some(src) = bundled {
                if src.exists() {
                    if let Err(e) = std::fs::copy(&src, &target) {
                        log::warn!("seed config from {} → {}: {e}", src.display(), target.display());
                    } else {
                        log::info!("seeded user config at {}", target.display());
                    }
                }
            }
        }
        return target;
    }
}

/// Strip Windows verbatim/UNC prefix `\\?\` (and `\\?\UNC\`) that Tauri's
/// `resource_dir()` returns. PyInstaller's bootstrap and many child
/// processes choke on verbatim paths when launching/loading `_internal/*.dll`.
fn strip_verbatim(p: std::path::PathBuf) -> std::path::PathBuf {
    #[cfg(windows)]
    {
        let s = p.to_string_lossy();
        if let Some(rest) = s.strip_prefix(r"\\?\UNC\") {
            return std::path::PathBuf::from(format!(r"\\{}", rest));
        }
        if let Some(rest) = s.strip_prefix(r"\\?\") {
            return std::path::PathBuf::from(rest.to_string());
        }
    }
    p
}

/// User-writable data root: `~/.lucid` (e.g. `C:\Users\<name>\.lucid` on Windows).
/// All per-user state — `config.toml`, `inbox/`, `logs/`, `templates.json`,
/// `schedules.json`, `memory.md`, `tools.md`, `copilot.json`, etc. — lives here.
fn lucid_home() -> std::path::PathBuf {
    let base = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::PathBuf::from("."));
    base.join(".lucid")
}

/// How to invoke the python sidecar. Resolution priority:
///   1. `LUCID_SIDECAR_EXE`  → spawn that binary directly with `--sidecar`.
///   2. Bundled `resources/lucid/lucid.exe` (PyInstaller output for packaged builds).
///   3. `LUCID_PYTHON` env (path to a python.exe) + `-m lucid --sidecar` (dev mode).
///   4. `python -m lucid --sidecar` (system python on PATH).
fn build_command(app: &AppHandle) -> (Command, String) {
    let cfg_path = ensure_user_config(app);
    let cfg_str = cfg_path.display().to_string();
    // 1) explicit binary override
    if let Ok(exe) = std::env::var("LUCID_SIDECAR_EXE") {
        let mut cmd = Command::new(&exe);
        cmd.arg("--sidecar").arg("--config").arg(&cfg_str);
        configure_common(&mut cmd, &cfg_str);
        return (cmd, exe);
    }
    // 2) bundled exe shipped via tauri.conf.json `bundle.resources`
    if let Ok(res_dir) = app.path().resource_dir() {
        let res_dir = strip_verbatim(res_dir);
        // PyInstaller onefile output: a single self-extracting exe at the
        // resource_dir root (see packaging/lucid.spec).
        let candidate = res_dir.join("lucid.exe");
        if candidate.exists() {
            let mut cmd = Command::new(&candidate);
            cmd.arg("--sidecar").arg("--config").arg(&cfg_str);
            configure_common(&mut cmd, &cfg_str);
            return (cmd, candidate.display().to_string());
        }
    }
    // 3) dev mode: explicit python interpreter via env
    let py = std::env::var("LUCID_PYTHON").unwrap_or_else(|_| "python".into());
    let mut cmd = Command::new(&py);
    cmd.arg("-m").arg("lucid").arg("--sidecar").arg("--config").arg(&cfg_str);
    configure_common(&mut cmd, &cfg_str);
    (cmd, format!("{py} -m lucid"))
}

fn configure_common(cmd: &mut Command, cfg_path: &str) {
    if let Ok(cwd) = std::env::var("LUCID_CWD") {
        cmd.current_dir(cwd);
    }
    // Make the config path discoverable even if --config is dropped or wrapped.
    cmd.env("LUCID_CONFIG", cfg_path);
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // Hide the console window on Windows when launched from Tauri.
    // tokio::process::Command has its own creation_flags on Windows.
    #[cfg(windows)]
    {
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
}

/// Spawn the sidecar once; auto-respawn on unexpected exit.
pub fn supervise(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        loop {
            if instance().shutting_down.load(Ordering::SeqCst) {
                log::info!("supervise: shutting_down flag set; exiting loop");
                break;
            }
            match spawn_once(&app).await {
                Ok(code) => {
                    let _ = app.emit(
                        EVENT_SIDECAR,
                        json!({"kind": "exit", "code": code}),
                    );
                    log::warn!("sidecar exited code={code:?}");
                    if instance().shutting_down.load(Ordering::SeqCst) {
                        log::info!("supervise: clean exit during shutdown; not respawning");
                        break;
                    }
                    log::warn!("respawning sidecar in 1s");
                }
                Err(e) => {
                    let _ = app.emit(
                        EVENT_SIDECAR,
                        json!({"kind": "spawn_error", "message": e.to_string()}),
                    );
                    log::error!("sidecar spawn failed: {e}");
                    if instance().shutting_down.load(Ordering::SeqCst) {
                        break;
                    }
                }
            }
            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        }
    });
}

async fn spawn_once(app: &AppHandle) -> Result<Option<i32>, String> {
    let (mut cmd, exe_desc) = build_command(app);
    let _ = app.emit(
        EVENT_SIDECAR,
        json!({"kind": "spawn", "exe": exe_desc}),
    );
    log::info!("spawning sidecar: {exe_desc}");
    let mut child: Child = cmd.spawn().map_err(|e| format!("spawn {exe_desc}: {e}"))?;
    let pid = child.id().unwrap_or(0);
    instance().child_pid.store(pid, Ordering::SeqCst);
    log::info!("sidecar pid = {pid}");
    let stdin = child.stdin.take().ok_or("no stdin")?;
    let stdout = child.stdout.take().ok_or("no stdout")?;
    let stderr = child.stderr.take().ok_or("no stderr")?;

    let sidecar = instance();
    *sidecar.stdin.lock().await = Some(stdin);

    // stdout reader
    //
    // ROBUSTNESS: read raw bytes via `read_until(b'\n')` and decode lossily,
    // never letting a single bad byte kill the reader task. The previous
    // implementation used `BufReader::lines().next_line()` and matched only
    // `Ok(Some(line))` in the while-let — but Tokio's `read_line` returns
    // `Err(InvalidData)` the moment it sees a non-UTF-8 byte sequence, which
    // does NOT match the pattern and silently terminates the entire stdout
    // reader task. Symptom (observed in thread-20260518-223343 and
    // thread-20260518-230314): UI froze at step 9 (the launch_app step
    // emitted an event containing the Chinese cmd-window title that got
    // re-encoded as CP936 mojibake when Python's stdout text codec mis-
    // configured; or any other stray non-UTF-8 byte from a C extension /
    // PyInstaller bootloader / unredirected print). Sidecar kept running
    // and writing to events.jsonl past step 22+, but the frontend got
    // ZERO events from step 10 onward. Reading bytes + `from_utf8_lossy`
    // guarantees a single bad byte just produces a U+FFFD that the JSON
    // parser will reject for that one line — and we `continue` instead of
    // breaking, so subsequent valid lines still flow.
    let app_out = app.clone();
    let sidecar_out = sidecar.clone();
    tauri::async_runtime::spawn(async move {
        let mut reader = BufReader::new(stdout);
        let mut buf: Vec<u8> = Vec::with_capacity(8192);
        loop {
            buf.clear();
            match reader.read_until(b'\n', &mut buf).await {
                Ok(0) => break, // EOF
                Ok(_) => {}
                Err(e) => {
                    log::warn!("sidecar stdout read error: {e}");
                    // Transient I/O error — try the next read rather than
                    // killing the task.
                    continue;
                }
            }
            // Trim trailing \n and optional \r (Windows line endings).
            while matches!(buf.last(), Some(b'\n') | Some(b'\r')) {
                buf.pop();
            }
            if buf.is_empty() {
                continue;
            }
            let line = String::from_utf8_lossy(&buf);
            let v: Value = match serde_json::from_str(&line) {
                Ok(v) => v,
                Err(_) => continue,
            };
            // RPC response: { id, result } or { id, error }
            if let Some(id) = v.get("id").and_then(|x| x.as_u64()) {
                let mut pending = sidecar_out.pending.lock().await;
                if let Some(tx) = pending.remove(&id) {
                    let res = if let Some(err) = v.get("error") {
                        Err(err.as_str().unwrap_or("rpc error").to_string())
                    } else {
                        Ok(v.get("result").cloned().unwrap_or(Value::Null))
                    };
                    let _ = tx.send(res);
                }
                continue;
            }
            // Otherwise it's an event; forward verbatim.
            let _ = app_out.emit(EVENT_LUCID, v);
        }
    });

    // stderr → forwarded as event with kind:"log" so the UI can show python tracebacks.
    // Same robustness as stdout reader above — never let a stray non-UTF-8
    // byte kill the stderr task (would silently lose Python tracebacks).
    let app_err = app.clone();
    tauri::async_runtime::spawn(async move {
        let mut reader = BufReader::new(stderr);
        let mut buf: Vec<u8> = Vec::with_capacity(4096);
        loop {
            buf.clear();
            match reader.read_until(b'\n', &mut buf).await {
                Ok(0) => break,
                Ok(_) => {}
                Err(_) => continue,
            }
            while matches!(buf.last(), Some(b'\n') | Some(b'\r')) {
                buf.pop();
            }
            if buf.is_empty() {
                continue;
            }
            let line = String::from_utf8_lossy(&buf).into_owned();
            let _ = app_err.emit(
                EVENT_SIDECAR,
                json!({"kind": "stderr", "line": line}),
            );
        }
    });

    let status = child.wait().await.map_err(|e| format!("wait: {e}"))?;
    // Drop stdin so caller knows we're disconnected; clear pending requests.
    *sidecar.stdin.lock().await = None;
    sidecar.child_pid.store(0, Ordering::SeqCst);
    let mut pending = sidecar.pending.lock().await;
    for (_, tx) in pending.drain() {
        let _ = tx.send(Err("sidecar terminated".into()));
    }
    Ok(status.code())
}

/// Best-effort full shutdown of the sidecar, called from the Tauri exit path
/// (tray "退出", `RunEvent::Exit`, etc.). Synchronous + blocking so the
/// Tauri process doesn't terminate before this returns and orphan the child.
///
/// Steps:
///   1. Mark `shutting_down` so `supervise()` won't respawn after the child
///      exits.
///   2. Send a `shutdown` JSON-RPC (graceful — gives Python a chance to fire
///      atexit hooks, which restore the system cursor).
///   3. Wait up to ~2.5s for the child PID to disappear.
///   4. If still alive, `taskkill /F /T /PID <pid>` to kill the whole tree.
///   5. As a belt-and-suspenders for the cursor (in case Python was killed
///      before its atexit fired), call `SystemParametersInfoW(SPI_SETCURSORS)`
///      from Rust to reload the user's configured cursor scheme.
pub fn shutdown_blocking() {
    let sidecar = instance();
    sidecar.shutting_down.store(true, Ordering::SeqCst);
    let pid = sidecar.child_pid.load(Ordering::SeqCst);
    log::info!("shutdown_blocking: sidecar pid={pid}");

    // Graceful shutdown RPC on a tiny temporary tokio runtime — we may be
    // called outside any existing async context (e.g. from RunEvent::Exit on
    // the Tauri main thread).
    if pid != 0 {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build();
        if let Ok(rt) = rt {
            let _ = rt.block_on(async {
                tokio::time::timeout(
                    std::time::Duration::from_millis(1500),
                    sidecar.request("shutdown", json!({})),
                )
                .await
            });
        }
    }

    // Wait briefly for the OS to reap the process.
    if pid != 0 {
        for _ in 0..10 {
            if !pid_is_alive(pid) {
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
    }

    // Force-kill the whole tree if still alive (covers cases where the
    // graceful shutdown deadlocked on an in-progress task / blocking I/O).
    if pid != 0 && pid_is_alive(pid) {
        log::warn!("sidecar pid {pid} still alive after graceful shutdown; force-killing tree");
        #[cfg(windows)]
        {
            let _ = std::process::Command::new("taskkill")
                .args(["/F", "/T", "/PID", &pid.to_string()])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status();
        }
        #[cfg(unix)]
        {
            unsafe {
                libc::kill(pid as i32, libc::SIGKILL);
            }
        }
    }

    // Final belt-and-suspenders: restore system cursors. If the Python child
    // got force-killed, its atexit hook never ran and the crab claw would
    // remain until next logon. SPI_SETCURSORS reloads the registry scheme.
    restore_system_cursors();
}

#[cfg(windows)]
fn pid_is_alive(pid: u32) -> bool {
    use windows::Win32::Foundation::{CloseHandle, STILL_ACTIVE};
    use windows::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        let h = match OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) {
            Ok(h) => h,
            Err(_) => return false,
        };
        if h.is_invalid() {
            return false;
        }
        let mut code: u32 = 0;
        let ok = GetExitCodeProcess(h, &mut code).is_ok();
        let _ = CloseHandle(h);
        ok && code as i32 == STILL_ACTIVE.0
    }
}

#[cfg(not(windows))]
fn pid_is_alive(pid: u32) -> bool {
    // POSIX: kill(pid, 0) returns 0 if process exists.
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

#[cfg(windows)]
fn restore_system_cursors() {
    use windows::Win32::UI::WindowsAndMessaging::{
        SystemParametersInfoW, SPI_SETCURSORS, SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS,
    };
    unsafe {
        let _ = SystemParametersInfoW(
            SPI_SETCURSORS,
            0,
            None,
            SYSTEM_PARAMETERS_INFO_UPDATE_FLAGS(0),
        );
    }
}

#[cfg(not(windows))]
fn restore_system_cursors() {}

// ---------- Tauri commands exposed to the frontend ----------

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FileRef {
    pub name: String,
    pub path: String,
    /// "image" | "file" (advisory; backend infers by extension if missing)
    #[serde(default)]
    pub kind: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StartArgs {
    pub instruction: String,
    /// Multi-modal attachments. Images pasted from the clipboard are persisted to
    /// `~/.lucid/inbox/` first via `save_inbox_image` so every entry
    /// here is an absolute disk path.
    #[serde(default)]
    pub file_refs: Option<Vec<FileRef>>,
}

#[tauri::command]
pub async fn sidecar_start_task(args: StartArgs) -> Result<Value, String> {
    let mut params = json!({"instruction": args.instruction});
    if let Some(refs) = args.file_refs {
        if !refs.is_empty() {
            params["file_refs"] = serde_json::to_value(refs).unwrap_or_else(|_| json!([]));
        }
    }
    instance().request("start_task", params).await
}

#[tauri::command]
pub async fn sidecar_cancel() -> Result<Value, String> {
    instance().request_with_timeout("cancel", json!({}), FAST_RPC_TIMEOUT).await
}

#[tauri::command]
pub async fn sidecar_get_status() -> Result<Value, String> {
    instance().request_with_timeout("get_status", json!({}), FAST_RPC_TIMEOUT).await
}

/// Liveness probe of the sidecar JSON-RPC pipe.
#[tauri::command]
pub async fn sidecar_ping() -> Result<Value, String> {
    instance().request_with_timeout("ping", json!({}), FAST_RPC_TIMEOUT).await
}

// ---- GitHub Copilot OAuth (device-code) bridge ----

#[tauri::command]
pub async fn copilot_status() -> Result<Value, String> {
    instance().request("copilot_status", json!({})).await
}

#[tauri::command]
pub async fn copilot_login_begin() -> Result<Value, String> {
    instance().request("copilot_login_begin", json!({})).await
}

#[tauri::command]
pub async fn copilot_login_poll(device_code: String) -> Result<Value, String> {
    instance()
        .request("copilot_login_poll", json!({"device_code": device_code}))
        .await
}

#[tauri::command]
pub async fn copilot_logout() -> Result<Value, String> {
    instance().request("copilot_logout", json!({})).await
}

// ---- Thread (conversation) management ----

#[tauri::command]
pub async fn thread_new(title: Option<String>) -> Result<Value, String> {
    instance().request_with_timeout("thread_new", json!({"title": title.unwrap_or_default()}), FAST_RPC_TIMEOUT).await
}

#[tauri::command]
pub async fn thread_list() -> Result<Value, String> {
    instance().request_with_timeout("thread_list", json!({}), FAST_RPC_TIMEOUT).await
}

#[tauri::command]
pub async fn thread_read(id: String) -> Result<Value, String> {
    // thread_read can be slower for very long threads (reads + parses events.jsonl,
    // which can be several MB), so keep the default 120s timeout here rather than
    // the fast one.
    instance().request("thread_read", json!({"id": id})).await
}

#[tauri::command]
pub async fn thread_set_active(id: Option<String>) -> Result<Value, String> {
    instance().request_with_timeout("thread_set_active", json!({"id": id}), FAST_RPC_TIMEOUT).await
}

#[tauri::command]
pub async fn thread_delete(id: String) -> Result<Value, String> {
    instance().request_with_timeout("thread_delete", json!({"id": id}), FAST_RPC_TIMEOUT).await
}

// ---- Task queue ----

#[tauri::command]
pub async fn task_queue_list() -> Result<Value, String> {
    instance().request_with_timeout("queue_list", json!({}), FAST_RPC_TIMEOUT).await
}

#[tauri::command]
pub async fn task_queue_remove(thread_id: String) -> Result<Value, String> {
    instance().request_with_timeout("queue_remove", json!({"thread_id": thread_id}), FAST_RPC_TIMEOUT).await
}

#[tauri::command]
pub async fn task_queue_clear() -> Result<Value, String> {
    instance().request_with_timeout("queue_clear", json!({}), FAST_RPC_TIMEOUT).await
}

/// Read an image file inside a thread directory, return as data URL.
#[tauri::command]
pub async fn thread_read_image(app: AppHandle, thread_id: String, file_name: String) -> Result<String, String> {
    // Threads now live under <logs>/threads/<thread_id>/. Older builds had
    // them directly under <logs>/<thread_id>/, so try the new layout first
    // and fall back to the legacy one for any leftover dirs.
    let root = logs_root(&app);
    let mut p = root.join("threads").join(&thread_id).join(&file_name);
    if !p.exists() {
        p = root.join(&thread_id).join(&file_name);
    }
    let bytes = std::fs::read(&p).map_err(|e| e.to_string())?;
    use base64::Engine;
    let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
    let mime = if file_name.ends_with(".jpg") || file_name.ends_with(".jpeg") {
        "image/jpeg"
    } else { "image/png" };
    Ok(format!("data:{};base64,{}", mime, b64))
}

// ---- memory.md ----
#[tauri::command]
pub async fn memory_read() -> Result<Value, String> {
    instance().request("memory_read", json!({})).await
}

#[tauri::command]
pub async fn memory_write(text: String) -> Result<Value, String> {
    instance().request("memory_write", json!({"text": text})).await
}

#[tauri::command]
pub async fn memory_append(text: String, source: Option<String>) -> Result<Value, String> {
    instance().request("memory_append", json!({"text": text, "source": source.unwrap_or_else(|| "user".into())})).await
}

#[tauri::command]
pub async fn memory_clear() -> Result<Value, String> {
    instance().request("memory_clear", json!({})).await
}

// ---- tools.md (操作技巧) ----
#[tauri::command]
pub async fn tools_read() -> Result<Value, String> {
    instance().request("tools_read", json!({})).await
}

#[tauri::command]
pub async fn tools_write(text: String) -> Result<Value, String> {
    instance().request("tools_write", json!({"text": text})).await
}

#[tauri::command]
pub async fn tools_append(text: String, kind: Option<String>, source: Option<String>) -> Result<Value, String> {
    instance().request("tools_append", json!({
        "text": text,
        "kind": kind.unwrap_or_else(|| "tip".into()),
        "source": source.unwrap_or_else(|| "user".into()),
    })).await
}

#[tauri::command]
pub async fn tools_reset() -> Result<Value, String> {
    instance().request("tools_reset", json!({})).await
}

// ---- per-app tips/<app>.md ----
#[tauri::command]
pub async fn app_tips_list() -> Result<Value, String> {
    instance().request("app_tips_list", json!({})).await
}

#[tauri::command]
pub async fn app_tips_read(app: String) -> Result<Value, String> {
    instance().request("app_tips_read", json!({"app": app})).await
}

#[tauri::command]
pub async fn app_tips_write(app: String, text: String) -> Result<Value, String> {
    instance().request("app_tips_write", json!({"app": app, "text": text})).await
}

#[tauri::command]
pub async fn app_tips_append(app: String, text: String, kind: Option<String>, source: Option<String>) -> Result<Value, String> {
    instance().request("app_tips_append", json!({
        "app": app,
        "text": text,
        "kind": kind.unwrap_or_else(|| "tip".into()),
        "source": source.unwrap_or_else(|| "user".into()),
    })).await
}

#[tauri::command]
pub async fn app_tips_reset(app: String) -> Result<Value, String> {
    instance().request("app_tips_reset", json!({"app": app})).await
}

#[tauri::command]
pub async fn app_tips_delete(app: String) -> Result<Value, String> {
    instance().request("app_tips_delete", json!({"app": app})).await
}

// ---- 任务模板 ----
#[tauri::command]
pub async fn template_list() -> Result<Value, String> {
    instance().request("template_list", json!({})).await
}

#[tauri::command]
pub async fn template_add(name: String, instruction: String) -> Result<Value, String> {
    instance().request("template_add", json!({
        "name": name, "instruction": instruction,
    })).await
}

#[tauri::command]
pub async fn template_update(id: String, name: Option<String>, instruction: Option<String>) -> Result<Value, String> {
    instance().request("template_update", json!({
        "id": id, "name": name, "instruction": instruction,
    })).await
}

#[tauri::command]
pub async fn template_delete(id: String) -> Result<Value, String> {
    instance().request("template_delete", json!({"id": id})).await
}

// ---- Skills (Anthropic Agent Skills format; see Docs/skills.md) ----
#[tauri::command]
pub async fn skill_list() -> Result<Value, String> {
    instance().request("skill_list", json!({})).await
}

#[tauri::command]
pub async fn skill_read(id: String) -> Result<Value, String> {
    instance().request("skill_read", json!({"id": id})).await
}

#[tauri::command]
pub async fn skill_add(name: String, description: String, body: String, version: Option<String>, license: Option<String>) -> Result<Value, String> {
    instance().request("skill_add", json!({
        "name": name,
        "description": description,
        "body": body,
        "version": version,
        "license": license,
    })).await
}

#[tauri::command]
pub async fn skill_update(id: String, name: Option<String>, description: Option<String>, body: Option<String>, version: Option<String>, license: Option<String>) -> Result<Value, String> {
    instance().request("skill_update", json!({
        "id": id,
        "name": name,
        "description": description,
        "body": body,
        "version": version,
        "license": license,
    })).await
}

#[tauri::command]
pub async fn skill_delete(id: String) -> Result<Value, String> {
    instance().request("skill_delete", json!({"id": id})).await
}

#[tauri::command]
pub async fn skill_install_url(url: String) -> Result<Value, String> {
    instance().request("skill_install_url", json!({"url": url})).await
}

#[tauri::command]
pub async fn skill_set_enabled(id: String, enabled: bool) -> Result<Value, String> {
    instance().request("skill_set_enabled", json!({"id": id, "enabled": enabled})).await
}

#[tauri::command]
pub async fn skill_repo_list() -> Result<Value, String> {
    instance().request("skill_repo_list", json!({})).await
}

#[tauri::command]
pub async fn skill_repo_add(url: String, name: Option<String>, description: Option<String>) -> Result<Value, String> {
    instance().request("skill_repo_add", json!({
        "url": url, "name": name, "description": description,
    })).await
}

#[tauri::command]
pub async fn skill_repo_delete(id: String) -> Result<Value, String> {
    instance().request("skill_repo_delete", json!({"id": id})).await
}

#[tauri::command]
pub async fn skill_repo_set_enabled(id: String, enabled: bool) -> Result<Value, String> {
    instance().request("skill_repo_set_enabled", json!({"id": id, "enabled": enabled})).await
}

#[tauri::command]
pub async fn skill_repo_refresh(force: Option<bool>) -> Result<Value, String> {
    instance().request("skill_repo_refresh", json!({"force": force.unwrap_or(false)})).await
}

#[tauri::command]
pub async fn skill_repo_search(query: String, limit: Option<u32>) -> Result<Value, String> {
    instance().request("skill_repo_search", json!({"query": query, "limit": limit})).await
}

#[tauri::command]
pub async fn skill_repo_install(repo_id: String, path: String) -> Result<Value, String> {
    instance().request("skill_repo_install", json!({"repo_id": repo_id, "path": path})).await
}

// ---- 定时任务 ----
#[tauri::command]
pub async fn schedule_list() -> Result<Value, String> {
    instance().request("schedule_list", json!({})).await
}

#[tauri::command]
pub async fn schedule_add(name: String, instruction: String, spec: Value, enabled: Option<bool>, constraints: Option<Value>, auto_chat_apps: Option<Vec<String>>, auto_chat_extra: Option<String>, taskbar_allow_visual: Option<bool>, taskbar_allow_uia: Option<bool>) -> Result<Value, String> {
    instance().request("schedule_add", json!({
        "name": name, "instruction": instruction, "spec": spec,
        "enabled": enabled.unwrap_or(true),
        "constraints": constraints,
        "auto_chat_apps": auto_chat_apps,
        "auto_chat_extra": auto_chat_extra,
        "taskbar_allow_visual": taskbar_allow_visual,
        "taskbar_allow_uia": taskbar_allow_uia,
    })).await
}

#[tauri::command]
pub async fn schedule_update(id: String, name: Option<String>, instruction: Option<String>, spec: Option<Value>, enabled: Option<bool>, constraints: Option<Value>, auto_chat_apps: Option<Vec<String>>, auto_chat_extra: Option<String>, taskbar_allow_visual: Option<bool>, taskbar_allow_uia: Option<bool>) -> Result<Value, String> {
    instance().request("schedule_update", json!({
        "id": id, "name": name, "instruction": instruction, "spec": spec,
        "enabled": enabled,
        "constraints": constraints,
        "auto_chat_apps": auto_chat_apps,
        "auto_chat_extra": auto_chat_extra,
        "taskbar_allow_visual": taskbar_allow_visual,
        "taskbar_allow_uia": taskbar_allow_uia,
    })).await
}

#[tauri::command]
pub async fn schedule_delete(id: String) -> Result<Value, String> {
    instance().request("schedule_delete", json!({"id": id})).await
}

#[tauri::command]
pub async fn schedule_run_now(id: String) -> Result<Value, String> {
    instance().request("schedule_run_now", json!({"id": id})).await
}

// ---- 打盹学习 (doze) ----
#[tauri::command]
pub async fn doze_status() -> Result<Value, String> {
    instance().request("doze_status", json!({})).await
}

#[tauri::command]
pub async fn doze_run_now() -> Result<Value, String> {
    instance().request("doze_run_now", json!({})).await
}

#[tauri::command]
pub async fn doze_clear_processed() -> Result<Value, String> {
    instance().request("doze_clear_processed", json!({})).await
}

#[tauri::command]
pub async fn doze_outputs(limit: Option<u32>) -> Result<Value, String> {
    instance()
        .request("doze_outputs", json!({ "limit": limit.unwrap_or(200) }))
        .await
}

#[tauri::command]
pub async fn doze_delete_output(id: String) -> Result<Value, String> {
    instance()
        .request("doze_delete_output", json!({ "id": id }))
        .await
}

/// List installed apps (scanned launcher icons) with name + base64 PNG icon,
/// in atlas.txt order. Used by the visual_notify auto-reply whitelist UI.
/// When `rescan` is true, the sidecar runs a fresh Start-Menu scan first so
/// apps the user just installed/uninstalled appear immediately.
#[tauri::command]
pub async fn installed_apps_list(rescan: Option<bool>) -> Result<Value, String> {
    instance().request("installed_apps_list", json!({
        "rescan": rescan.unwrap_or(false),
    })).await
}

/// Tell the sidecar to re-read the user-config so settings changes take effect
/// without restarting the whole process. Will refuse if a task is in-flight.
#[tauri::command]
pub async fn reload_config() -> Result<Value, String> {
    instance().request("reload_config", json!({})).await
}

/// Read base_url / model / api_key from config.toml so the
/// settings UI can prefill. Returns null fields if config can't be read.
#[tauri::command]
pub async fn read_settings() -> Result<Value, String> {
    let path = settings_path();
    let raw = std::fs::read_to_string(&path).unwrap_or_default();
    let mut provider = String::new();
    let mut emergency_hotkey = String::new();
    let mut temperature: Option<f64> = None;
    let mut top_p: Option<f64> = None;
    let mut proxy_base_url = String::new();
    let mut proxy_model = String::new();
    let mut proxy_api_key = String::new();
    let mut anthropic_api_key = String::new();
    let mut anthropic_model = String::new();
    let mut anthropic_base_url = String::new();
    let mut copilot_model = String::new();
    // ---- voice section ----
    let mut v_enabled: Option<bool> = None;
    let mut v_engine = String::new();
    let mut v_model_size = String::new();
    let mut v_language = String::new();
    let mut v_compute_type = String::new();
    let mut v_device = String::new();
    let mut v_hotkey = String::new();
    let mut v_hold_threshold_ms: Option<i64> = None;
    let mut v_stop_mode = String::new();
    let mut v_start_feedback = String::new();
    let mut v_focus_aware: Option<bool> = None;
    let mut v_mode = String::new();
    let mut v_auto_send: Option<bool> = None;
    let mut v_max_seconds: Option<i64> = None;
    let mut v_overlay_screen = String::new();
    let mut v_overlay_y_offset_px: Option<i64> = None;
    let mut v_keep_audio: Option<bool> = None;
    let mut v_hf_endpoint = String::new();
    let mut section = String::new();
    for line in raw.lines() {
        let l = line.trim();
        if l.starts_with('[') && l.ends_with(']') {
            section = l.to_string();
            continue;
        }
        match section.as_str() {
            "[llm]" => {
                if let Some(v) = parse_kv(l, "provider")   { provider   = v; }
                if let Some(v) = parse_kv(l, "temperature") { temperature = v.parse::<f64>().ok(); }
                if let Some(v) = parse_kv(l, "top_p")       { top_p       = v.parse::<f64>().ok(); }
            }
            "[llm.proxy]" => {
                if let Some(v) = parse_kv(l, "base_url") { proxy_base_url = v; }
                if let Some(v) = parse_kv(l, "model")    { proxy_model    = v; }
                if let Some(v) = parse_kv(l, "api_key")  { proxy_api_key  = v; }
            }
            "[llm.anthropic]" => {
                if let Some(v) = parse_kv(l, "api_key")  { anthropic_api_key  = v; }
                if let Some(v) = parse_kv(l, "model")    { anthropic_model    = v; }
                if let Some(v) = parse_kv(l, "base_url") { anthropic_base_url = v; }
            }
            "[llm.copilot]" => {
                if let Some(v) = parse_kv(l, "model") { copilot_model = v; }
            }
            "[safety]" => {
                if let Some(v) = parse_kv(l, "emergency_hotkey") { emergency_hotkey = v; }
            }
            "[voice]" => {
                if let Some(v) = parse_kv(l, "enabled")             { v_enabled = parse_bool(&v); }
                if let Some(v) = parse_kv(l, "engine")              { v_engine = v; }
                if let Some(v) = parse_kv(l, "model_size")          { v_model_size = v; }
                if let Some(v) = parse_kv(l, "language")            { v_language = v; }
                if let Some(v) = parse_kv(l, "compute_type")        { v_compute_type = v; }
                if let Some(v) = parse_kv(l, "device")              { v_device = v; }
                if let Some(v) = parse_kv(l, "hotkey")              { v_hotkey = v; }
                if let Some(v) = parse_kv(l, "hold_threshold_ms")   { v_hold_threshold_ms = v.parse::<i64>().ok(); }
                if let Some(v) = parse_kv(l, "stop_mode")           { v_stop_mode = v; }
                if let Some(v) = parse_kv(l, "start_feedback")      { v_start_feedback = v; }
                if let Some(v) = parse_kv(l, "focus_aware")         { v_focus_aware = parse_bool(&v); }
                if let Some(v) = parse_kv(l, "mode")                { v_mode = v; }
                if let Some(v) = parse_kv(l, "auto_send")           { v_auto_send = parse_bool(&v); }
                if let Some(v) = parse_kv(l, "max_seconds")         { v_max_seconds = v.parse::<i64>().ok(); }
                if let Some(v) = parse_kv(l, "overlay_screen")      { v_overlay_screen = v; }
                if let Some(v) = parse_kv(l, "overlay_y_offset_px") { v_overlay_y_offset_px = v.parse::<i64>().ok(); }
                if let Some(v) = parse_kv(l, "keep_audio")          { v_keep_audio = parse_bool(&v); }
                if let Some(v) = parse_kv(l, "hf_endpoint")         { v_hf_endpoint = v; }
            }
            _ => {}
        }
    }
    if provider.is_empty() { provider = "proxy".into(); }
    Ok(json!({
        "path": path.display().to_string(),
        "provider": provider,
        "emergency_hotkey": emergency_hotkey,
        "temperature": temperature,
        "top_p": top_p,
        "proxy": {
            "base_url": proxy_base_url,
            "model": proxy_model,
            "api_key": proxy_api_key,
        },
        "anthropic": {
            "api_key": anthropic_api_key,
            "model": anthropic_model,
            "base_url": anthropic_base_url,
        },
        "copilot": {
            "model": copilot_model,
        },
        "voice": {
            "enabled": v_enabled,
            "engine": v_engine,
            "model_size": v_model_size,
            "language": v_language,
            "compute_type": v_compute_type,
            "device": v_device,
            "hotkey": v_hotkey,
            "hold_threshold_ms": v_hold_threshold_ms,
            "stop_mode": v_stop_mode,
            "start_feedback": v_start_feedback,
            "focus_aware": v_focus_aware,
            "mode": v_mode,
            "auto_send": v_auto_send,
            "max_seconds": v_max_seconds,
            "overlay_screen": v_overlay_screen,
            "overlay_y_offset_px": v_overlay_y_offset_px,
            "keep_audio": v_keep_audio,
            "hf_endpoint": v_hf_endpoint,
        },
    }))
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct ProviderProxyPatch {
    pub base_url: Option<String>,
    pub model: Option<String>,
    pub api_key: Option<String>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct ProviderAnthropicPatch {
    pub api_key: Option<String>,
    pub model: Option<String>,
    pub base_url: Option<String>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct ProviderCopilotPatch {
    pub model: Option<String>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct VoicePatch {
    pub enabled: Option<bool>,
    pub engine: Option<String>,
    pub model_size: Option<String>,
    pub language: Option<String>,
    pub compute_type: Option<String>,
    pub device: Option<String>,
    pub hotkey: Option<String>,
    pub hold_threshold_ms: Option<i64>,
    pub stop_mode: Option<String>,
    pub start_feedback: Option<String>,
    pub focus_aware: Option<bool>,
    pub mode: Option<String>,
    pub auto_send: Option<bool>,
    pub max_seconds: Option<i64>,
    pub overlay_screen: Option<String>,
    pub overlay_y_offset_px: Option<i64>,
    pub keep_audio: Option<bool>,
    pub hf_endpoint: Option<String>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct UiPatch {
    pub locale: Option<String>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct SettingsPatch {
    pub provider: Option<String>,
    pub temperature: Option<f64>,
    pub top_p: Option<f64>,
    pub emergency_hotkey: Option<String>,
    pub proxy: Option<ProviderProxyPatch>,
    pub anthropic: Option<ProviderAnthropicPatch>,
    pub copilot: Option<ProviderCopilotPatch>,
    pub voice: Option<VoicePatch>,
    pub ui: Option<UiPatch>,
}

/// Apply a partial patch to config.toml in-place using simple line rewriting
/// (preserving comments / unrelated keys). Only the supported keys are touched.
/// Sections that don't exist yet are appended at the end; missing keys in
/// existing sections are inserted right after the section header.
#[tauri::command]
pub async fn write_settings(patch: SettingsPatch) -> Result<Value, String> {
    let path = settings_path();
    if let Err(e) = ensure_settings_file(&path) {
        return Err(format!("ensure {}: {e}", path.display()));
    }
    let raw = std::fs::read_to_string(&path).map_err(|e| format!("read {}: {e}", path.display()))?;

    // Build the desired key→value map per section from the patch.
    let mut want: std::collections::BTreeMap<&'static str, Vec<(&'static str, String)>> = Default::default();
    if let Some(v) = &patch.provider  { want.entry("[llm]").or_default().push(("provider", v.clone())); }
    if let Some(v) = &patch.temperature { want.entry("[llm]").or_default().push(("temperature", format!("{}", v))); }
    if let Some(v) = &patch.top_p       { want.entry("[llm]").or_default().push(("top_p",       format!("{}", v))); }
    if let Some(p) = &patch.proxy {
        if let Some(v) = &p.base_url { want.entry("[llm.proxy]").or_default().push(("base_url", v.clone())); }
        if let Some(v) = &p.model    { want.entry("[llm.proxy]").or_default().push(("model",    v.clone())); }
        if let Some(v) = &p.api_key  { want.entry("[llm.proxy]").or_default().push(("api_key",  v.clone())); }
    }
    if let Some(p) = &patch.anthropic {
        if let Some(v) = &p.api_key  { want.entry("[llm.anthropic]").or_default().push(("api_key",  v.clone())); }
        if let Some(v) = &p.model    { want.entry("[llm.anthropic]").or_default().push(("model",    v.clone())); }
        if let Some(v) = &p.base_url { want.entry("[llm.anthropic]").or_default().push(("base_url", v.clone())); }
    }
    if let Some(p) = &patch.copilot {
        if let Some(v) = &p.model { want.entry("[llm.copilot]").or_default().push(("model", v.clone())); }
    }
    if let Some(v) = &patch.emergency_hotkey { want.entry("[safety]").or_default().push(("emergency_hotkey", v.clone())); }
    if let Some(p) = &patch.voice {
        if let Some(v) = &p.enabled            { want.entry("[voice]").or_default().push(("enabled",            if *v {"true".into()} else {"false".into()})); }
        if let Some(v) = &p.engine             { want.entry("[voice]").or_default().push(("engine",             v.clone())); }
        if let Some(v) = &p.model_size         { want.entry("[voice]").or_default().push(("model_size",         v.clone())); }
        if let Some(v) = &p.language           { want.entry("[voice]").or_default().push(("language",           v.clone())); }
        if let Some(v) = &p.compute_type       { want.entry("[voice]").or_default().push(("compute_type",       v.clone())); }
        if let Some(v) = &p.device             { want.entry("[voice]").or_default().push(("device",             v.clone())); }
        if let Some(v) = &p.hotkey             { want.entry("[voice]").or_default().push(("hotkey",             v.clone())); }
        if let Some(v) = &p.hold_threshold_ms  { want.entry("[voice]").or_default().push(("hold_threshold_ms",  format!("{}", v))); }
        if let Some(v) = &p.stop_mode          { want.entry("[voice]").or_default().push(("stop_mode",          v.clone())); }
        if let Some(v) = &p.start_feedback     { want.entry("[voice]").or_default().push(("start_feedback",     v.clone())); }
        if let Some(v) = &p.focus_aware        { want.entry("[voice]").or_default().push(("focus_aware",        if *v {"true".into()} else {"false".into()})); }
        if let Some(v) = &p.mode               { want.entry("[voice]").or_default().push(("mode",               v.clone())); }
        if let Some(v) = &p.auto_send          { want.entry("[voice]").or_default().push(("auto_send",          if *v {"true".into()} else {"false".into()})); }
        if let Some(v) = &p.max_seconds        { want.entry("[voice]").or_default().push(("max_seconds",        format!("{}", v))); }
        if let Some(v) = &p.overlay_screen     { want.entry("[voice]").or_default().push(("overlay_screen",     v.clone())); }
        if let Some(v) = &p.overlay_y_offset_px{ want.entry("[voice]").or_default().push(("overlay_y_offset_px",format!("{}", v))); }
        if let Some(v) = &p.keep_audio         { want.entry("[voice]").or_default().push(("keep_audio",         if *v {"true".into()} else {"false".into()})); }
        if let Some(v) = &p.hf_endpoint        { want.entry("[voice]").or_default().push(("hf_endpoint",        v.clone())); }
    }
    if let Some(p) = &patch.ui {
        if let Some(v) = &p.locale { want.entry("[ui]").or_default().push(("locale", v.clone())); }
    }

    // Group existing file into sections (preserving order). Index 0 is the
    // pre-section preamble (rare; probably empty/comments).
    let mut sections: Vec<(String, Vec<String>)> = vec![(String::new(), Vec::new())];
    for line in raw.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('[') && trimmed.ends_with(']') {
            sections.push((trimmed.to_string(), Vec::new()));
        } else {
            sections.last_mut().unwrap().1.push(line.to_string());
        }
    }

    let is_numeric = |k: &str| matches!(
        k,
        "temperature" | "top_p" | "hold_threshold_ms" | "max_seconds" | "overlay_y_offset_px"
        | "enabled" | "focus_aware" | "auto_send" | "keep_audio"
    );
    let format_kv = |k: &str, v: &str| -> String {
        if is_numeric(k) {
            format!("{k} = {v}")
        } else {
            let escaped = v.replace('\\', "\\\\").replace('"', "\\\"");
            format!("{k} = \"{escaped}\"")
        }
    };

    // Track which (section, key) pairs we've already written to suppress dup-appends.
    let mut written: std::collections::BTreeSet<(String, String)> = Default::default();

    // Pass 1: rewrite in-place inside existing sections.
    for (sec, lines) in sections.iter_mut() {
        let Some(targets) = want.get(sec.as_str()) else { continue };
        for line in lines.iter_mut() {
            let trimmed_line = line.trim_start();
            // skip comments
            if trimmed_line.starts_with('#') { continue; }
            for (k, v) in targets {
                // simple "key = ..." prefix match
                let prefix_eq = format!("{k} =");
                let prefix_eq2 = format!("{k}=");
                if trimmed_line.starts_with(&prefix_eq) || trimmed_line.starts_with(&prefix_eq2) {
                    *line = format_kv(k, v);
                    written.insert((sec.clone(), (*k).to_string()));
                    break;
                }
            }
        }
    }

    // Pass 2: insert missing keys right after their section header (still inside).
    for (sec, lines) in sections.iter_mut() {
        let Some(targets) = want.get(sec.as_str()) else { continue };
        let mut to_prepend: Vec<String> = Vec::new();
        for (k, v) in targets {
            if written.contains(&(sec.clone(), (*k).to_string())) { continue; }
            to_prepend.push(format_kv(k, v));
            written.insert((sec.clone(), (*k).to_string()));
        }
        if !to_prepend.is_empty() {
            // Insert at the top of the section's body so it stays under the header.
            let mut new_body = to_prepend;
            new_body.append(lines);
            *lines = new_body;
        }
    }

    // Pass 3: append entirely-new sections at the end.
    let existing_sections: std::collections::BTreeSet<String> =
        sections.iter().map(|(s, _)| s.clone()).collect();
    for (sec, kvs) in &want {
        if existing_sections.contains(*sec) { continue; }
        let mut body: Vec<String> = Vec::new();
        for (k, v) in kvs {
            body.push(format_kv(k, v));
            written.insert(((*sec).to_string(), (*k).to_string()));
        }
        sections.push(((*sec).to_string(), body));
    }

    // Reassemble.
    let mut out = String::with_capacity(raw.len() + 256);
    for (i, (sec, lines)) in sections.iter().enumerate() {
        if !sec.is_empty() {
            if i > 0 && !out.is_empty() && !out.ends_with("\n\n") {
                if !out.ends_with('\n') { out.push('\n'); }
            }
            out.push_str(sec);
            out.push('\n');
        }
        for l in lines {
            out.push_str(l);
            out.push('\n');
        }
    }

    std::fs::write(&path, &out).map_err(|e| format!("write: {e}"))?;
    Ok(json!({"ok": true, "path": path.display().to_string()}))
}

fn settings_path() -> std::path::PathBuf {
    if let Ok(p) = std::env::var("LUCID_CONFIG") {
        return std::path::PathBuf::from(p);
    }
    if let Ok(cwd) = std::env::var("LUCID_CWD") {
        return std::path::PathBuf::from(cwd).join("config.toml");
    }
    std::env::current_dir()
        .unwrap_or_default()
        .join("config.toml")
}

/// `write_settings` may be called before the file exists (e.g. user config was
/// never seeded because the bundled template was missing). Make sure the parent
/// directory exists and create an empty skeleton if needed.
fn ensure_settings_file(path: &std::path::Path) -> std::io::Result<()> {
    if path.exists() { return Ok(()); }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let skeleton = "[llm]\nprovider = \"anthropic\"\nmax_tokens = 16384\nkeep_recent_screenshots = 4\n\n[llm.anthropic]\nmodel = \"claude-opus-4-6\"\napi_key = \"\"\n\n[llm.copilot]\nmodel = \"claude-opus-4-6\"\n\n[llm.proxy]\nbase_url = \"http://localhost:4000\"\nmodel = \"\"\napi_key = \"\"\n";
    std::fs::write(path, skeleton)
}

fn parse_kv(line: &str, key: &str) -> Option<String> {
    let line = line.split('#').next()?.trim();
    let mut parts = line.splitn(2, '=');
    let k = parts.next()?.trim();
    if k != key { return None; }
    let v = parts.next()?.trim();
    if let Some(stripped) = v.strip_prefix('"').and_then(|s| s.strip_suffix('"')) {
        return Some(stripped.to_string());
    }
    Some(v.to_string())
}

/// Parse a TOML-ish boolean literal. Returns None for any unknown value so
/// we don't silently coerce bad config.
fn parse_bool(v: &str) -> Option<bool> {
    match v.trim().to_ascii_lowercase().as_str() {
        "true" | "yes" | "1" | "on" => Some(true),
        "false" | "no" | "0" | "off" => Some(false),
        _ => None,
    }
}

#[allow(dead_code)]
fn rewrite_kv(line: &str, key: &str, new_val: &str) -> Option<String> {
    let trimmed = line.trim_start();
    if !trimmed.starts_with(key) { return None; }
    let after = trimmed[key.len()..].trim_start();
    if !after.starts_with('=') { return None; }
    let escaped = new_val.replace('\\', "\\\\").replace('"', "\\\"");
    Some(format!("{key} = \"{escaped}\""))
}

#[allow(dead_code)]
fn rewrite_kv_raw(line: &str, key: &str, new_val: &str) -> Option<String> {
    let trimmed = line.trim_start();
    if !trimmed.starts_with(key) { return None; }
    let after = trimmed[key.len()..].trim_start();
    if !after.starts_with('=') { return None; }
    Some(format!("{key} = {new_val}"))
}

/// Run an adaptation self-check (Phase 1.5) by invoking `python -m lucid.selfcheck <what>`
/// out-of-band and returning the parsed JSON. Does NOT use the long-lived sidecar pipe;
/// each call is a one-shot subprocess so it works even before/after the sidecar is alive.
#[tauri::command]
pub async fn run_selfcheck(what: String) -> Result<Value, String> {
    let py = std::env::var("LUCID_PYTHON").unwrap_or_else(|_| "python".into());
    let mut cmd = Command::new(py);
    cmd.arg("-m").arg("lucid.selfcheck").arg(&what);
    if let Ok(cwd) = std::env::var("LUCID_CWD") { cmd.current_dir(cwd); }
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    #[cfg(windows)]
    { const CREATE_NO_WINDOW: u32 = 0x0800_0000; cmd.creation_flags(CREATE_NO_WINDOW); }
    let out = cmd.output().await.map_err(|e| format!("spawn selfcheck: {e}"))?;
    if !out.status.success() {
        return Err(format!(
            "selfcheck exited {:?}: {}",
            out.status.code(),
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    let text = String::from_utf8_lossy(&out.stdout);
    serde_json::from_str::<Value>(&text).map_err(|e| format!("parse selfcheck json: {e}"))
}


/// List run directories under <repo>/logs sorted newest first.
/// Returns: [{name, mtime_ms, instruction?}]
#[tauri::command]
pub async fn list_runs(app: AppHandle) -> Result<Vec<Value>, String> {
    let logs_dir = logs_root(&app);
    if !logs_dir.exists() {
        return Ok(vec![]);
    }
    let mut out = Vec::new();
    for entry in std::fs::read_dir(&logs_dir).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        if !entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            continue;
        }
        let name = entry.file_name().to_string_lossy().into_owned();
        let mtime = entry
            .metadata()
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        // Try to read first line of run.log for context (instruction is encoded in dir name slug too).
        out.push(json!({"name": name, "mtime_ms": mtime, "path": entry.path().display().to_string()}));
    }
    out.sort_by(|a, b| b["mtime_ms"].as_u64().cmp(&a["mtime_ms"].as_u64()));
    Ok(out)
}

/// Return the messages.jsonl content + list of png file names for a given run dir name.
#[tauri::command]
pub async fn read_run(app: AppHandle, name: String) -> Result<Value, String> {
    let dir = logs_root(&app).join(&name);
    if !dir.exists() {
        return Err(format!("run not found: {name}"));
    }
    let messages_path = dir.join("messages.jsonl");
    let mut steps = Vec::new();
    if messages_path.exists() {
        let raw = std::fs::read_to_string(&messages_path).map_err(|e| e.to_string())?;
        for line in raw.lines() {
            if line.trim().is_empty() {
                continue;
            }
            if let Ok(v) = serde_json::from_str::<Value>(line) {
                steps.push(v);
            }
        }
    }
    let mut images = Vec::new();
    for entry in std::fs::read_dir(&dir).map_err(|e| e.to_string())? {
        let entry = entry.map_err(|e| e.to_string())?;
        let fname = entry.file_name().to_string_lossy().into_owned();
        if fname.ends_with(".png") || fname.ends_with(".jpg") {
            images.push(fname);
        }
    }
    images.sort();
    let log_path = dir.join("run.log");
    let log_text = std::fs::read_to_string(&log_path).unwrap_or_default();
    Ok(json!({
        "name": name,
        "dir": dir.display().to_string(),
        "steps": steps,
        "images": images,
        "log": log_text,
    }))
}

/// Read a single image as base64 data URL. Used by the history UI.
#[tauri::command]
pub async fn read_image_b64(app: AppHandle, run_name: String, file_name: String) -> Result<String, String> {
    let p = logs_root(&app).join(&run_name).join(&file_name);
    let bytes = std::fs::read(&p).map_err(|e| e.to_string())?;
    use base64::Engine;
    let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
    let mime = if file_name.ends_with(".jpg") { "image/jpeg" } else { "image/png" };
    Ok(format!("data:{};base64,{}", mime, b64))
}

/// Read an arbitrary local image (e.g. an inbox paste, or any user-attached
/// file) as a base64 data URL so the webview can render it inline. The path
/// MUST live under either the inbox directory or the per-user app data
/// directory — otherwise we refuse, to avoid turning this into a file-read
/// oracle for the renderer.
#[tauri::command]
pub async fn read_attachment_b64(app: AppHandle, path: String) -> Result<String, String> {
    let pb = std::path::PathBuf::from(&path);
    let canon = std::fs::canonicalize(&pb).map_err(|e| format!("canonicalize: {e}"))?;
    let canon = strip_verbatim(canon);
    // Allow only paths under the inbox dir (or, in dev, the user data dir).
    let inbox = inbox_root(&app);
    let inbox_canon = std::fs::canonicalize(&inbox).unwrap_or(inbox.clone());
    let inbox_canon = strip_verbatim(inbox_canon);
    let app_data = lucid_home();
    let allowed = canon.starts_with(&inbox_canon) || canon.starts_with(&app_data);
    if !allowed {
        return Err(format!(
            "refused: path not under inbox/app data ({})",
            canon.display()
        ));
    }
    let bytes = std::fs::read(&canon).map_err(|e| format!("read: {e}"))?;
    if bytes.len() > 32 * 1024 * 1024 {
        return Err(format!("attachment too large: {} bytes", bytes.len()));
    }
    use base64::Engine;
    let b64 = base64::engine::general_purpose::STANDARD.encode(&bytes);
    let lower = canon
        .extension()
        .and_then(|s| s.to_str())
        .map(|s| s.to_ascii_lowercase())
        .unwrap_or_default();
    let mime = match lower.as_str() {
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        "gif" => "image/gif",
        "bmp" => "image/bmp",
        _ => "image/png",
    };
    Ok(format!("data:{};base64,{}", mime, b64))
}

/// Persist a clipboard-pasted image (or any in-memory blob) to the per-user
/// inbox directory and return its absolute path. Frontend calls this whenever
/// the user pastes a screenshot — the resulting path is then attached to the
/// next `start_task` as a `FileRef`, so the model can `load_local_images(path=…)`
/// on demand instead of having every paste burn an `image_url` block in
/// every request.
#[tauri::command]
pub async fn save_inbox_image(app: AppHandle, name: String, bytes: Vec<u8>) -> Result<Value, String> {
    use std::time::{SystemTime, UNIX_EPOCH};
    let inbox = inbox_root(&app);
    std::fs::create_dir_all(&inbox).map_err(|e| format!("create inbox dir: {e}"))?;
    // Sanitise extension from the suggested name; default to .png.
    let raw_ext = std::path::Path::new(&name)
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("png")
        .to_ascii_lowercase();
    let ext = match raw_ext.as_str() {
        "png" | "jpg" | "jpeg" | "webp" | "gif" | "bmp" => raw_ext,
        _ => "png".to_string(),
    };
    let ts = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis()).unwrap_or(0);
    // 8-hex from the low bits of ts ^ ptr — collision-resistant enough for inbox.
    let suffix = format!("{:08x}", (ts as u32) ^ (bytes.as_ptr() as usize as u32));
    let stem = std::path::Path::new(&name)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("paste")
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'))
        .take(40)
        .collect::<String>();
    let stem = if stem.is_empty() { "paste".to_string() } else { stem };
    let fname = format!("{}-{}-{}.{}", chrono_like_ts(ts), stem, suffix, ext);
    let target = inbox.join(&fname);
    std::fs::write(&target, &bytes).map_err(|e| format!("write inbox file: {e}"))?;
    Ok(json!({
        "path": target.to_string_lossy().to_string(),
        "name": name,
        "size": bytes.len(),
    }))
}

fn chrono_like_ts(ms: u128) -> String {
    // Minimal yyyymmdd-HHMMSS without bringing in chrono — uses UTC seconds.
    let secs = (ms / 1000) as i64;
    let days = secs.div_euclid(86_400);
    let tod = secs.rem_euclid(86_400) as u32;
    // 1970-01-01 baseline → year/month/day (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u32;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y0 = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y0 + 1 } else { y0 };
    let hh = tod / 3600;
    let mm = (tod % 3600) / 60;
    let ss = tod % 60;
    format!("{:04}{:02}{:02}-{:02}{:02}{:02}", y, m, d, hh, mm, ss)
}

/// Inbox directory for ephemeral pasted images. Same parent as `config.toml`,
/// e.g. `~/.lucid/inbox/`.
fn inbox_root(_app: &AppHandle) -> std::path::PathBuf {
    if let Ok(cwd) = std::env::var("LUCID_CWD") {
        let p = std::path::PathBuf::from(cwd).join("inbox");
        return p;
    }
    lucid_home().join("inbox")
}

/// Resolve the logs directory the sidecar writes to.
/// Priority:
///   1. `LUCID_LOGS_DIR` env (explicit override)
///   2. `<LUCID_CWD>/logs`
///   3. `~/.lucid/logs` (default in installed builds)
fn logs_root(_app: &AppHandle) -> std::path::PathBuf {
    if let Ok(p) = std::env::var("LUCID_LOGS_DIR") {
        return std::path::PathBuf::from(p);
    }
    if let Ok(cwd) = std::env::var("LUCID_CWD") {
        let p = std::path::PathBuf::from(cwd).join("logs");
        if p.exists() { return p; }
    }
    lucid_home().join("logs")
}
