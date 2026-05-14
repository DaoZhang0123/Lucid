//! Low-level Win32 keyboard hook for Space key with **press-through**.
//!
//! Tauri's global-shortcut plugin grabs Space at the OS level and suppresses
//! the original keystroke entirely — which means with `hotkey = "Space"` the
//! user can no longer type a literal space anywhere. This module provides the
//! alternative path used when the configured PTT hotkey is bare `Space`:
//!
//! 1.  Install `WH_KEYBOARD_LL` on a dedicated thread that owns a message pump.
//! 2.  Swallow the *real* Space DOWN/UP edges so they don't reach the OS.
//! 3.  Schedule a delayed `pressed` emit so very short taps (< grace) never
//!     light up the holding overlay.
//! 4.  On Space UP:
//!       • duration ≥ `hold_threshold_ms` — emit `released` only; the JS-side
//!         state machine has already entered recording at the threshold.
//!       • duration < `hold_threshold_ms` — emit `released` (only if `pressed`
//!         was already sent, so JS cancels the overlay), then *replay* a
//!         synthetic Space DOWN+UP via `SendInput` so the focused window sees a
//!         normal Space tap. The synthetic events carry a sentinel value in
//!         `dwExtraInfo` so the hook ignores its own injection.
//!
//! For any hotkey other than bare `Space`, `voice.rs` keeps using
//! `tauri-plugin-global-shortcut` (e.g. `Ctrl+Space`, `F12`).

#![cfg(windows)]

use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU32, AtomicUsize, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::json;
use tauri::{AppHandle, Emitter};
use windows::Win32::Foundation::{LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::System::Threading::GetCurrentThreadId;
use windows::Win32::UI::Input::KeyboardAndMouse::{
    SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYBD_EVENT_FLAGS,
    KEYEVENTF_KEYUP, VIRTUAL_KEY, VK_SPACE,
};
use windows::Win32::UI::WindowsAndMessaging::{
    CallNextHookEx, DispatchMessageW, GetMessageW, PostThreadMessageW, SetWindowsHookExW,
    TranslateMessage, UnhookWindowsHookEx, KBDLLHOOKSTRUCT, LLKHF_INJECTED, MSG,
    WH_KEYBOARD_LL, WM_KEYDOWN, WM_KEYUP, WM_QUIT, WM_SYSKEYDOWN, WM_SYSKEYUP,
};

/// Sentinel placed in `KBDLLHOOKSTRUCT.dwExtraInfo` for our own replayed
/// SendInput events, so the hook callback can recognise & ignore them.
const SYNTH_TAG: usize = 0x4C55_4344; // "LUCD"

/// Grace window before showing the holding overlay. A real "tap" should never
/// flash UI; only sustained holds light up the progress bar.
const PRESSED_EMIT_GRACE_MS: u64 = 180;

// ---- module-level state ----
static HOOK_THREAD_ID: AtomicU32 = AtomicU32::new(0);
static HOOK_HANDLE: AtomicUsize = AtomicUsize::new(0); // HHOOK as usize
static INSTALLED: AtomicBool = AtomicBool::new(false);

// Threshold (in ms) above which a Space hold is considered PTT, not a tap.
// JS owns the "enter recording" trigger at this threshold; Rust uses it to
// decide whether to replay a synthetic Space on release.
static HOLD_THRESHOLD_MS: AtomicI64 = AtomicI64::new(5000);

// Per-press state.
static PRESS_INSTANT: Mutex<Option<Instant>> = Mutex::new(None);
static PRESSED_EMITTED: AtomicBool = AtomicBool::new(false);
static IS_HELD: AtomicBool = AtomicBool::new(false);
// Monotonic counter used to invalidate stale delayed-emit closures when the
// user releases before grace elapses (or rapidly retaps).
static PRESS_GENERATION: AtomicU32 = AtomicU32::new(0);

// Cached AppHandle so the hook callback can emit events.
static APP_HANDLE: Mutex<Option<AppHandle>> = Mutex::new(None);

/// Install (or re-install) the Space keyboard hook. Idempotent.
///
/// `hold_threshold_ms` must match the JS-side `cfg.hold_threshold_ms` so Rust
/// and JS agree on what counts as a "long" press.
pub fn install(app: AppHandle, hold_threshold_ms: i64) {
    HOLD_THRESHOLD_MS.store(hold_threshold_ms.max(0), Ordering::Relaxed);
    {
        let mut slot = APP_HANDLE.lock().expect("app handle mutex poisoned");
        *slot = Some(app);
    }
    if INSTALLED.swap(true, Ordering::SeqCst) {
        return; // already running
    }

    std::thread::Builder::new()
        .name("lucid-space-hook".into())
        .spawn(|| unsafe {
            HOOK_THREAD_ID.store(GetCurrentThreadId(), Ordering::SeqCst);
            let hmod = GetModuleHandleW(None).unwrap_or_default();
            let hook = match SetWindowsHookExW(
                WH_KEYBOARD_LL,
                Some(low_level_keyboard_proc),
                hmod,
                0,
            ) {
                Ok(h) => h,
                Err(e) => {
                    log::error!("SetWindowsHookExW(WH_KEYBOARD_LL) failed: {e}");
                    INSTALLED.store(false, Ordering::SeqCst);
                    return;
                }
            };
            HOOK_HANDLE.store(hook.0 as usize, Ordering::SeqCst);

            // Standard message pump — the LL hook needs an active pump on the
            // thread that installed it, otherwise Windows drops the hook.
            let mut msg = MSG::default();
            loop {
                let r = GetMessageW(&mut msg, None, 0, 0);
                if r.0 <= 0 { break; } // WM_QUIT or error
                let _ = TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }
            let _ = UnhookWindowsHookEx(hook);
            HOOK_HANDLE.store(0, Ordering::SeqCst);
            INSTALLED.store(false, Ordering::SeqCst);
        })
        .expect("failed to spawn lucid-space-hook thread");
}

/// Tear the hook down. Safe to call when not installed.
pub fn uninstall() {
    if !INSTALLED.load(Ordering::SeqCst) {
        return;
    }
    let tid = HOOK_THREAD_ID.load(Ordering::SeqCst);
    if tid != 0 {
        unsafe {
            // Wake the message pump so it sees we want to quit.
            let _ = PostThreadMessageW(tid, WM_QUIT, WPARAM(0), LPARAM(0));
        }
    }
}

/// Update the hold threshold without re-installing the hook.
#[allow(dead_code)]
pub fn set_hold_threshold_ms(ms: i64) {
    HOLD_THRESHOLD_MS.store(ms.max(0), Ordering::Relaxed);
}

// ---------------------------------------------------------------------------
// Hook callback (runs on the lucid-space-hook thread).
// ---------------------------------------------------------------------------

unsafe extern "system" fn low_level_keyboard_proc(
    n_code: i32,
    w_param: WPARAM,
    l_param: LPARAM,
) -> LRESULT {
    if n_code < 0 {
        return CallNextHookEx(None, n_code, w_param, l_param);
    }
    let kbd = &*(l_param.0 as *const KBDLLHOOKSTRUCT);
    let vk = VIRTUAL_KEY(kbd.vkCode as u16);
    if vk != VK_SPACE {
        return CallNextHookEx(None, n_code, w_param, l_param);
    }
    // Ignore our own synthetic events (replays).
    if (kbd.flags.0 & LLKHF_INJECTED.0) != 0 || kbd.dwExtraInfo == SYNTH_TAG {
        return CallNextHookEx(None, n_code, w_param, l_param);
    }

    let msg = w_param.0 as u32;
    match msg {
        WM_KEYDOWN | WM_SYSKEYDOWN => on_space_down(),
        WM_KEYUP | WM_SYSKEYUP => on_space_up(),
        _ => return CallNextHookEx(None, n_code, w_param, l_param),
    }
    // Swallow the original keystroke. We re-inject if needed.
    LRESULT(1)
}

fn on_space_down() {
    // Windows repeats KEYDOWN while held. Only act on the first edge.
    if IS_HELD.swap(true, Ordering::SeqCst) {
        return;
    }
    let gen = PRESS_GENERATION.fetch_add(1, Ordering::SeqCst).wrapping_add(1);
    PRESSED_EMITTED.store(false, Ordering::SeqCst);
    *PRESS_INSTANT.lock().expect("press mutex poisoned") = Some(Instant::now());

    // Schedule a delayed `pressed` emit after the grace window. If the user
    // releases first, on_space_up cancels by bumping PRESS_GENERATION.
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(PRESSED_EMIT_GRACE_MS));
        if !IS_HELD.load(Ordering::SeqCst) {
            return; // released during grace — pure tap
        }
        if PRESS_GENERATION.load(Ordering::SeqCst) != gen {
            return; // a newer press superseded us
        }
        PRESSED_EMITTED.store(true, Ordering::SeqCst);
        emit_voice("pressed");
    });
}

