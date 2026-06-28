//! macOS window behavior: activation + non-activating floating NSPanels that draw
//! over other apps' full-screen Spaces. Whole module is macOS-only.
#![cfg(target_os = "macos")]

/// Bring the app to the foreground so its windows can become key (and text
/// fields editable). An accessory app stays inactive otherwise.
pub(crate) fn activate_app() {
    use objc::runtime::{Object, YES};
    use objc::{class, msg_send, sel, sel_impl};
    unsafe {
        let ns_app: *mut Object = msg_send![class!(NSApplication), sharedApplication];
        let _: () = msg_send![ns_app, activateIgnoringOtherApps: YES];
    }
}

/// Convert the orb window into a non-activating `NSPanel` that floats over other
/// apps' full-screen Spaces.
///
/// A plain `NSWindow` cannot be drawn over another app's full-screen window at
/// any level (confirmed: level 101 tops normal windows but not full-screen). The
/// working recipe — used by Spotlight-style overlays — is a panel with the
/// non-activating style mask plus `canJoinAllSpaces | fullScreenAuxiliary`.
#[allow(deprecated)] // tauri-nspanel re-exports the (now-deprecated) cocoa crate; still correct.
pub(crate) fn make_floating_panel(window: &tauri::WebviewWindow) {
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

/// Make the chat drawer float over other apps' full-screen Spaces while still
/// accepting keyboard input — the Spotlight/Alfred recipe.
///
/// A non-activating panel can become *key* (so you can type into it) WITHOUT
/// activating Jack as the front app. That matters because activating a normal app
/// over another app's full-screen window makes macOS switch to the Desktop Space —
/// which is why the chat used to appear on the wrong screen. `fullScreenAuxiliary`
/// + `canJoinAllSpaces` let it draw over the current full-screen Space instead.
/// We keep the resizable bit so the drawer can still be dragged-to-resize.
#[allow(deprecated)] // tauri-nspanel re-exports the (now-deprecated) cocoa crate; still correct.
pub(crate) fn make_chat_panel(window: &tauri::WebviewWindow) {
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
