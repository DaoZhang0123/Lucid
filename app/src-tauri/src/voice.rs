//! Voice (push-to-talk) bridge.
//!
//! Most of the interesting logic — long-press detection, `MediaRecorder`
//! lifecycle, overlay state management — lives in the frontend
//! (`app/src/lib/voice.ts` + `app/src/routes/voice-overlay/+page.svelte`).
//! Rust here is intentionally thin:
//!
//! - `voice_overlay_show / hide / set_state` create / manage a separate
//!   always-on-top, transparent, click-through `WebviewWindow` pinned at the
//!   top-center of the chosen monitor.
//! - `voice_register_hotkey` (re)registers the global shortcut driving PTT.
//!   We forward Pressed / Released events to the frontend via
//!   `lucid://voice` so the long-press timer + recorder run in JS.
//! - `sidecar_transcribe` proxies the base64 audio blob to the Python
//!   sidecar's `transcribe_audio` RPC.
//!
//! Why frontend-heavy: `MediaRecorder` (and `getUserMedia`) is only available
//! to the webview, and reusing the existing Svelte i18n / theme / state
//! plumbing for the overlay UI is much cheaper than reimplementing it in Rust.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::sync::Mutex;
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, WebviewUrl};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

use crate::sidecar;

pub const EVENT_VOICE: &str = "lucid://voice";

const OVERLAY_LABEL: &str = "voice-overlay";

// Currently-registered voice hotkey (if any). Tracked here so we can unregister
// it cleanly before installing a replacement when the user changes the binding
// in /settings.
static CURRENT_HOTKEY: Mutex<Option<Shortcut>> = Mutex::new(None);

// ---------------------------------------------------------------------------
// Hotkey parsing — accept human strings like "Space", "Ctrl+Alt+V", "F12"
// ---------------------------------------------------------------------------

fn parse_modifier(token: &str) -> Option<Modifiers> {
    match token.to_ascii_lowercase().as_str() {
        "ctrl" | "control" => Some(Modifiers::CONTROL),
        "alt" => Some(Modifiers::ALT),
        "shift" => Some(Modifiers::SHIFT),
        "win" | "meta" | "super" | "cmd" => Some(Modifiers::META),
        _ => None,
    }
}

fn parse_code(token: &str) -> Option<Code> {
    let t = token.trim();
    if t.is_empty() { return None; }
    // Single letter
    if t.len() == 1 {
        let c = t.chars().next().unwrap().to_ascii_uppercase();
        return match c {
            'A'..='Z' => match c {
                'A' => Some(Code::KeyA), 'B' => Some(Code::KeyB), 'C' => Some(Code::KeyC),
                'D' => Some(Code::KeyD), 'E' => Some(Code::KeyE), 'F' => Some(Code::KeyF),
                'G' => Some(Code::KeyG), 'H' => Some(Code::KeyH), 'I' => Some(Code::KeyI),
                'J' => Some(Code::KeyJ), 'K' => Some(Code::KeyK), 'L' => Some(Code::KeyL),
                'M' => Some(Code::KeyM), 'N' => Some(Code::KeyN), 'O' => Some(Code::KeyO),
                'P' => Some(Code::KeyP), 'Q' => Some(Code::KeyQ), 'R' => Some(Code::KeyR),
                'S' => Some(Code::KeyS), 'T' => Some(Code::KeyT), 'U' => Some(Code::KeyU),
                'V' => Some(Code::KeyV), 'W' => Some(Code::KeyW), 'X' => Some(Code::KeyX),
                'Y' => Some(Code::KeyY), 'Z' => Some(Code::KeyZ),
                _ => None,
            },
            '0'..='9' => match c {
                '0' => Some(Code::Digit0), '1' => Some(Code::Digit1), '2' => Some(Code::Digit2),
                '3' => Some(Code::Digit3), '4' => Some(Code::Digit4), '5' => Some(Code::Digit5),
                '6' => Some(Code::Digit6), '7' => Some(Code::Digit7), '8' => Some(Code::Digit8),
                '9' => Some(Code::Digit9),
                _ => None,
            },
            _ => None,
        };
    }
    // Multi-char names
    let lower = t.to_ascii_lowercase();
    Some(match lower.as_str() {
        "space" | "spacebar" => Code::Space,
        "enter" | "return" => Code::Enter,
        "tab" => Code::Tab,
        "esc" | "escape" => Code::Escape,
        "backspace" => Code::Backspace,
        "delete" | "del" => Code::Delete,
        "insert" | "ins" => Code::Insert,
        "home" => Code::Home,
        "end" => Code::End,
        "pageup" | "pgup" => Code::PageUp,
        "pagedown" | "pgdn" => Code::PageDown,
        "left" | "arrowleft" => Code::ArrowLeft,
        "right" | "arrowright" => Code::ArrowRight,
        "up" | "arrowup" => Code::ArrowUp,
        "down" | "arrowdown" => Code::ArrowDown,
        "f1" => Code::F1, "f2" => Code::F2, "f3" => Code::F3, "f4" => Code::F4,
        "f5" => Code::F5, "f6" => Code::F6, "f7" => Code::F7, "f8" => Code::F8,
        "f9" => Code::F9, "f10" => Code::F10, "f11" => Code::F11, "f12" => Code::F12,
        _ => return None,
    })
}

