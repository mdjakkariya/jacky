//! Engine sidecar lifecycle: spawn the bundled `autobot-daemon` and stop it on exit.
//! All intelligence lives in that Python process; this shell only renders.
use std::sync::Mutex;

use tauri::Manager;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;

/// Holds the spawned engine sidecar so we can stop it when the app quits.
pub(crate) struct Engine(pub(crate) Mutex<Option<CommandChild>>);

/// Start the bundled `autobot-daemon` sidecar; the orb connects to it over the
/// local WebSocket. Logs (but doesn't crash) if it can't start.
#[allow(dead_code)] // only called in release builds (see setup)
pub(crate) fn start_engine(app: &tauri::AppHandle) {
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
pub(crate) fn stop_engine(app: &tauri::AppHandle) {
    if let Some(engine) = app.try_state::<Engine>() {
        if let Some(child) = engine.0.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
}
