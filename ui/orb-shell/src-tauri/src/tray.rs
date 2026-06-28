//! Menu-bar (tray) control surface + the standard Edit menu. `build_tray` wires the
//! menu and its event handler; it returns the lock state + item so the ⌘⌃L global
//! shortcut (registered in `shortcuts`) can keep the menu label in sync.
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::TrayIconBuilder;
use tauri::Wry;

// Orb size presets (square, logical px). The web orb scales to fill the window.
const SIZE_SMALL: f64 = 150.0;
const SIZE_MEDIUM: f64 = 220.0;
const SIZE_LARGE: f64 = 300.0;

// Labels for the two toggles, so the menu always reflects the current state.
const SHOW: &str = "Show orb";
const HIDE: &str = "Hide orb";
pub(crate) const MOVABLE: &str = "Movable";
pub(crate) const LOCKED: &str = "Locked";

/// Install a minimal app menu whose Edit items give text fields their standard
/// keyboard editing (typing relies on the window being key; Cut/Copy/Paste/
/// Select-All rely on these menu items' ⌘ key equivalents).
pub(crate) fn install_edit_menu(app: &tauri::App) -> tauri::Result<()> {
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

/// Build the menu-bar tray and its event handler. Returns `(locked, lock_item)` so the
/// ⌘⌃L global shortcut can toggle the same state and keep the label in sync.
///
/// Two toggles whose labels show the current state, plus a Size submenu and Quit. The
/// accelerator field makes macOS render each shortcut right-aligned and greyed (native
/// look). On a tray/status-item menu these key-equivalents are display-only — they fire
/// only while the menu is open — so the GLOBAL hotkeys registered separately (same as
/// summon) are the real, always-on handlers. The combos match.
pub(crate) fn build_tray(app: &tauri::App) -> tauri::Result<(Arc<AtomicBool>, MenuItem<Wry>)> {
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
    let about = MenuItem::with_id(app, "about", "About Jack", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit Jack", true, Some("Command+Control+Q"))?;

    // Dev builds get a "Clean up storage" item at the top for quick resets.
    #[cfg(debug_assertions)]
    let cleanup =
        MenuItem::with_id(app, "cleanup", "Clean up storage (dev)", true, None::<&str>)?;
    #[cfg(debug_assertions)]
    let menu = Menu::with_items(
        app,
        &[&cleanup, &chat, &voice, &view, &lock, &size, &settings, &report, &about, &quit],
    )?;
    #[cfg(not(debug_assertions))]
    let menu = Menu::with_items(
        app,
        &[&chat, &voice, &view, &lock, &size, &settings, &report, &about, &quit],
    )?;

    let visible = Arc::new(AtomicBool::new(false)); // launch hidden (chat-first)
    let locked = Arc::new(AtomicBool::new(false));
    let view_item = view.clone();
    let lock_item = lock.clone();
    // Clones returned for the global ⌘⌃L handler (the tray closure moves the originals).
    let locked_ret = locked.clone();
    let lock_item_ret = lock.clone();

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
                if now {
                    crate::windows::surface_orb(app); // show + re-sync the orb's web side
                } else {
                    crate::windows::with_orb(app, |w| {
                        let _ = w.hide();
                    });
                }
                let _ = view_item.set_text(if now { HIDE } else { SHOW });
            }
            "lock" => {
                let now = !locked.load(Ordering::Relaxed);
                locked.store(now, Ordering::Relaxed);
                crate::windows::with_orb(app, |w| {
                    let _ = w.set_ignore_cursor_events(now);
                });
                let _ = lock_item.set_text(if now { LOCKED } else { MOVABLE });
            }
            "size_s" => crate::windows::resize(app, SIZE_SMALL),
            "size_m" => crate::windows::resize(app, SIZE_MEDIUM),
            "size_l" => crate::windows::resize(app, SIZE_LARGE),
            "chat" => crate::windows::open_chat(app.clone()),
            "voice" => crate::windows::request_voice(app),
            "settings" => crate::windows::open_settings(app, ""),
            "report" => crate::windows::open_settings(app, "report"),
            "about" => crate::windows::open_about(app),
            #[cfg(debug_assertions)]
            "cleanup" => eprintln!("[jack] {}", crate::commands::cleanup_storage(app)),
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;

    Ok((locked_ret, lock_item_ret))
}