/// Parse "Ctrl+Alt+Space" / "Space" / "F12" into a Shortcut.
pub fn parse_shortcut(spec: &str) -> Result<Shortcut, String> {
    let spec = spec.trim();
    if spec.is_empty() { return Err("empty hotkey".into()); }
    let mut mods = Modifiers::empty();
    let mut code: Option<Code> = None;
    for tok in spec.split('+').map(str::trim).filter(|s| !s.is_empty()) {
        if let Some(m) = parse_modifier(tok) {
            mods |= m;
        } else if let Some(c) = parse_code(tok) {
            if code.is_some() {
                return Err(format!("multiple non-modifier keys in '{spec}'"));
            }
            code = Some(c);
        } else {
            return Err(format!("unknown key token: {tok}"));
        }
    }
    let code = code.ok_or_else(|| format!("hotkey '{spec}' has no main key"))?;
    let mods_opt = if mods.is_empty() { None } else { Some(mods) };
    Ok(Shortcut::new(mods_opt, code))
}

// ---------------------------------------------------------------------------
// Tauri commands
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TranscribeArgs {
    pub audio_b64: String,
    #[serde(default)]
    pub mime: Option<String>,
    #[serde(default)]
    pub ui_locale: Option<String>,
}

/// Forward a base64-encoded audio blob to the Python sidecar for transcription.
#[tauri::command]
pub async fn sidecar_transcribe(args: TranscribeArgs) -> Result<Value, String> {
    let mut params = json!({"audio_b64": args.audio_b64});
    if let Some(m) = args.mime {
        params["mime"] = json!(m);
    }
    if let Some(l) = args.ui_locale {
        params["ui_locale"] = json!(l);
    }
    sidecar::instance().request("transcribe_audio", params).await
}

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DispatchContext {
    #[serde(default)]
    pub has_running_thread: Option<bool>,
    #[serde(default)]
    pub active_input_focus: Option<bool>,
    #[serde(default)]
    pub last_user_text: Option<String>,
    #[serde(default)]
    pub locale: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DispatchArgs {
    pub text: String,
    #[serde(default)]
    pub mode: Option<String>,
    #[serde(default)]
    pub context: Option<DispatchContext>,
}

/// Classify a transcribed utterance into thread_new / thread_abort /
/// dictation_append. See ``lucid/voice_dispatch.py`` and
/// ``Docs/voice-input.md`` §5.2.
#[tauri::command]
pub async fn voice_dispatch(args: DispatchArgs) -> Result<Value, String> {
    let mut params = json!({"text": args.text});
    if let Some(m) = args.mode {
        params["mode"] = json!(m);
    }
    if let Some(ctx) = args.context {
        let mut c = serde_json::Map::new();
        if let Some(v) = ctx.has_running_thread { c.insert("has_running_thread".into(), json!(v)); }
        if let Some(v) = ctx.active_input_focus { c.insert("active_input_focus".into(), json!(v)); }
        if let Some(v) = ctx.last_user_text { c.insert("last_user_text".into(), json!(v)); }
        if let Some(v) = ctx.locale { c.insert("locale".into(), json!(v)); }
        if !c.is_empty() {
            params["context"] = Value::Object(c);
        }
    }
    sidecar::instance().request("voice_dispatch", params).await
}

#[tauri::command]
pub async fn voice_status() -> Result<Value, String> {
    sidecar::instance().request("voice_status", json!({})).await
}

#[tauri::command]
pub async fn voice_unload() -> Result<Value, String> {
    sidecar::instance().request("voice_unload", json!({})).await
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ModelArgs {
    pub model_size: String,
    #[serde(default)]
    pub hf_endpoint: Option<String>,
}

/// Where the requested Whisper model is cached. `location` is "user", "bundled", or "".
#[tauri::command]
pub async fn voice_model_status(args: ModelArgs) -> Result<Value, String> {
    let mut p = json!({"model_size": args.model_size});
    if let Some(e) = &args.hf_endpoint {
        p["hf_endpoint"] = json!(e);
    }
    sidecar::instance().request("voice_model_status", p).await
}

/// Pre-download a Whisper model into the user cache. Blocking; can take a
/// while for `small` (~488MB) or larger over a slow link.
#[tauri::command]
pub async fn voice_download_model(args: ModelArgs) -> Result<Value, String> {
    let mut p = json!({"model_size": args.model_size});
    if let Some(e) = &args.hf_endpoint {
        p["hf_endpoint"] = json!(e);
    }
    sidecar::instance().request("voice_download_model", p).await
}

#[tauri::command]
pub async fn voice_config() -> Result<Value, String> {
    sidecar::instance().request("voice_config", json!({})).await
}

#[derive(Debug, Default, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct OverlayShowArgs {
    /// "cursor" | "primary" | "active-window" — which monitor to anchor to.
    #[serde(default)]
    pub screen: Option<String>,
    /// Distance from the top edge in logical pixels.
    #[serde(default)]
    pub y_offset_px: Option<i32>,
    /// Initial state; passed straight through to the overlay page via URL hash.
    #[serde(default)]
    pub state: Option<String>,
}

/// Create (or focus + reposition) the always-on-top, transparent, click-through
/// voice overlay window pinned to the top-center of the chosen monitor.
#[tauri::command]
pub async fn voice_overlay_show(app: AppHandle, args: OverlayShowArgs) -> Result<(), String> {
    let initial_state = args.state.unwrap_or_else(|| "holding".to_string());
    let url_path = format!("voice-overlay#{}", initial_state);
    let win = if let Some(existing) = app.get_webview_window(OVERLAY_LABEL) {
        existing
    } else {
        let builder = tauri::WebviewWindowBuilder::new(
            &app,
            OVERLAY_LABEL,
            WebviewUrl::App(url_path.into()),
        )
        .title("Lucid Voice")
        .inner_size(420.0, 96.0)
        .always_on_top(true)
        .decorations(false)
        .shadow(false)
        .skip_taskbar(true)
        .resizable(false)
        .transparent(true)
        .focused(false)
        .visible(false);
        builder.build().map_err(|e| format!("build overlay: {e}"))?
    };

    // ---- position: top-center of the chosen monitor ----
    // Pick a monitor based on `screen`. We resolve via the overlay window's
    // primary/current monitor first; for "cursor" we'd need the cursor position
    // which Tauri doesn't expose portably — fall back to primary in that case.
    let mon = match args.screen.as_deref() {
        Some("primary") => win.primary_monitor().ok().flatten(),
        // Default: the monitor the overlay is currently on (which after a fresh
        // build is the primary). Good enough as a first cut; the overlay never
        // moves between sessions so this stays predictable.
        _ => win.current_monitor().ok().flatten().or_else(|| win.primary_monitor().ok().flatten()),
    };
    if let Some(mon) = mon {
        let scale = mon.scale_factor();
        let mw = mon.size().width as f64; // physical pixels
        let mx = mon.position().x as f64;
        let my = mon.position().y as f64;
        let logical_w = 420.0_f64;
        let physical_w = logical_w * scale;
        let x = mx + (mw - physical_w) / 2.0;
        let y_off = args.y_offset_px.unwrap_or(8) as f64 * scale;
        let y = my + y_off;
        let _ = win.set_position(PhysicalPosition::new(x.round() as i32, y.round() as i32));
    }

    // Interactive by default — the overlay needs to receive clicks for its
    // controls (cancel button, mode toggle) and for drag-to-reposition. The
    // window is small (420x96) so blocking clicks under it for a few seconds
    // is fine. Per-element click-through is impossible at the OS level; the
    // old per-button passthrough toggle silently never worked anyway because
    // mouseenter doesn't fire on a fully-passthrough window.
    let _ = win.set_ignore_cursor_events(false);
    let _ = win.show();
    // Don't focus — must not steal focus from whatever the user is doing.
    Ok(())
}

#[tauri::command]
pub async fn voice_overlay_hide(app: AppHandle) -> Result<(), String> {
    if let Some(w) = app.get_webview_window(OVERLAY_LABEL) {
        let _ = w.hide();
    }
    Ok(())
}

/// Push a state-change message into the overlay window. Payload shape is
/// up to the frontend; common keys: `state`, `text`, `recordingMs`, `error`.
#[tauri::command]
pub async fn voice_overlay_set_state(app: AppHandle, payload: Value) -> Result<(), String> {
    if app.get_webview_window(OVERLAY_LABEL).is_some() {
        let _ = app.emit_to(OVERLAY_LABEL, "voice-overlay-state", payload);
    }
    Ok(())
}

/// Toggle whether the overlay should pass mouse clicks straight through (true,
/// the default) or capture them (false, while hovering an interactive control).
#[tauri::command]
pub async fn voice_overlay_set_passthrough(app: AppHandle, passthrough: bool) -> Result<(), String> {
    if let Some(w) = app.get_webview_window(OVERLAY_LABEL) {
        let _ = w.set_ignore_cursor_events(passthrough);
    }
    Ok(())
}

/// (Re)register the voice PTT global shortcut. Pressed / Released events are
/// forwarded to the frontend on `lucid://voice` so the long-press timer +
/// MediaRecorder live in JS.
///
/// Special case: when `hotkey == "Space"` (no modifiers) on Windows, we use a
/// low-level `WH_KEYBOARD_LL` hook instead of `tauri-plugin-global-shortcut`
/// so short taps fall through to the focused app (see `space_hook.rs`). For
/// any other binding (`Ctrl+Space`, `F12`, …) we keep using the cross-platform
/// global-shortcut path.
#[tauri::command]
pub fn voice_register_hotkey(
    app: AppHandle,
    hotkey: String,
    hold_threshold_ms: Option<i64>,
) -> Result<(), String> {
    // Drop any existing global-shortcut binding first.
    {
        let plugin = app.global_shortcut();
        let mut guard = CURRENT_HOTKEY.lock().map_err(|e| e.to_string())?;
        if let Some(prev) = guard.take() {
            let _ = plugin.unregister(prev);
        }
    }

    let trimmed = hotkey.trim();
    let is_bare_space = trimmed.eq_ignore_ascii_case("space")
        || trimmed.eq_ignore_ascii_case("spacebar");

    #[cfg(windows)]
    {
        if is_bare_space {
            let threshold = hold_threshold_ms.unwrap_or(5000);
            crate::space_hook::install(app.clone(), threshold);
            return Ok(());
        }
        // Falling through to global-shortcut → make sure the hook isn't lingering.
        crate::space_hook::uninstall();
    }
    #[cfg(not(windows))]
    {
        let _ = is_bare_space; // unused on non-Windows
    }

    let new_sc = parse_shortcut(trimmed)?;
    let plugin = app.global_shortcut();
    plugin.register(new_sc).map_err(|e| format!("register {hotkey}: {e}"))?;
    *CURRENT_HOTKEY.lock().map_err(|e| e.to_string())? = Some(new_sc);
    Ok(())
}

#[tauri::command]
pub fn voice_unregister_hotkey(app: AppHandle) -> Result<(), String> {
    #[cfg(windows)]
    crate::space_hook::uninstall();
    let plugin = app.global_shortcut();
    let mut guard = CURRENT_HOTKEY.lock().map_err(|e| e.to_string())?;
    if let Some(prev) = guard.take() {
        let _ = plugin.unregister(prev);
    }
    Ok(())
}

/// Forward a Pressed/Released event for the currently-registered voice
/// hotkey to the frontend. Called from the global-shortcut plugin handler
/// installed in `lib.rs`.
pub fn forward_hotkey_event(app: &AppHandle, sc: &Shortcut, state: ShortcutState) {
    let cur = CURRENT_HOTKEY.lock().ok().and_then(|g| *g);
    if cur.as_ref() != Some(sc) { return; }
    let kind = match state {
        ShortcutState::Pressed => "pressed",
        ShortcutState::Released => "released",
    };
    let _ = app.emit(EVENT_VOICE, json!({"kind": kind}));
}
