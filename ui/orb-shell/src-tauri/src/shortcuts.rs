//! Global hotkeys (⌘⌃ namespace) that work from any app. An accessory app has no menu
//! bar, so the tray's menu key-equivalents only fire while the menu is open — these are
//! the real, always-on handlers. Registered in Rust → no JS capability. `locked` +
//! `lock_item` are shared with the tray so ⌘⌃L keeps the menu label in sync.
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use tauri::menu::MenuItem;
use tauri::Wry;

pub(crate) fn register_shortcuts(app: &tauri::App, locked: Arc<AtomicBool>, lock_item: MenuItem<Wry>) {
    use tauri_plugin_global_shortcut::{GlobalShortcutExt, ShortcutState};

    // Non-fatal: if a combo is already taken system-wide, log it and keep going — a
    // hotkey clash must never stop Jack from launching.
    macro_rules! reg {
        ($combo:expr, $handler:expr) => {
            if let Err(e) = app.global_shortcut().on_shortcut($combo, $handler) {
                eprintln!("[jack] global shortcut {} unavailable: {e}", $combo);
            }
        };
    }

    reg!("Command+Control+J", |app, _s, e| {
        if e.state() == ShortcutState::Pressed {
            crate::windows::toggle_jack(app);
        }
    });
    reg!("Command+Control+C", |app, _s, e| {
        if e.state() == ShortcutState::Pressed {
            crate::windows::open_chat(app.clone());
        }
    });
    reg!("Command+Control+V", |app, _s, e| {
        if e.state() == ShortcutState::Pressed {
            crate::windows::request_voice(app);
        }
    });
    reg!("Command+Control+S", |app, _s, e| {
        if e.state() == ShortcutState::Pressed {
            crate::windows::open_settings(app, "");
        }
    });
    reg!("Command+Control+R", |app, _s, e| {
        if e.state() == ShortcutState::Pressed {
            crate::windows::open_settings(app, "report");
        }
    });
    reg!("Command+Control+Q", |app, _s, e| {
        if e.state() == ShortcutState::Pressed {
            app.exit(0);
        }
    });
    reg!("Command+Control+L", move |app, _s, e| {
        if e.state() == ShortcutState::Pressed {
            let now = !locked.load(Ordering::Relaxed);
            locked.store(now, Ordering::Relaxed);
            crate::windows::with_orb(app, |w| {
                let _ = w.set_ignore_cursor_events(now);
            });
            let _ = lock_item.set_text(if now { crate::tray::LOCKED } else { crate::tray::MOVABLE });
        }
    });
}
