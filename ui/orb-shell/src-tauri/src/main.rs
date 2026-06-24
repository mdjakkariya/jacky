// Jack — floating orb shell. A thin Tauri window that loads ui/orb/index.html
// (the live orb client) and applies the macOS "ambient presence" window behavior.
// All intelligence lives in the Python daemon; this process only renders.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::TrayIconBuilder,
    LogicalSize, Manager, WebviewUrl, WebviewWindowBuilder,
};
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

/// Holds the spawned engine sidecar so we can stop it when the app quits.
struct Engine(Mutex<Option<CommandChild>>);

/// The surface the user last had open: `true` = chat drawer, `false` = voice orb.
/// The ⌘⌃J summon toggle restores THIS (persisting the mode) instead of switching
/// chat⇄voice — and since the orb only becomes the last surface once voice is enabled
/// (a gated action), the toggle never surfaces a useless orb on a fresh, chat-only app.
struct LastSurface(AtomicBool);

fn set_last_surface(app: &tauri::AppHandle, chat: bool) {
    if let Some(s) = app.try_state::<LastSurface>() {
        s.0.store(chat, Ordering::Relaxed);
    }
}

/// Start the bundled `autobot-daemon` sidecar; the orb connects to it over the
/// local WebSocket. Logs (but doesn't crash) if it can't start.
#[allow(dead_code)] // only called in release builds (see setup)
fn start_engine(app: &tauri::AppHandle) {
    app.manage(Engine(Mutex::new(None)));
    let sidecar = app.shell().sidecar("autobot-daemon").map(|cmd| {
        // Tell the engine our PID so its watchdog can exit if we (the orb) go away.
        // PyInstaller's onefile bootloader orphans the real Python child when we
        // kill the sidecar, so this self-shutdown is what actually frees :8765 on
        // quit / force-quit / crash. (We still kill() on a clean exit, below.)
        let cmd = cmd.env("AUTOBOT_PARENT_PID", std::process::id().to_string());
        // Point the engine at the app's bundled voices, so a fresh install can
        // seed a default Piper voice and speak immediately.
        match app.path().resource_dir() {
            Ok(dir) => cmd.env("AUTOBOT_VOICE_DIR", dir.join("voices")),
            Err(_) => cmd,
        }
    });
    match sidecar {
        Ok(cmd) => match cmd.spawn() {
            Ok((_rx, child)) => {
                *app.state::<Engine>().0.lock().unwrap() = Some(child);
                eprintln!("[jack] engine started (sidecar)");
            }
            Err(e) => eprintln!("[jack] failed to start engine: {e}"),
        },
        Err(e) => eprintln!("[jack] engine sidecar not found: {e}"),
    }
}

