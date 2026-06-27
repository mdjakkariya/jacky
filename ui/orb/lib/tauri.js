/** Thin, safe wrappers over the Tauri bridge. Every call is a no-op (resolves
 *  undefined) in a plain browser, so pages work standalone for dev. */

export function hasTauri() { return !!(window.__TAURI__ && window.__TAURI__.core); }
export function tauriWindow() { return (window.__TAURI__ && window.__TAURI__.window) || null; }

export async function invoke(cmd, args) {
  if (!hasTauri()) return undefined;
  try { return await window.__TAURI__.core.invoke(cmd, args); } catch (e) { return undefined; }
}

export const openExternal = (url) => invoke("open_external", { url });
export const revealInFinder = (path) => invoke("reveal_in_finder", { path });
export const copyToClipboard = (text) => invoke("copy_to_clipboard", { text });
export const pickFolder = () => invoke("pick_folder");
export const closeChat = () => invoke("close_chat");
export const hideChat = () => invoke("hide_chat");
export const openSettingsVoice = () => invoke("open_settings_voice");

export async function appVersion() {
  try { return await window.__TAURI__.app.getVersion(); } catch (e) { return "0.0.0"; }
}
