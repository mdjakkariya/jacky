/** Single source of truth for talking to the local daemon: one auto-reconnecting
 *  WebSocket with type-based subscription, plus a typed fetch client. Replaces the
 *  hand-rolled connect/reconnect loops and the ~30 inline fetch() calls. */

function deriveBase() {
  const q = new URLSearchParams(location.search);
  // settings opens with ?api=…, orb with ?ws=…; chat with neither.
  const api = q.get("api");
  if (api) return api.replace(/\/$/, "");
  const ws = q.get("ws");
  if (ws) return ws.replace(/^ws/, "http").replace(/\/ws$/, "");
  return "http://127.0.0.1:8765";
}

class Daemon {
  constructor() {
    this.base = deriveBase();
    this.wsUrl = this.base.replace(/^http/, "ws") + "/ws";
    this._ws = null;
    this._handlers = new Map(); // type -> Set<fn>
    this._openFns = new Set();
    this._closeFns = new Set();
    this._connected = false;
  }

  on(type, handler) {
    if (!this._handlers.has(type)) this._handlers.set(type, new Set());
    this._handlers.get(type).add(handler);
    return () => this._handlers.get(type)?.delete(handler);
  }
  onOpen(fn) { this._openFns.add(fn); return () => this._openFns.delete(fn); }
  onClose(fn) { this._closeFns.add(fn); return () => this._closeFns.delete(fn); }

  // Exposed for tests; routes a raw WS message event to type handlers.
  _dispatch(ev) {
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    const set = this._handlers.get(m.type);
    if (set) set.forEach((fn) => fn(m));
  }

  connect() {
    let ws;
    try { ws = new WebSocket(this.wsUrl); } catch (e) { this._scheduleReconnect(); return; }
    this._ws = ws;
    ws.onopen = () => { this._connected = true; this._openFns.forEach((fn) => fn()); };
    ws.onmessage = (ev) => this._dispatch(ev);
    ws.onclose = () => { this._connected = false; this._closeFns.forEach((fn) => fn()); this._scheduleReconnect(); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }
  _scheduleReconnect() { setTimeout(() => this.connect(), 1500); }

  async get(path) { return (await fetch(this.base + path)).json(); }
  async post(path, body) {
    const opts = { method: "POST", headers: { "content-type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    return (await fetch(this.base + path, opts)).json();
  }
  async delete(path) {
    const opts = { method: "DELETE", headers: { "content-type": "application/json" } };
    return (await fetch(this.base + path, opts)).json();
  }

  // --- named wrappers (thin; preserve each call site's exact payload) ---
  chat(text) { return this.post("/chat", { text }); }
  confirm(body) { return this.post("/confirm", body); } // chat: {value}; orb: {answer}
  action(tool, args) { return this.post("/action", { tool, args: args || {} }); }
  newSession() { return this.post("/session/new"); }
  workspace() { return this.get("/workspace"); }
  setWorkspace(path) { return this.post("/workspace", { path }); }
  voiceStatus() { return this.get("/voice/status"); }
  voiceDownload() { return this.post("/voice/download"); }
  settings() { return this.get("/settings"); }
  setSettings(patch) { return this.post("/settings", patch); }
  report() { return this.get("/report"); }
  reportConcise() { return this.get("/report/concise"); }
  reportFile() { return this.get("/report/file"); }
  healthz() { return fetch(this.base + "/healthz"); } // caller checks .ok
  permissions() { return this.get("/permissions"); }
  openPermission(key) { return this.post("/permissions/open", { key }); }
  models() { return this.get("/models"); }
  access() { return this.get("/access"); }
  grantAccess(path, write) { return this.post("/access/grant", { path, write }); }
  revokeAccess(path) { return this.post("/access/revoke", { path }); }
  secret(name, value) { return this.post("/secret", { name, value }); }

  // --- meeting methods ---
  meetingStart() { return this.post("/meeting/start", {}); }
  meetingStop() { return this.post("/meeting/stop", {}); }
  meetingPause() { return this.post("/meeting/pause", {}); }
  meetingResume() { return this.post("/meeting/resume", {}); }
  meetingLast() { return this.get("/meeting/last"); }

  // --- MCP methods ---
  mcpServers() { return this.get("/mcp/servers"); }
  addMcpServer(descriptor) { return this.post("/mcp/servers", descriptor); }
  removeMcpServer(id) { return this.delete(`/mcp/servers/${id}`); }
  enableMcpServer(id) { return this.post(`/mcp/servers/${id}/enable`); }
  disableMcpServer(id) { return this.post(`/mcp/servers/${id}/disable`); }
  connectMcpServer(id) { return this.post(`/mcp/servers/${id}/connect`); }
  testMcpServer(id) { return this.post(`/mcp/servers/${id}/test`); }
  mcpTools(id) { return this.get(`/mcp/servers/${id}/tools`); }
  setMcpToolOverride(id, tool, patch) { return this.post(`/mcp/servers/${id}/tools/${tool}`, patch); }
  mcpAuthStart(id) { return this.post(`/mcp/servers/${id}/auth/start`); }
  mcpSetToken(id, token) { return this.post("/secret", { name: `mcp.${id}.token`, value: token }); }
}

export const daemon = new Daemon();
