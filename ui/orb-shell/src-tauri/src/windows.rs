//! Window orchestration: opening/showing/hiding the orb, chat drawer, Settings, and
//! About windows, the summon toggle, and the window-facing `#[tauri::command]`s.
use std::sync::atomic::{AtomicBool, Ordering};

use tauri::{LogicalSize, Manager, WebviewUrl, WebviewWindowBuilder};

/// The surface the user last had open: `true` = chat drawer, `false` = voice orb.
/// The ⌘⌃J summon toggle restores THIS (persisting the mode) instead of switching
/// chat⇄voice — and since the orb only becomes the last surface once voice is enabled
/// (a gated action), the toggle never surfaces a useless orb on a fresh, chat-only app.
pub(crate) struct LastSurface(pub(crate) AtomicBool);

pub(crate) fn set_last_surface(app: &tauri::AppHandle, chat: bool) {
    if let Some(s) = app.try_state::<LastSurface>() {
        s.0.store(chat, Ordering::Relaxed);
    }
}

/// Open the Settings window — callable from the orb (e.g. the first-run wizard).
#[tauri::command]
pub(crate) fn open_settings_window(app: tauri::AppHandle) {
    open_settings(&app, "");
}

/// Open Settings on the Listening tab's voice-download section — used when the user
/// picks Voice but the speech models aren't downloaded yet.
#[tauri::command]
pub(crate) fn open_settings_voice(app: tauri::AppHandle) {
    open_settings(&app, "voice");
}

/// Open (or focus) the chat drawer — a right-docked panel for typed chat mode.
///
/// It's a *non-activating* panel (see `make_chat_panel`): it can become key to
/// receive typing, but never activates Jack as the front app. That's deliberate —
/// activating a normal app while you're in another app's full-screen Space makes
/// macOS jump you to the Desktop Space, which is exactly the bug we're avoiding.
#[tauri::command]
pub(crate) fn open_chat(app: tauri::AppHandle) {
    set_last_surface(&app, true); // chat is now the surface to restore on summon
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
            crate::platform::make_chat_panel(&win);
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
pub(crate) fn close_chat(app: tauri::AppHandle) {
    set_last_surface(&app, false); // voice/orb is now the surface to restore on summon
    if let Some(win) = app.get_webview_window("chat") {
        let _ = win.hide();
    }
    // Switching back to voice: the orb must reappear (it was hidden for chat) and its
    // web side must re-sync, or a stale idle-hide timer tucks it straight back away.
    surface_orb(&app);
    // Audible "switched to voice" cue, played by the orb (the now-visible surface).
    // The chat webview is hidden here, so its audio context is suspended — which is
    // why driving the cue from chat only ever fired once.
    with_orb(&app, |w| {
        let _ = w.eval("window.__modeEarcon && window.__modeEarcon('voice')");
    });
    #[cfg(target_os = "macos")]
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
}

/// Hide the chat drawer WITHOUT bringing the orb back — used when voice isn't set up
/// yet, so closing chat doesn't surface a useless orb (no voice models downloaded).
#[tauri::command]
pub(crate) fn hide_chat(app: tauri::AppHandle) {
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
pub(crate) fn request_voice(app: &tauri::AppHandle) {
    if let Some(chat) = app.get_webview_window("chat") {
        let _ = chat.eval("window.__enableVoice && window.__enableVoice()");
    } else {
        open_settings(app, "voice");
    }
}

/// Open (or focus) the small About window: app version + a manual update check.
///
/// Like Settings, this flips the app to the Regular activation policy while open
/// so the window comes forward and its button is clickable, then drops back to
/// Accessory (no Dock icon, background presence) once it's closed.
pub(crate) fn open_about(app: &tauri::AppHandle) {
    #[cfg(target_os = "macos")]
    {
        let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
        crate::platform::activate_app();
    }

    if let Some(win) = app.get_webview_window("about") {
        let _ = win.show();
        let _ = win.set_focus();
        return;
    }

    let handle = app.clone();
    match WebviewWindowBuilder::new(app, "about", WebviewUrl::App("about.html".into()))
        .title("About Jack")
        .inner_size(360.0, 380.0)
        .resizable(false)
        .build()
    {
        Ok(win) => {
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
        Err(e) => eprintln!("[jack] failed to open About: {e}"),
    }
}

/// Open (or focus) the Settings window — a normal, focusable window.
///
/// The app normally runs as a macOS *accessory* (no Dock icon, non-activating),
/// which means its windows can't receive keyboard focus — you couldn't type in
/// the API-key field. So while Settings is open we switch to the regular
/// activation policy (the app activates, fields are typable, a Dock icon appears)
/// and switch back to accessory when the window closes.
pub(crate) fn open_settings(app: &tauri::AppHandle, target: &str) {
    // `target`: "" (default Model tab), "report" (open the debug-report sheet), or
    // "voice" (jump to the Listening tab's voice-download section).
    // Become a regular app AND actually activate it — without activating, no
    // window can become "key", so text fields can't take focus or accept input.
    #[cfg(target_os = "macos")]
    {
        let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
        crate::platform::activate_app();
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
pub(crate) fn with_orb(app: &tauri::AppHandle, f: impl FnOnce(&tauri::WebviewWindow)) {
    if let Some(window) = app.get_webview_window("orb") {
        f(&window);
    }
}

/// Surface the orb window: show it at the OS level AND drive its own `showOrb()` so
/// the web side's auto-hide state stays in sync. Without the second step a stale idle
/// timer (armed at launch while the orb was hidden) can tuck the orb away right after
/// it's shown, leaving voice with no visible orb.
pub(crate) fn surface_orb(app: &tauri::AppHandle) {
    // The orb is an NSPanel and the app is an Accessory (no Dock, never the active
    // app). A plain window.show() / orderFront won't bring a hidden panel forward for
    // an inactive app — only orderFrontRegardless does (the same call make_floating_panel
    // uses to first show it). So drive the panel directly; the standard show() is just
    // a non-macOS / not-yet-a-panel fallback.
    #[cfg(target_os = "macos")]
    {
        use tauri_nspanel::ManagerExt;
        if let Ok(panel) = app.get_webview_panel("orb") {
            // w.show() updates Tauri's visibility state and un-occludes the WKWebView so
            // it actually renders; order_front_regardless brings the non-activating panel
            // to the front even though the accessory app is never the active app (a plain
            // show/orderFront is a no-op then). Not panel.show() — that makes it key and
            // would steal focus from the user's editor.
            with_orb(app, |w| {
                let _ = w.show();
            });
            panel.order_front_regardless();
            with_orb(app, |w| {
                let _ = w.eval("window.__showOrb && window.__showOrb()");
            });
            return;
        }
    }
    with_orb(app, |w| {
        let _ = w.show();
        let _ = w.eval("window.__showOrb && window.__showOrb()");
    });
}

/// Summon/dismiss Jack (the ⌘⌃J global hotkey). It toggles the *visibility* of the
/// current surface and, when summoning from hidden, restores whatever was last shown
/// (chat or voice) — it does NOT switch modes. So pressing it in chat hides/shows the
/// chat drawer, and it only ever shows the orb if the user had already enabled voice
/// (the orb is never surfaced on a fresh, chat-only app).
pub(crate) fn toggle_jack(app: &tauri::AppHandle) {
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
        surface_orb(app);
    }
}

/// Resize the orb window to a square `px`; the web orb scales to fill it.
pub(crate) fn resize(app: &tauri::AppHandle, px: f64) {
    with_orb(app, |w| {
        let _ = w.set_size(LogicalSize::new(px, px));
    });
}
