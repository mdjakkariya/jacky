//! Leaf Tauri commands (no window orchestration): app version, external links,
//! Finder reveal, the folder picker, clipboard, and the dev storage cleanup.

/// Return the app version (the compile-time `Cargo.toml` version, kept in sync with
/// `tauri.conf.json`/`pyproject.toml` by the release bump script). Read by the About
/// window so the version is never hard-coded in the webview.
#[tauri::command]
pub(crate) fn app_version() -> String {
    env!("CARGO_PKG_VERSION").to_string()
}

/// Open a URL in the user's default browser (e.g. the prefilled GitHub issue form).
/// Constrained to https links so it can only ever open the web, never run anything.
#[tauri::command]
pub(crate) fn open_external(url: String) {
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
pub(crate) fn reveal_in_finder(path: String) {
    #[cfg(target_os = "macos")]
    {
        let _ = std::process::Command::new("open").arg("-R").arg(&path).spawn();
    }
}

/// Open a native macOS folder chooser (via AppleScript) and return the chosen POSIX
/// path, or `None` if the user cancels. Same shell-out style as `reveal_in_finder`;
/// no extra Tauri plugin required.
#[tauri::command]
pub(crate) fn pick_folder() -> Option<String> {
    let out = std::process::Command::new("osascript")
        .arg("-e")
        .arg("POSIX path of (choose folder with prompt \"Choose a folder for Jack to work in\")")
        .output()
        .ok()?;
    if !out.status.success() {
        return None; // user cancelled (osascript exits non-zero)
    }
    let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
    if path.is_empty() { None } else { Some(path) }
}

/// Copy text to the system clipboard. The webview's navigator.clipboard is
/// unreliable under Tauri's custom protocol, so we write it natively (pbcopy).
/// Returns whether it succeeded so the UI can confirm honestly.
#[tauri::command]
pub(crate) fn copy_to_clipboard(text: String) -> bool {
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

/// Dev only: wipe debug artifacts under ~/.autobot (logs, sessions, reports) for a
/// fresh start. Leaves settings, secrets, voices, and memory untouched. The running
/// engine keeps writing to its already-open log until it's restarted.
#[cfg(debug_assertions)]
pub(crate) fn cleanup_storage(app: &tauri::AppHandle) -> String {
    use tauri::Manager;
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
