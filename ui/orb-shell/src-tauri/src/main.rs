// Jack — floating orb shell. A thin Tauri process that renders the webview UIs (orb,
// chat drawer, settings, about) and applies the macOS "ambient presence" window
// behavior. All intelligence lives in the Python daemon; this process only renders +
// manages windows. The logic is split across sibling modules; this file is the entry
// point: plugins, the invoke handler, the setup orchestration, and the run loop.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod commands;
mod engine;
#[cfg(target_os = "macos")]
mod platform;
mod shortcuts;
mod tray;
mod windows;

use std::sync::atomic::AtomicBool;

use tauri::Manager;

fn main() {
    // The shell plugin lets us launch the bundled engine sidecar.
    let builder = tauri::Builder::default().plugin(tauri_plugin_shell::init());

    // The NSPanel plugin (macOS) lets us float over full-screen apps.
    #[cfg(target_os = "macos")]
    let builder = builder.plugin(tauri_nspanel::init());

    // Global hotkeys (⌘⌃…) to summon/control Jack from any app. The bindings are
    // registered in setup(); this just installs the plugin.
    let builder = builder.plugin(tauri_plugin_global_shortcut::Builder::new().build());

    builder
        .invoke_handler(tauri::generate_handler![
            windows::open_settings_window,
            windows::open_settings_voice,
            commands::app_version,
            commands::open_external,
            commands::reveal_in_finder,
            commands::pick_folder,
            commands::copy_to_clipboard,
            windows::open_chat,
            windows::close_chat,
            windows::hide_chat
        ])
        .setup(|app| {
            // Track which surface to restore on summon; chat is the launch surface.
            app.manage(windows::LastSurface(AtomicBool::new(true)));

            // Bundled release: launch the embedded engine (the orb is its UI client).
            // In dev (`cargo tauri dev`) run the engine separately with `make run`, so
            // you can iterate on it and the orb won't double-spawn it on :8765.
            #[cfg(not(debug_assertions))]
            engine::start_engine(app.handle());

            // On macOS, run as an "accessory" so the orb has no Dock icon and never
            // appears in the ⌘-Tab switcher — it's a presence, not an app.
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            // App menu with a standard Edit menu — REQUIRED for text editing in the
            // Settings webview (macOS routes ⌘X/C/V/A through these items).
            tray::install_edit_menu(app)?;

            if let Some(orb) = app.get_webview_window("orb") {
                // macOS: become a non-activating panel so we sit over full-screen.
                #[cfg(target_os = "macos")]
                crate::platform::make_floating_panel(&orb);
                // Other platforms: a plain always-on-top window is the best we do.
                #[cfg(not(target_os = "macos"))]
                {
                    let _ = orb.set_always_on_top(true);
                }
            }

            // Menu-bar tray (returns the lock state/item the ⌘⌃L shortcut shares), then
            // the always-on global hotkeys.
            let (locked, lock_item) = tray::build_tray(app)?;
            shortcuts::register_shortcuts(app, locked, lock_item);

            // Launch into chat: the engine defaults to chat mode (no mic, no voice
            // models needed), so open the chat drawer and leave the orb hidden. The orb
            // only appears once the user switches to voice (close_chat shows it), which
            // the chat UI gates on the voice models being downloaded.
            windows::open_chat(app.handle().clone());

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Jack orb")
        .run(|app, event| {
            // Stop the engine sidecar when the app quits, so it doesn't linger.
            if let tauri::RunEvent::Exit = event {
                engine::stop_engine(app);
            }
        });
}
