import { copyToClipboard } from "./tauri.js";

/** Copy text reliably: navigator.clipboard is unreliable under Tauri's custom
 *  protocol, so prefer the native command and fall back to the web API. */
export async function copyText(txt) {
  try { if (await copyToClipboard(txt)) return true; } catch (e) { /* fall through */ }
  try { await navigator.clipboard.writeText(txt); return true; } catch (e) { return false; }
}