fn on_space_up() {
    if !IS_HELD.swap(false, Ordering::SeqCst) {
        return; // spurious / never saw the down
    }
    // Invalidate any pending delayed-pressed.
    let _ = PRESS_GENERATION.fetch_add(1, Ordering::SeqCst);

    let press_at = PRESS_INSTANT.lock().expect("press mutex poisoned").take();
    let duration_ms = press_at
        .map(|t| t.elapsed().as_millis() as i64)
        .unwrap_or(0);
    let threshold = HOLD_THRESHOLD_MS.load(Ordering::Relaxed);
    let pressed_was_emitted = PRESSED_EMITTED.swap(false, Ordering::SeqCst);

    if duration_ms >= threshold && threshold > 0 {
        // Long hold — JS already entered recording. Just notify release.
        if pressed_was_emitted {
            emit_voice("released");
        } else {
            // Edge case: threshold < grace. Make sure JS sees both edges so
            // its state machine doesn't desync.
            emit_voice("pressed");
            emit_voice("released");
        }
        return;
    }

    // Short tap (or sub-threshold hold) — replay synthetic Space + cancel UI.
    if pressed_was_emitted {
        emit_voice("released");
    }
    replay_space_tap();
}

fn emit_voice(kind: &'static str) {
    let app_opt = APP_HANDLE.lock().expect("app handle mutex poisoned").clone();
    if let Some(app) = app_opt {
        let _ = app.emit(crate::voice::EVENT_VOICE, json!({"kind": kind}));
    }
}

fn replay_space_tap() {
    unsafe {
        let down = INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: VK_SPACE,
                    wScan: 0,
                    dwFlags: KEYBD_EVENT_FLAGS(0),
                    time: 0,
                    dwExtraInfo: SYNTH_TAG,
                },
            },
        };
        let up = INPUT {
            r#type: INPUT_KEYBOARD,
            Anonymous: INPUT_0 {
                ki: KEYBDINPUT {
                    wVk: VK_SPACE,
                    wScan: 0,
                    dwFlags: KEYEVENTF_KEYUP,
                    time: 0,
                    dwExtraInfo: SYNTH_TAG,
                },
            },
        };
        let inputs = [down, up];
        let n = SendInput(&inputs, std::mem::size_of::<INPUT>() as i32);
        if n != inputs.len() as u32 {
            log::warn!("SendInput replay sent {n}/{} events", inputs.len());
        }
    }
}