/// Stop the engine sidecar on app exit so it doesn't linger.
fn stop_engine(app: &tauri::AppHandle) {
    if let Some(engine) = app.try_state::<Engine>() {
        if let Some(child) = engine.0.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
}

/// Open the Settings window — callable from the orb (e.g. the first-run wizard).
#[tauri::command]
fn open_settings_window(app: tauri::AppHandle) {
    open_settings(&app, "");
}

/// Open Settings on the Listening tab's voice-download section — used when the user
/// picks Voice but the speech models aren't downloaded yet.
#[tauri::command]
fn open_settings_voice(app: tauri::AppHandle) {
    open_settings(&app, "voice");
}

/// Open (or focus) the chat drawer — a right-docked panel for typed chat mode.
///
/// It's a *non-activating* panel (see `make_chat_panel`): it can become key to
/// receive typing, but never activates Jack as the front app. That's deliberate —
/// activating a normal app while you're in another app's full-screen Space makes
/// macOS jump you to the Desktop Space, which is exactly the bug we're avoiding.
#[tauri::command]
fn open_chat(app: tauri::AppHandle) {
    set_last_surface(&app, true);  // chat is now the surface to restore on summon
    // Chat and voice are one-at-a-time: hide the orb while the chat drawer is open
    // (close_chat shows it again), so they never overlap.
    with_orb(&app, |w| {
        let _ = w.hide();
    });
    if let Some(win) = app.get_webview_window("chat") {
        // Don't call set_visible_on_all_workspaces here — it resets the collection
        // behaviour and would strip the full-screen-auxiliary bit the panel needs.
        // The panel keeps its behaviour across hide/show, so just bring it forward.
        let _ = win.show();
        let _ = win.set_focus();
        return;
    }
    let width = 380.0;
    let height = 640.0;
    let handle = app.clone();
    match WebviewWindowBuilder::new(&app, "chat", WebviewUrl::App("chat.html".into()))
        .title("Jack — Chat")
        .inner_size(width, height)
        .resizable(true)
        .transparent(true)
        .decorations(false)
        .always_on_top(true)
        .build()
    {
        Ok(win) => {
            // Dock to the right edge of the primary monitor.
            if let Ok(Some(mon)) = app.primary_monitor() {
                let size = mon.size().to_logical::<f64>(mon.scale_factor());
                let _ = win.set_size(tauri::LogicalSize::new(width, size.height - 96.0));
                let _ = win.set_position(tauri::LogicalPosition::new(size.width - width - 16.0, 48.0));
            }
            // Float over other apps' full-screen Spaces too, Spotlight-style, while
            // staying typeable — without yanking you to the Desktop Space.
            #[cfg(target_os = "macos")]
            make_chat_panel(&win);
            let _ = win.set_focus();
            win.on_window_event(move |event| {
                if matches!(
                    event,
                    tauri::WindowEvent::CloseRequested { .. } | tauri::WindowEvent::Destroyed
                ) {
                    #[cfg(target_os = "macos")]
                    let _ = handle.set_activation_policy(tauri::ActivationPolicy::Accessory);
                }
            });
        }
        Err(e) => eprintln!("[jack] failed to open chat: {e}"),
    }
}

/// Hide the chat drawer, bring the orb back, and return to the background presence.
#[tauri::command]
fn close_chat(app: tauri::AppHandle) {
    set_last_surface(&app, false);  // voice/orb is now the surface to restore on summon
    if let Some(win) = app.get_webview_window("chat") {
        let _ = win.hide();
    }
    // Switching back to voice: the orb must reappear (it was hidden for chat).
    with_orb(&app, |w| {
        let _ = w.show();
    });
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
}

/// Hide the chat drawer WITHOUT bringing the orb back — used when voice isn't set up
/// yet, so closing chat doesn't surface a useless orb (no voice models downloaded).
#[tauri::command]
fn hide_chat(app: tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("chat") {
        let _ = win.hide();
    }
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
}

/// Switch to voice (⌘⌃V). Reuses the chat UI's gated flow: if the voice models are
/// present it enables voice and shows the orb; if not, it opens the download in
/// Settings. The chat window always exists (created at launch), so we drive its JS;
/// if it somehow doesn't, fall back to opening the Settings voice section directly.
fn request_voice(app: &tauri::AppHandle) {
    if let Some(chat) = app.get_webview_window("chat") {
        let _ = chat.eval("window.__enableVoice && window.__enableVoice()");
    } else {
        open_settings(app, "voice");
    }
}

/// Open a URL in the user's default browser (e.g. the prefilled GitHub issue form).
/// Constrained to https links so it can only ever open the web, never run anything.
#[tauri::command]
fn open_external(url: String) {
    if !url.starts_with("https://") {
        return;
    }
    #[cfg(target_os = "macos")]
    {
        let _ = std::process::Command::new("open").arg(&url).spawn();
    }
}

/// Reveal a file in Finder (selects it in its folder), so the user can copy/share it.
#[tauri::command]
fn reveal_in_finder(path: String) {
    #[cfg(target_os = "macos")]
    {
        let _ = std::process::Command::new("open").arg("-R").arg(&path).spawn();
    }
}

/// Dev only: wipe debug artifacts under ~/.autobot (logs, sessions, reports) for a
/// fresh start. Leaves settings, secrets, voices, and memory untouched. The running
/// engine keeps writing to its already-open log until it's restarted.
#[cfg(debug_assertions)]
fn cleanup_storage(app: &tauri::AppHandle) -> String {
    let home = match app.path().home_dir() {
        Ok(h) => h,
        Err(_) => return "couldn't resolve home dir".into(),
    };
    let base = home.join(".autobot");
    let mut removed = 0u32;
    for sub in ["logs", "sessions", "reports"] {
        if let Ok(entries) = std::fs::read_dir(base.join(sub)) {
            for entry in entries.flatten() {
                if std::fs::remove_file(entry.path()).is_ok() {
                    removed += 1;
                }
            }
        }
    }
    format!("cleaned up {removed} file(s) under ~/.autobot (restart the engine for a fresh log)")
}

/// Copy text to the system clipboard. The webview's navigator.clipboard is
/// unreliable under Tauri's custom protocol, so we write it natively (pbcopy).
/// Returns whether it succeeded so the UI can confirm honestly.
#[tauri::command]
fn copy_to_clipboard(text: String) -> bool {
    #[cfg(target_os = "macos")]
    {
        use std::io::Write;
        use std::process::{Command, Stdio};
        if let Ok(mut child) = Command::new("pbcopy").stdin(Stdio::piped()).spawn() {
            if let Some(mut stdin) = child.stdin.take() {
                if stdin.write_all(text.as_bytes()).is_err() {
                    return false;
                }
            }
            return child.wait().map(|s| s.success()).unwrap_or(false);
        }
    }
    false
}

// Orb size presets (square, logical px). The web orb scales to fill the window.
const SIZE_SMALL: f64 = 150.0;
const SIZE_MEDIUM: f64 = 220.0;
const SIZE_LARGE: f64 = 300.0;

// Labels for the two toggles, so the menu always reflects the current state.
const SHOW: &str = "Show orb";
const HIDE: &str = "Hide orb";
const MOVABLE: &str = "Movable";
const LOCKED: &str = "Locked";

fn main() {
    // The shell plugin lets us launch the bundled engine sidecar.
    let builder = tauri::Builder::default().plugin(tauri_plugin_shell::init());

    // The NSPanel plugin (macOS) lets us float over full-screen apps.
    #[cfg(target_os = "macos")]
    let builder = builder.plugin(tauri_nspanel::init());

    // Global hotkey (⌘⇧J) to summon/dismiss Jack from any app. The actual binding
    // is registered in setup(); this just installs the plugin.
    let builder = builder.plugin(tauri_plugin_global_shortcut::Builder::new().build());

    builder
        .invoke_handler(tauri::generate_handler![
            open_settings_window,
            open_settings_voice,
            open_external,
            reveal_in_finder,
            copy_to_clipboard,
            open_chat,
            close_chat,
            hide_chat
        ])
        .setup(|app| {
            // Track which surface to restore on summon; chat is the launch surface.
            app.manage(LastSurface(AtomicBool::new(true)));
            // Bundled release: launch the embedded engine (the orb is its UI client).
            // In dev (`cargo tauri dev`) run the engine separately with `make run`,
            // so you can iterate on it and the orb won't double-spawn it on :8765.
            #[cfg(not(debug_assertions))]
            start_engine(app.handle());

            // On macOS, run as an "accessory" so the orb has no Dock icon and
            // never appears in the ⌘-Tab switcher — it's a presence, not an app.
            #[cfg(target_os = "macos")]
            app.set_activation_policy(tauri::ActivationPolicy::Accessory);

            // App menu with a standard Edit menu — REQUIRED for text editing in
            // the Settings webview: macOS routes typing/Cut/Copy/Paste/Select-All
            // (⌘X/C/V/A) through these items' key equivalents. Without it the
            // fields can't be edited or pasted into.
            install_edit_menu(app)?;

            if let Some(orb) = app.get_webview_window("orb") {
                // macOS: become a non-activating panel so we sit over full-screen.
                #[cfg(target_os = "macos")]
                make_floating_panel(&orb);
                // Other platforms: a plain always-on-top window is the best we do.
                #[cfg(not(target_os = "macos"))]
                {
                    let _ = orb.set_always_on_top(true);
                }
            }

            // Menu-bar (tray) control surface. Two toggles whose labels show the
            // current state, plus a Size submenu and Quit. The accelerator field makes
            // macOS render each shortcut right-aligned and greyed (native look). On a
            // tray/status-item menu these key-equivalents are display-only — they fire
            // only while the menu is open — so the GLOBAL hotkeys registered after the
            // tray (same as summon) are the real, always-on handlers. The combos match.
            let view = MenuItem::with_id(app, "view", SHOW, true, Some("Command+Control+J"))?;
            let lock = MenuItem::with_id(app, "lock", MOVABLE, true, Some("Command+Control+L"))?;

            let small = MenuItem::with_id(app, "size_s", "Small", true, None::<&str>)?;
            let medium = MenuItem::with_id(app, "size_m", "Medium", true, None::<&str>)?;
            let large = MenuItem::with_id(app, "size_l", "Large", true, None::<&str>)?;
            let size = Submenu::with_items(app, "Size", true, &[&small, &medium, &large])?;

            let chat = MenuItem::with_id(app, "chat", "Chat…", true, Some("Command+Control+C"))?;
            let voice = MenuItem::with_id(app, "voice", "Voice…", true, Some("Command+Control+V"))?;
            let settings =
                MenuItem::with_id(app, "settings", "Settings…", true, Some("Command+Control+S"))?;
            let report =
                MenuItem::with_id(app, "report", "Report an issue…", true, Some("Command+Control+R"))?;
            let quit = MenuItem::with_id(app, "quit", "Quit Jack", true, Some("Command+Control+Q"))?;

            // Dev builds get a "Clean up storage" item at the top for quick resets.
            #[cfg(debug_assertions)]
            let cleanup =
                MenuItem::with_id(app, "cleanup", "Clean up storage (dev)", true, None::<&str>)?;
            #[cfg(debug_assertions)]
            let menu = Menu::with_items(
                app,
                &[&cleanup, &chat, &voice, &view, &lock, &size, &settings, &report, &quit],
            )?;
            #[cfg(not(debug_assertions))]
            let menu = Menu::with_items(
                app,
                &[&chat, &voice, &view, &lock, &size, &settings, &report, &quit],
            )?;

            let visible = Arc::new(AtomicBool::new(false));  // launch hidden (chat-first)
            let locked = Arc::new(AtomicBool::new(false));
            let view_item = view.clone();
            let lock_item = lock.clone();
            // Clones for the global ⌘⌃L handler (the tray closure moves the originals).
            let locked_sc = locked.clone();
            let lock_item_sc = lock.clone();

            let icon = app
                .default_window_icon()
                .cloned()
                .expect("bundle icon configured in tauri.conf.json");

            TrayIconBuilder::with_id("jack-tray")
                .icon(icon)
                .tooltip("Jack")
                .menu(&menu)
                .on_menu_event(move |app, event| match event.id.as_ref() {
                    "view" => {
                        let now = !visible.load(Ordering::Relaxed);
                        visible.store(now, Ordering::Relaxed);
                        with_orb(app, |w| {
                            let _ = if now { w.show() } else { w.hide() };
                        });
                        let _ = view_item.set_text(if now { HIDE } else { SHOW });
                    }
                    "lock" => {
                        let now = !locked.load(Ordering::Relaxed);
                        locked.store(now, Ordering::Relaxed);
                        with_orb(app, |w| {
                            let _ = w.set_ignore_cursor_events(now);
                        });
                        let _ = lock_item.set_text(if now { LOCKED } else { MOVABLE });
                    }
                    "size_s" => resize(app, SIZE_SMALL),
                    "size_m" => resize(app, SIZE_MEDIUM),
                    "size_l" => resize(app, SIZE_LARGE),
                    "chat" => open_chat(app.clone()),
                    "voice" => request_voice(app),
                    "settings" => open_settings(app, ""),
                    "report" => open_settings(app, "report"),
                    #[cfg(debug_assertions)]
                    "cleanup" => eprintln!("[jack] {}", cleanup_storage(app)),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;

            // Global hotkeys for every option (an accessory app has no menu bar, so
            // menu key-equivalents only fire while the menu is open — these work from
            // anywhere, like summon). All in the ⌘⌃ namespace so they never shadow an
            // app-local ⌘-letter binding. Registered in Rust → no JS capability.
            {
                use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

                // Non-fatal: if a combo is already taken system-wide, log it and keep
                // going — a hotkey clash must never stop Jack from launching.
                macro_rules! reg {
                    ($combo:expr, $handler:expr) => {
                        if let Err(e) = app.global_shortcut().on_shortcut($combo, $handler) {
                            eprintln!("[jack] global shortcut {} unavailable: {e}", $combo);
                        }
                    };
                }

                reg!("Command+Control+J", |app, _s, e| {
                    if e.state() == ShortcutState::Pressed {
                        toggle_jack(app);
                    }
                });
                reg!("Command+Control+C", |app, _s, e| {
                    if e.state() == ShortcutState::Pressed {
                        open_chat(app.clone());
                    }
                });
                reg!("Command+Control+V", |app, _s, e| {
                    if e.state() == ShortcutState::Pressed {
                        request_voice(app);
                    }
                });
                reg!("Command+Control+S", |app, _s, e| {
                    if e.state() == ShortcutState::Pressed {
                        open_settings(app, "");
                    }
                });
                reg!("Command+Control+R", |app, _s, e| {
                    if e.state() == ShortcutState::Pressed {
                        open_settings(app, "report");
                    }
                });
                reg!("Command+Control+Q", |app, _s, e| {
                    if e.state() == ShortcutState::Pressed {
                        app.exit(0);
                    }
                });
                reg!("Command+Control+L", move |app, _s, e| {
                    if e.state() == ShortcutState::Pressed {
                        let now = !locked_sc.load(Ordering::Relaxed);
                        locked_sc.store(now, Ordering::Relaxed);
                        with_orb(app, |w| {
                            let _ = w.set_ignore_cursor_events(now);
                        });
                        let _ = lock_item_sc.set_text(if now { LOCKED } else { MOVABLE });
                    }
                });
            }

            // Launch into chat: the engine defaults to chat mode (no mic, no voice
            // models needed), so open the chat drawer and leave the orb hidden. The
            // orb only appears once the user switches to voice (close_chat shows it),
            // which the chat UI gates on the voice models being downloaded.
            open_chat(app.handle().clone());

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building Jack orb")
        .run(|app, event| {
            // Stop the engine sidecar when the app quits, so it doesn't linger.
            if let tauri::RunEvent::Exit = event {
                stop_engine(app);
            }
        });
}

/// Bring the app to the foreground so its windows can become key (and text
/// fields editable). An accessory app stays inactive otherwise.
#[cfg(target_os = "macos")]
fn activate_app() {
    use objc::runtime::{Object, YES};
    use objc::{class, msg_send, sel, sel_impl};
    unsafe {
        let ns_app: *mut Object = msg_send![class!(NSApplication), sharedApplication];
        let _: () = msg_send![ns_app, activateIgnoringOtherApps: YES];
    }
}

/// Install a minimal app menu whose Edit items give text fields their standard
/// keyboard editing (typing relies on the window being key; Cut/Copy/Paste/
/// Select-All rely on these menu items' ⌘ key equivalents).
fn install_edit_menu(app: &tauri::App) -> tauri::Result<()> {
    let edit = Submenu::with_items(
        app,
        "Edit",
        true,
        &[
            &PredefinedMenuItem::undo(app, None)?,
            &PredefinedMenuItem::redo(app, None)?,
            &PredefinedMenuItem::separator(app)?,
            &PredefinedMenuItem::cut(app, None)?,
            &PredefinedMenuItem::copy(app, None)?,
            &PredefinedMenuItem::paste(app, None)?,
            &PredefinedMenuItem::select_all(app, None)?,
        ],
    )?;
    let menu = Menu::with_items(app, &[&edit])?;
    app.set_menu(menu)?;
    Ok(())
}

/// Open (or focus) the Settings window — a normal, focusable window.
///
/// The app normally runs as a macOS *accessory* (no Dock icon, non-activating),
/// which means its windows can't receive keyboard focus — you couldn't type in
/// the API-key field. So while Settings is open we switch to the regular
/// activation policy (the app activates, fields are typable, a Dock icon appears)
/// and switch back to accessory when the window closes.
fn open_settings(app: &tauri::AppHandle, target: &str) {
    // `target`: "" (default Model tab), "report" (open the debug-report sheet), or
    // "voice" (jump to the Listening tab's voice-download section).
    // Become a regular app AND actually activate it — without activating, no
    // window can become "key", so text fields can't take focus or accept input.
    #[cfg(target_os = "macos")]
    {
        let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
        activate_app();
    }

    if let Some(win) = app.get_webview_window("settings") {
        let _ = win.show();
        let _ = win.set_focus();
        // Already open: ask the webview to jump to the requested place.
        match target {
            "report" => {
                let _ = win.eval("window.__openReport && window.__openReport()");
            }
            "voice" => {
                let _ = win.eval("window.__openVoice && window.__openVoice()");
            }
            _ => {}
        }
        return;
    }

    // Fresh window: a #hash the page checks on load (#report sheet / #voice tab).
    let url = if target.is_empty() {
        "settings.html".to_string()
    } else {
        format!("settings.html#{target}")
    };
    let handle = app.clone();
    match WebviewWindowBuilder::new(app, "settings", WebviewUrl::App(url.into()))
        .title("Jack — Settings")
        .inner_size(580.0, 680.0)
        .resizable(true)
        .build()
    {
        Ok(win) => {
            let _ = win.set_focus();
            // on_window_event lives on the built window (not the builder) in v2.
            win.on_window_event(move |event| {
                if matches!(
                    event,
                    tauri::WindowEvent::CloseRequested { .. } | tauri::WindowEvent::Destroyed
                ) {
                    // Back to a background presence once Settings is closed.
                    #[cfg(target_os = "macos")]
                    let _ = handle.set_activation_policy(tauri::ActivationPolicy::Accessory);
                }
            });
        }
        Err(e) => eprintln!("[jack] failed to open Settings: {e}"),
    }
}

/// Run `f` against the orb window if it exists.
fn with_orb(app: &tauri::AppHandle, f: impl FnOnce(&tauri::WebviewWindow)) {
    if let Some(window) = app.get_webview_window("orb") {
        f(&window);
    }
}

/// Summon/dismiss Jack (the ⌘⌃J global hotkey). It toggles the *visibility* of the
/// current surface and, when summoning from hidden, restores whatever was last shown
/// (chat or voice) — it does NOT switch modes. So pressing it in chat hides/shows the
/// chat drawer, and it only ever shows the orb if the user had already enabled voice
/// (the orb is never surfaced on a fresh, chat-only app).
fn toggle_jack(app: &tauri::AppHandle) {
    let chat_visible = app
        .get_webview_window("chat")
        .map(|w| w.is_visible().unwrap_or(false))
        .unwrap_or(false);
    if chat_visible {
        hide_chat(app.clone()); // dismiss the drawer — do NOT surface the orb
        return;
    }
    let orb_visible = app
        .get_webview_window("orb")
        .map(|w| w.is_visible().unwrap_or(false))
        .unwrap_or(false);
    if orb_visible {
        with_orb(app, |w| {
            let _ = w.hide();
        });
        return;
    }
    // Both hidden — summon the surface that was last shown (persisted; no mode switch).
    let chat_last = app
        .try_state::<LastSurface>()
        .map(|s| s.0.load(Ordering::Relaxed))
        .unwrap_or(true);
    if chat_last {
        open_chat(app.clone());
    } else {
        with_orb(app, |w| {
            let _ = w.show();
            let _ = w.set_always_on_top(true);
        });
    }
}

/// Resize the orb window to a square `px`; the web orb scales to fill it.
fn resize(app: &tauri::AppHandle, px: f64) {
    with_orb(app, |w| {
        let _ = w.set_size(LogicalSize::new(px, px));
    });
}

/// macOS: convert the orb window into a non-activating `NSPanel` that floats over
/// other apps' full-screen Spaces.
///
/// A plain `NSWindow` cannot be drawn over another app's full-screen window at
/// any level (confirmed: level 101 tops normal windows but not full-screen). The
/// working recipe — used by Spotlight-style overlays — is a panel with the
/// non-activating style mask plus `canJoinAllSpaces | fullScreenAuxiliary`.
#[cfg(target_os = "macos")]
#[allow(deprecated)] // tauri-nspanel re-exports the (now-deprecated) cocoa crate; still correct.
fn make_floating_panel(window: &tauri::WebviewWindow) {
    use tauri_nspanel::cocoa::appkit::NSWindowCollectionBehavior;
    use tauri_nspanel::WebviewWindowExt;

    // NSWindowStyleMaskNonactivatingPanel = 1 << 7; NSMainMenuWindowLevel = 24.
    const NS_NONACTIVATING_PANEL: i32 = 1 << 7;
    const NS_MAIN_MENU_WINDOW_LEVEL: i32 = 24;

    match window.to_panel() {
        Ok(panel) => {
            panel.set_level(NS_MAIN_MENU_WINDOW_LEVEL + 1);
            // Non-activating: showing the orb never steals focus from your editor.
            panel.set_style_mask(NS_NONACTIVATING_PANEL);
            panel.set_collection_behaviour(
                NSWindowCollectionBehavior::NSWindowCollectionBehaviorCanJoinAllSpaces
                    | NSWindowCollectionBehavior::NSWindowCollectionBehaviorStationary
                    | NSWindowCollectionBehavior::NSWindowCollectionBehaviorFullScreenAuxiliary,
            );
            panel.order_front_regardless();
            eprintln!("[jack] orb is now a floating NSPanel (over full-screen)");
        }
        Err(_) => eprintln!("[jack] to_panel() failed — orb stays a normal window"),
    }
}

/// macOS: make the chat drawer float over other apps' full-screen Spaces while
/// still accepting keyboard input — the Spotlight/Alfred recipe.
///
/// A non-activating panel can become *key* (so you can type into it) WITHOUT
/// activating Jack as the front app. That matters because activating a normal app
/// over another app's full-screen window makes macOS switch to the Desktop Space —
/// which is why the chat used to appear on the wrong screen. `fullScreenAuxiliary`
/// + `canJoinAllSpaces` let it draw over the current full-screen Space instead.
/// We keep the resizable bit so the drawer can still be dragged-to-resize.
#[cfg(target_os = "macos")]
#[allow(deprecated)] // tauri-nspanel re-exports the (now-deprecated) cocoa crate; still correct.
fn make_chat_panel(window: &tauri::WebviewWindow) {
    use tauri_nspanel::cocoa::appkit::NSWindowCollectionBehavior;
    use tauri_nspanel::WebviewWindowExt;

    const NS_NONACTIVATING_PANEL: i32 = 1 << 7; // NSWindowStyleMaskNonactivatingPanel
    const NS_RESIZABLE: i32 = 1 << 3; // NSWindowStyleMaskResizable
    const NS_MAIN_MENU_WINDOW_LEVEL: i32 = 24;

    match window.to_panel() {
        Ok(panel) => {
            panel.set_level(NS_MAIN_MENU_WINDOW_LEVEL + 1);
            panel.set_style_mask(NS_NONACTIVATING_PANEL | NS_RESIZABLE);
            panel.set_collection_behaviour(
                NSWindowCollectionBehavior::NSWindowCollectionBehaviorCanJoinAllSpaces
                    | NSWindowCollectionBehavior::NSWindowCollectionBehaviorFullScreenAuxiliary,
            );
            panel.order_front_regardless();
            eprintln!("[jack] chat is now a floating key panel (over full-screen)");
        }
        Err(e) => eprintln!("[jack] chat to_panel() failed — stays a normal window: {e}"),
    }
}
