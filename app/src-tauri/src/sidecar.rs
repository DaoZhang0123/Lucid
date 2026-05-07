//! Sidecar bridge: spawn `python -m ctrlapp --sidecar` and pipe NDJSON.
//!
//! - Frontend → Rust: invoke commands `sidecar_start_task / sidecar_cancel /
//!   sidecar_get_status / sidecar_set_autonomy / sidecar_ping`.
//! - Rust → Frontend: each line of sidecar stdout is forwarded as a Tauri
//!   event named `ctrlapp://event`.
//! - Crash recovery: if the child exits unexpectedly we emit
//!   `ctrlapp://sidecar` with `{kind:"exit", code}` and respawn after 1s.

use std::collections::HashMap;
use std::process::Stdio;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use once_cell::sync::OnceCell;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::{AppHandle, Emitter, Manager};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::{Child, ChildStdin, Command};
use tokio::sync::{oneshot, Mutex};

pub const EVENT_CTRLAPP: &str = "ctrlapp://event";
pub const EVENT_SIDECAR: &str = "ctrlapp://sidecar";

/// Tracks a running sidecar process & its inflight RPC requests.
pub struct Sidecar {
    stdin: Mutex<Option<ChildStdin>>,
    next_id: AtomicU64,
    pending: Mutex<HashMap<u64, oneshot::Sender<Result<Value, String>>>>,
}

impl Sidecar {
    fn new() -> Self {
        Self {
            stdin: Mutex::new(None),
            next_id: AtomicU64::new(1),
            pending: Mutex::new(HashMap::new()),
        }
    }

    /// Send a JSON-RPC request to the sidecar and wait for its response.
    pub async fn request(&self, method: &str, params: Value) -> Result<Value, String> {
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
        match tokio::time::timeout(std::time::Duration::from_secs(120), rx).await {
            Ok(Ok(res)) => res,
            Ok(Err(_)) => Err("sidecar response channel closed".into()),
            Err(_) => {
                self.pending.lock().await.remove(&id);
                Err("sidecar request timed out".into())
            }
        }
    }
}

static INSTANCE: OnceCell<Arc<Sidecar>> = OnceCell::new();

pub fn instance() -> Arc<Sidecar> {
    INSTANCE
        .get_or_init(|| Arc::new(Sidecar::new()))
        .clone()
}

