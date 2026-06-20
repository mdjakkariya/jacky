// Jack — floating orb shell. A thin Tauri window that loads ui/orb/index.html
// (the live orb client) and applies the macOS "ambient presence" window behavior.
// All intelligence lives in the Python daemon; this process only renders.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::TrayIconBuilder,
    LogicalSize, Manager, WebviewUrl, WebviewWindowBuilder,
};

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
    let builder = tauri::Builder::default();

    // The NSPanel plugin (macOS) lets us float over full-screen apps.
    #[cfg(target_os = "macos")]
    let builder = builder.plugin(tauri_nspanel::init());

    builder
        .setup(|app| {
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
            // current state, plus a Size submenu and Quit.
            let view = MenuItem::with_id(app, "view", HIDE, true, None::<&str>)?;
            let lock = MenuItem::with_id(app, "lock", MOVABLE, true, None::<&str>)?;

            let small = MenuItem::with_id(app, "size_s", "Small", true, None::<&str>)?;
            let medium = MenuItem::with_id(app, "size_m", "Medium", true, None::<&str>)?;
            let large = MenuItem::with_id(app, "size_l", "Large", true, None::<&str>)?;
            let size = Submenu::with_items(app, "Size", true, &[&small, &medium, &large])?;

            let settings = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Jack", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&view, &lock, &size, &settings, &quit])?;

            let visible = Arc::new(AtomicBool::new(true));
            let locked = Arc::new(AtomicBool::new(false));
            let view_item = view.clone();
            let lock_item = lock.clone();

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
                    "settings" => open_settings(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Jack orb");
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
fn open_settings(app: &tauri::AppHandle) {
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);

    if let Some(win) = app.get_webview_window("settings") {
        let _ = win.show();
        let _ = win.set_focus();
        return;
    }

    let handle = app.clone();
    match WebviewWindowBuilder::new(app, "settings", WebviewUrl::App("settings.html".into()))
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
