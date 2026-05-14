// lucid Tauri shell entry. See `sidecar.rs` for the bridge to the Python
// agent process.
mod sidecar;
mod voice;
#[cfg(windows)]
mod space_hook;

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager,
};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

/// Paint the Windows native title bar a specific RGB color
/// (DWMWA_CAPTION_COLOR, requires Win11 22H2+). Falls back gracefully on
/// older Windows / non-Windows builds.
#[tauri::command]
fn set_caption_color(window: tauri::Window, r: u8, g: u8, b: u8) -> Result<(), String> {
    #[cfg(windows)]
    {
        use windows::Win32::Foundation::{COLORREF, HWND};
        use windows::Win32::Graphics::Dwm::{
            DwmSetWindowAttribute, DWMWA_CAPTION_COLOR,
        };
        let hwnd = window.hwnd().map_err(|e| e.to_string())?;
        let hwnd = HWND(hwnd.0 as *mut _);
        // COLORREF is 0x00BBGGRR
        let color = COLORREF((b as u32) << 16 | (g as u32) << 8 | (r as u32));
        unsafe {
            DwmSetWindowAttribute(
                hwnd,
                DWMWA_CAPTION_COLOR,
                &color as *const _ as *const _,
                std::mem::size_of::<COLORREF>() as u32,
            )
            .map_err(|e| e.to_string())?;
        }
    }
    #[cfg(not(windows))]
    {
        let _ = (window, r, g, b);
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let _ = env_logger::try_init();

    // Default emergency hotkey: Ctrl+Alt+Esc (also documented in design.md §4.7)
    let emergency = Shortcut::new(
        Some(Modifiers::CONTROL | Modifiers::ALT),
        Code::Escape,
    );

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(move |app, shortcut, event| {
                    if shortcut == &emergency && event.state() == ShortcutState::Pressed {
                        log::warn!("emergency hotkey pressed; cancelling sidecar task");
                        let app_h = app.clone();
                        tauri::async_runtime::spawn(async move {
                            let _ = sidecar::instance()
                                .request("cancel", serde_json::json!({}))
                                .await;
                            let _ = app_h.emit(
                                sidecar::EVENT_SIDECAR,
                                serde_json::json!({"kind":"emergency_stop"}),
                            );
                        });
                    } else {
                        // Anything else might be the voice PTT shortcut — let
                        // voice.rs decide whether to forward the press/release
                        // to the frontend (matches against its current binding).
                        voice::forward_hotkey_event(app, shortcut, event.state());
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            sidecar::sidecar_start_task,
            sidecar::sidecar_cancel,
            sidecar::sidecar_get_status,
            sidecar::sidecar_ping,
            sidecar::list_runs,
            sidecar::read_run,
            sidecar::read_image_b64,
            sidecar::save_inbox_image,
            sidecar::read_attachment_b64,
            sidecar::read_settings,
            sidecar::write_settings,
            sidecar::run_selfcheck,
            sidecar::copilot_status,
            sidecar::copilot_login_begin,
            sidecar::copilot_login_poll,
            sidecar::copilot_logout,
            sidecar::reload_config,
            sidecar::thread_new,
            sidecar::thread_list,
            sidecar::thread_read,
            sidecar::thread_set_active,
            sidecar::thread_delete,
            sidecar::thread_read_image,
            sidecar::task_queue_list,
            sidecar::task_queue_remove,
            sidecar::task_queue_clear,
            sidecar::memory_read,
            sidecar::memory_write,
            sidecar::memory_append,
            sidecar::memory_clear,
            sidecar::tools_read,
            sidecar::tools_write,
            sidecar::tools_append,
            sidecar::tools_reset,
            sidecar::app_tips_list,
            sidecar::app_tips_read,
            sidecar::app_tips_write,
            sidecar::app_tips_append,
            sidecar::app_tips_reset,
            sidecar::app_tips_delete,
            sidecar::template_list,
            sidecar::template_add,
            sidecar::template_update,
            sidecar::template_delete,
            sidecar::skill_list,
            sidecar::skill_read,
            sidecar::skill_add,
            sidecar::skill_update,
            sidecar::skill_delete,
            sidecar::skill_install_url,
            sidecar::schedule_list,
            sidecar::schedule_add,
            sidecar::schedule_update,
            sidecar::schedule_delete,
            sidecar::schedule_run_now,
            sidecar::doze_status,
            sidecar::doze_run_now,
            sidecar::doze_clear_processed,
            sidecar::doze_outputs,
            sidecar::doze_delete_output,
            sidecar::installed_apps_list,
            set_caption_color,
            voice::sidecar_transcribe,
            voice::voice_status,
            voice::voice_unload,
            voice::voice_config,
            voice::voice_model_status,
            voice::voice_download_model,
            voice::voice_overlay_show,
            voice::voice_overlay_hide,
            voice::voice_overlay_set_state,
            voice::voice_overlay_set_passthrough,
            voice::voice_register_hotkey,
            voice::voice_unregister_hotkey,
        ])
        .setup(move |app| {
            // ---------- system tray ----------
            let show_i = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>)?;
            let hide_i = MenuItem::with_id(app, "hide", "隐藏到托盘", true, None::<&str>)?;
            let cancel_i = MenuItem::with_id(app, "cancel", "急停（取消任务）", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_i, &hide_i, &cancel_i, &quit_i])?;

            let _tray = TrayIconBuilder::new()
                .icon(app.default_window_icon().unwrap().clone())
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                    "hide" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.hide();
                        }
                    }
                    "cancel" => {
                        let app_h = app.clone();
                        tauri::async_runtime::spawn(async move {
                            let _ = sidecar::instance()
                                .request("cancel", serde_json::json!({}))
                                .await;
                            let _ = app_h.emit(
                                sidecar::EVENT_SIDECAR,
                                serde_json::json!({"kind":"emergency_stop"}),
                            );
                        });
                    }
                    "quit" => {
                        // Try graceful shutdown of sidecar then exit.
                        let app_h = app.clone();
                        tauri::async_runtime::spawn(async move {
                            let _ = sidecar::instance()
                                .request("shutdown", serde_json::json!({}))
                                .await;
                            app_h.exit(0);
                        });
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(w) = app.get_webview_window("main") {
                            if w.is_visible().unwrap_or(false) {
                                let _ = w.hide();
                            } else {
                                let _ = w.show();
                                let _ = w.set_focus();
                            }
                        }
                    }
                })
                .build(app)?;

            // ---------- register emergency hotkey ----------
            if let Err(e) = app.global_shortcut().register(emergency) {
                log::warn!("failed to register emergency hotkey: {e}");
            }

            // ---------- start sidecar supervisor ----------
            // Resolve (and seed if missing) the per-user config.toml, then publish
            // its absolute path via env so both the sidecar process and the
            // settings UI agree on the same file.
            let cfg_path = sidecar::ensure_user_config(&app.handle());
            std::env::set_var("LUCID_CONFIG", &cfg_path);
            log::info!("user config resolved at {}", cfg_path.display());
            sidecar::supervise(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            // Closing the main window should hide it to tray instead of quitting.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    let _ = window.hide();
                    api.prevent_close();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