/// Resolve (and lazily seed) the user-writable config path. Priority:
///   1. `CTRLAPP_CONFIG` env (explicit override).
///   2. `<app_local_data_dir>/config.toml` (per-user, e.g. `%LOCALAPPDATA%\dev.ctrlapp\config.toml`).
///      If missing, copy from the bundled default at `<resource_dir>/config.toml`.
///   3. Bundled default at `<resource_dir>/config.toml` (read-only fallback).
///   4. `<cwd>/config.toml` (dev mode).
pub fn ensure_user_config(app: &AppHandle) -> std::path::PathBuf {
    if let Ok(p) = std::env::var("CTRLAPP_CONFIG") {
        return std::path::PathBuf::from(p);
    }
    if let Ok(dir) = app.path().app_local_data_dir() {
        let dir = strip_verbatim(dir);
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
    if let Ok(res_dir) = app.path().resource_dir() {
        let p = res_dir.join("config.toml");
        if p.exists() { return p; }
    }
    std::env::current_dir().unwrap_or_default().join("config.toml")
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

/// How to invoke the python sidecar. Resolution priority:
///   1. `CTRLAPP_SIDECAR_EXE`  → spawn that binary directly with `--sidecar`.
///   2. Bundled `resources/ctrlapp/ctrlapp.exe` (PyInstaller output for packaged builds).
///   3. `CTRLAPP_PYTHON` env (path to a python.exe) + `-m ctrlapp --sidecar` (dev mode).
///   4. `python -m ctrlapp --sidecar` (system python on PATH).
fn build_command(app: &AppHandle) -> (Command, String) {
    let cfg_path = ensure_user_config(app);
    let cfg_str = cfg_path.display().to_string();
    // 1) explicit binary override
    if let Ok(exe) = std::env::var("CTRLAPP_SIDECAR_EXE") {
        let mut cmd = Command::new(&exe);
        cmd.arg("--sidecar").arg("--config").arg(&cfg_str);
        configure_common(&mut cmd, &cfg_str);
        return (cmd, exe);
    }
    // 2) bundled exe shipped via tauri.conf.json `bundle.resources`
    if let Ok(res_dir) = app.path().resource_dir() {
        let res_dir = strip_verbatim(res_dir);
        // PyInstaller onefile output: a single self-extracting exe at the
        // resource_dir root (see packaging/ctrlapp.spec).
        let candidate = res_dir.join("ctrlapp.exe");
        if candidate.exists() {
            let mut cmd = Command::new(&candidate);
            cmd.arg("--sidecar").arg("--config").arg(&cfg_str);
            configure_common(&mut cmd, &cfg_str);
            return (cmd, candidate.display().to_string());
        }
    }
    // 3) dev mode: explicit python interpreter via env
    let py = std::env::var("CTRLAPP_PYTHON").unwrap_or_else(|_| "python".into());
    let mut cmd = Command::new(&py);
    cmd.arg("-m").arg("ctrlapp").arg("--sidecar").arg("--config").arg(&cfg_str);
    configure_common(&mut cmd, &cfg_str);
    (cmd, format!("{py} -m ctrlapp"))
}

fn configure_common(cmd: &mut Command, cfg_path: &str) {
    if let Ok(cwd) = std::env::var("CTRLAPP_CWD") {
        cmd.current_dir(cwd);
    }
    // Make the config path discoverable even if --config is dropped or wrapped.
    cmd.env("CTRLAPP_CONFIG", cfg_path);
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
            match spawn_once(&app).await {
                Ok(code) => {
                    let _ = app.emit(
                        EVENT_SIDECAR,
                        json!({"kind": "exit", "code": code}),
                    );
                    log::warn!("sidecar exited code={code:?}, respawning in 1s");
                }
                Err(e) => {
                    let _ = app.emit(
                        EVENT_SIDECAR,
                        json!({"kind": "spawn_error", "message": e.to_string()}),
                    );
                    log::error!("sidecar spawn failed: {e}");
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
    let stdin = child.stdin.take().ok_or("no stdin")?;
    let stdout = child.stdout.take().ok_or("no stdout")?;
    let stderr = child.stderr.take().ok_or("no stderr")?;

    let sidecar = instance();
    *sidecar.stdin.lock().await = Some(stdin);

    // stdout reader
    let app_out = app.clone();
    let sidecar_out = sidecar.clone();
    tauri::async_runtime::spawn(async move {
        let reader = BufReader::new(stdout);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
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
            let _ = app_out.emit(EVENT_CTRLAPP, v);
        }
    });

    // stderr → forwarded as event with kind:"log" so the UI can show python tracebacks.
    let app_err = app.clone();
    tauri::async_runtime::spawn(async move {
        let reader = BufReader::new(stderr);
        let mut lines = reader.lines();
        while let Ok(Some(line)) = lines.next_line().await {
            let _ = app_err.emit(
                EVENT_SIDECAR,
                json!({"kind": "stderr", "line": line}),
            );
        }
    });

    let status = child.wait().await.map_err(|e| format!("wait: {e}"))?;
    // Drop stdin so caller knows we're disconnected; clear pending requests.
    *sidecar.stdin.lock().await = None;
    let mut pending = sidecar.pending.lock().await;
    for (_, tx) in pending.drain() {
        let _ = tx.send(Err("sidecar terminated".into()));
    }
    Ok(status.code())
}

// ---------- Tauri commands exposed to the frontend ----------

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct StartArgs {
    pub instruction: String,
    pub autonomy: Option<String>,
    pub max_steps: Option<u32>,
}

#[tauri::command]
pub async fn sidecar_start_task(args: StartArgs) -> Result<Value, String> {
    let mut params = json!({"instruction": args.instruction});
    if let Some(a) = args.autonomy {
        params["autonomy"] = json!(a);
    }
    if let Some(m) = args.max_steps {
        params["max_steps"] = json!(m);
    }
    instance().request("start_task", params).await
}

#[tauri::command]
pub async fn sidecar_cancel() -> Result<Value, String> {
    instance().request("cancel", json!({})).await
}

#[tauri::command]
pub async fn sidecar_get_status() -> Result<Value, String> {
    instance().request("get_status", json!({})).await
}

#[tauri::command]
pub async fn sidecar_set_autonomy(autonomy: String) -> Result<Value, String> {
    instance()
        .request("set_autonomy", json!({"autonomy": autonomy}))
        .await
}

/// Liveness probe of the sidecar JSON-RPC pipe.
#[tauri::command]
pub async fn sidecar_ping() -> Result<Value, String> {
    instance().request("ping", json!({})).await
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
    instance().request("thread_new", json!({"title": title.unwrap_or_default()})).await
}

#[tauri::command]
pub async fn thread_list() -> Result<Value, String> {
    instance().request("thread_list", json!({})).await
}

#[tauri::command]
pub async fn thread_read(id: String) -> Result<Value, String> {
    instance().request("thread_read", json!({"id": id})).await
}

#[tauri::command]
pub async fn thread_set_active(id: Option<String>) -> Result<Value, String> {
    instance().request("thread_set_active", json!({"id": id})).await
}

#[tauri::command]
pub async fn thread_delete(id: String) -> Result<Value, String> {
    instance().request("thread_delete", json!({"id": id})).await
}

// ---- Task queue ----

#[tauri::command]
pub async fn task_queue_list() -> Result<Value, String> {
    instance().request("queue_list", json!({})).await
}

#[tauri::command]
pub async fn task_queue_remove(thread_id: String) -> Result<Value, String> {
    instance().request("queue_remove", json!({"thread_id": thread_id})).await
}

#[tauri::command]
pub async fn task_queue_clear() -> Result<Value, String> {
    instance().request("queue_clear", json!({})).await
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
pub async fn template_add(name: String, instruction: String, autonomy: Option<String>, max_steps: Option<i64>) -> Result<Value, String> {
    instance().request("template_add", json!({
        "name": name, "instruction": instruction,
        "autonomy": autonomy, "max_steps": max_steps,
    })).await
}

#[tauri::command]
pub async fn template_update(id: String, name: Option<String>, instruction: Option<String>, autonomy: Option<String>, max_steps: Option<i64>) -> Result<Value, String> {
    instance().request("template_update", json!({
        "id": id, "name": name, "instruction": instruction,
        "autonomy": autonomy, "max_steps": max_steps,
    })).await
}

#[tauri::command]
pub async fn template_delete(id: String) -> Result<Value, String> {
    instance().request("template_delete", json!({"id": id})).await
}

// ---- 定时任务 ----
#[tauri::command]
pub async fn schedule_list() -> Result<Value, String> {
    instance().request("schedule_list", json!({})).await
}

#[tauri::command]
pub async fn schedule_add(name: String, instruction: String, spec: Value, autonomy: Option<String>, max_steps: Option<i64>, enabled: Option<bool>, constraints: Option<Value>, auto_chat_apps: Option<Vec<String>>) -> Result<Value, String> {
    instance().request("schedule_add", json!({
        "name": name, "instruction": instruction, "spec": spec,
        "autonomy": autonomy, "max_steps": max_steps,
        "enabled": enabled.unwrap_or(true),
        "constraints": constraints,
        "auto_chat_apps": auto_chat_apps,
    })).await
}

#[tauri::command]
pub async fn schedule_update(id: String, name: Option<String>, instruction: Option<String>, spec: Option<Value>, autonomy: Option<String>, max_steps: Option<i64>, enabled: Option<bool>, constraints: Option<Value>, auto_chat_apps: Option<Vec<String>>) -> Result<Value, String> {
    instance().request("schedule_update", json!({
        "id": id, "name": name, "instruction": instruction, "spec": spec,
        "autonomy": autonomy, "max_steps": max_steps, "enabled": enabled,
        "constraints": constraints,
        "auto_chat_apps": auto_chat_apps,
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

/// Read base_url / model / api_key + autonomy / max_steps from config.toml so the
/// settings UI can prefill. Returns null fields if config can't be read.
#[tauri::command]
pub async fn read_settings() -> Result<Value, String> {
    let path = settings_path();
    let raw = std::fs::read_to_string(&path).unwrap_or_default();
    let mut provider = String::new();
    let mut max_steps: i64 = 0;
    let mut autonomy = String::new();
    let mut temperature: Option<f64> = None;
    let mut top_p: Option<f64> = None;
    let mut proxy_base_url = String::new();
    let mut proxy_model = String::new();
    let mut proxy_api_key = String::new();
    let mut anthropic_api_key = String::new();
    let mut anthropic_model = String::new();
    let mut anthropic_base_url = String::new();
    let mut copilot_model = String::new();
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
                if let Some(v) = parse_kv(l, "max_steps")  { max_steps  = v.parse::<i64>().unwrap_or(0); }
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
                if let Some(v) = parse_kv(l, "autonomy") { autonomy = v; }
            }
            _ => {}
        }
    }
    if provider.is_empty() { provider = "proxy".into(); }
    Ok(json!({
        "path": path.display().to_string(),
        "provider": provider,
        "autonomy": autonomy,
        "max_steps": max_steps,
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
pub struct SettingsPatch {
    pub provider: Option<String>,
    pub autonomy: Option<String>,
    pub max_steps: Option<i64>,
    pub temperature: Option<f64>,
    pub top_p: Option<f64>,
    pub proxy: Option<ProviderProxyPatch>,
    pub anthropic: Option<ProviderAnthropicPatch>,
    pub copilot: Option<ProviderCopilotPatch>,
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
    if let Some(v) = &patch.max_steps { want.entry("[llm]").or_default().push(("max_steps", v.to_string())); }
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
    if let Some(v) = &patch.autonomy { want.entry("[safety]").or_default().push(("autonomy", v.clone())); }

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

    let is_numeric = |k: &str| matches!(k, "max_steps" | "temperature" | "top_p");
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
    if let Ok(p) = std::env::var("CTRLAPP_CONFIG") {
        return std::path::PathBuf::from(p);
    }
    if let Ok(cwd) = std::env::var("CTRLAPP_CWD") {
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
    let skeleton = "[llm]\nprovider = \"anthropic\"\nmax_steps = 25\nmax_tokens = 16384\nkeep_recent_screenshots = 4\n\n[llm.anthropic]\nmodel = \"claude-opus-4-6\"\napi_key = \"\"\n\n[llm.copilot]\nmodel = \"claude-opus-4-6\"\n\n[llm.proxy]\nbase_url = \"http://localhost:4000\"\nmodel = \"\"\napi_key = \"\"\n\n[safety]\nautonomy = \"confirm_critical\"\n";
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

/// Run an adaptation self-check (Phase 1.5) by invoking `python -m ctrlapp.selfcheck <what>`
/// out-of-band and returning the parsed JSON. Does NOT use the long-lived sidecar pipe;
/// each call is a one-shot subprocess so it works even before/after the sidecar is alive.
#[tauri::command]
pub async fn run_selfcheck(what: String) -> Result<Value, String> {
    let py = std::env::var("CTRLAPP_PYTHON").unwrap_or_else(|_| "python".into());
    let mut cmd = Command::new(py);
    cmd.arg("-m").arg("ctrlapp.selfcheck").arg(&what);
    if let Ok(cwd) = std::env::var("CTRLAPP_CWD") { cmd.current_dir(cwd); }
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

/// Resolve the logs directory the sidecar writes to.
/// Priority:
///   1. `CTRLAPP_LOGS_DIR` env (explicit override)
///   2. `<CTRLAPP_CWD>/logs`
///   3. `<app_local_data_dir>/logs` (default in installed builds)
fn logs_root(app: &AppHandle) -> std::path::PathBuf {
    if let Ok(p) = std::env::var("CTRLAPP_LOGS_DIR") {
        return std::path::PathBuf::from(p);
    }
    if let Ok(cwd) = std::env::var("CTRLAPP_CWD") {
        let p = std::path::PathBuf::from(cwd).join("logs");
        if p.exists() { return p; }
    }
    if let Ok(dir) = app.path().app_local_data_dir() {
        return strip_verbatim(dir).join("logs");
    }
    std::path::PathBuf::from("logs")
}
