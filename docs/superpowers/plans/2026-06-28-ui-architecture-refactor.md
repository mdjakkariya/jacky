# Jack UI Architecture Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restructure the four monolithic Tauri-webview HTML files into a buildless, modular, testable vanilla-JS + light-DOM-web-components architecture, with zero change to behavior or visuals.

**Architecture:** Each `ui/orb/*.html` becomes a thin shell (markup + one `<link>` + one `<script type="module">`). Logic moves into `ui/orb/lib/` (shared framework-free modules), `ui/orb/components/` (light-DOM custom elements that enhance existing markup, no Shadow DOM), and `ui/orb/pages/` (per-document entry modules). CSS moves into `ui/orb/styles/` with one shared `tokens.css` + native `@layer`. A dev-only `ui/package.json` adds Vitest + happy-dom; nothing about what ships changes — Tauri keeps loading raw files.

**Tech Stack:** Vanilla ES modules (no bundler), native Custom Elements (light DOM), native CSS (`@layer`, nesting, custom properties), Vitest 4 + happy-dom (dev-only test runner), npm (dev-only).

**Design spec:** `docs/superpowers/specs/2026-06-28-ui-architecture-design.md` (read it first).

## Global Constraints

Every task implicitly includes these (copied from the spec):

- **No runtime build step.** Tauri's `frontendDist` stays `../../orb`; pages load via `WebviewUrl::App("<page>.html")`. Do not introduce a bundler or change `tauri.conf.json`.
- **Strict CSP, no `unsafe-eval`, no external CDN.** `script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'`. No `eval`/`new Function`, no CDN-loaded code, no off-device calls. All code vendored locally.
- **On-device only / English only / permission gate intact** — this refactor touches no daemon logic and adds no network calls beyond those that already exist (the GitHub update check is preserved as-is).
- **Behavior + visuals must be identical to `main`.** This is a structural refactor. Logic is moved verbatim; markup and CSS values are preserved.
- **Imports are relative ES-module paths** (e.g. `import { daemon } from "../../lib/daemon.js"`). No import map.
- **Light DOM only** for components (no Shadow DOM), except none are needed here — the orb's WebGL stays a plain canvas in `lib/orb-renderer.js`.
- **Dev toolchain lives at `ui/`** (outside `frontendDist`); `node_modules/` is gitignored. Test files are co-located as `*.test.js` next to the module they test; Vitest globs `orb/**/*.test.js`.
- **Conventions:** Conventional Commits with DCO sign-off (`git commit -s`). **No `Co-Authored-By` trailer.** Branch is `refactor/ui-architecture` (already created).
- **Node version:** Node ≥ 20 (Vitest 4 requirement).

## File Structure

**Create:**
- `ui/package.json`, `ui/vitest.config.js`, `ui/.gitignore` addition (or root `.gitignore`)
- `ui/orb/lib/`: `daemon.js`, `tauri.js`, `clipboard.js`, `earcons.js`, `markdown.js`, `format.js`, `dom.js`, `orb-renderer.js` (+ co-located `*.test.js`)
- `ui/orb/styles/`: `tokens.css`, `reset.css`, `base.css`, `orb.css`, `chat.css`, `settings.css`, `about.css`
- `ui/orb/components/<name>/<name>.{js,css,test.js}` per component
- `ui/orb/pages/`: `orb.js`, `chat.js`, `settings.js`, `about.js`

**Modify:**
- `ui/orb/index.html`, `chat.html`, `settings.html`, `about.html` — strip inline `<style>`/`<script>`, keep body markup, add `<link>` + `<script type="module">`
- `Makefile` — add `ui-test` target + `.PHONY`
- `.gitignore` — add `node_modules/`

**Untouched:** `ui/orb-shell/src-tauri/**` (Rust shell, `tauri.conf.json`), all of `src/autobot/**` (daemon).

---

## Phase 0 — Dev toolchain scaffolding

### Task 1: Dev-only test toolchain (Vitest + happy-dom)

**Files:**
- Create: `ui/package.json`, `ui/vitest.config.js`, `ui/orb/lib/_sanity.test.js`
- Modify: `Makefile` (add `ui-test` target + `.PHONY` entry), `.gitignore` (add `node_modules/`)

**Interfaces:**
- Produces: `npm --prefix ui test` runs Vitest over `ui/orb/**/*.test.js` in the happy-dom environment; `make ui-test` is the canonical entry point.

- [ ] **Step 1: Create `ui/package.json`**

```json
{
  "name": "jack-ui",
  "private": true,
  "type": "module",
  "description": "Dev-only toolchain for the Jack webview UI. Not shipped; not required to run the app.",
  "scripts": {
    "test": "vitest run",
    "test:watch": "vitest"
  },
  "devDependencies": {
    "vitest": "^4.1.9",
    "happy-dom": "^20.10.6"
  }
}
```

- [ ] **Step 2: Create `ui/vitest.config.js`**

```js
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // happy-dom is fast and implements customElements + lifecycle callbacks,
    // which is what our light-DOM components need. Add `// @vitest-environment jsdom`
    // atop a file only if it needs fuller CSS-cascade fidelity.
    environment: "happy-dom",
    globals: true,
    include: ["orb/**/*.test.js"],
  },
});
```

- [ ] **Step 3: Add `node_modules/` to `.gitignore`**

Add under the "Virtual envs / uv" or a new "Node (dev-only UI toolchain)" section:

```
# Node (dev-only UI toolchain — Vitest; never shipped)
node_modules/
```

- [ ] **Step 4: Add the `ui-test` Makefile target**

Add `ui-test` to the `.PHONY` line (line 5), and add this target after the `test` target (after line 32):

```makefile
ui-test: ## Run the UI unit tests (Vitest + happy-dom; dev-only, needs Node ≥ 20)
	npm --prefix ui install
	npm --prefix ui test
```

- [ ] **Step 5: Write a sanity test to prove the harness works**

Create `ui/orb/lib/_sanity.test.js`:

```js
import { describe, it, expect } from "vitest";

describe("test harness", () => {
  it("runs in a DOM environment", () => {
    const el = document.createElement("div");
    el.textContent = "ok";
    expect(el.textContent).toBe("ok");
  });

  it("supports custom elements", () => {
    expect(typeof customElements).toBe("object");
    expect(typeof customElements.define).toBe("function");
  });
});
```

- [ ] **Step 6: Run the tests — verify they pass**

Run: `make ui-test`
Expected: Vitest installs deps, then reports both `_sanity.test.js` tests PASS.

- [ ] **Step 7: Commit**

```bash
git add ui/package.json ui/vitest.config.js ui/orb/lib/_sanity.test.js Makefile .gitignore
git commit -s -m "chore: add dev-only Vitest + happy-dom toolchain for the UI"
```

---

## Phase 1 — Shared pure libs (TDD-first; highest ROI)

These extract pure functions out of the IIFEs. Write tests capturing **current** behavior, then move the code. Delete `_sanity.test.js` once real tests exist (fold into Task 2).

### Task 2: `lib/format.js` — number/version formatters

**Files:**
- Create: `ui/orb/lib/format.js`, `ui/orb/lib/format.test.js`
- Delete: `ui/orb/lib/_sanity.test.js` (replaced by real tests)
- Source: `ui/orb/chat.html:663-678` (`fmtK`, `fmtModel`, `fmtUSD`), `chat.html:750-757` (`cmpVer`), `about.html:74-87` (`parts`/`cmp` — unified into `cmpVer`)

**Interfaces:**
- Produces:
  - `fmtK(n: number): string` — `<1000` → `toLocaleString()`; else `"36.0k"` / `"200k"` (≥100k rounds, else 1 decimal).
  - `fmtModel(m: string): string` — strips leading `claude-`, turns trailing `-4-5` → `-4.5`.
  - `fmtUSD(v: number): string` — `<=0`→`"$0.00"`, `<0.0001`→`"<$0.0001"`, `<1`→4 decimals, else 2 decimals.
  - `cmpVer(a: string, b: string): -1|0|1` — strips a leading `v` and any `-prerelease` suffix, compares numeric dotted core.

- [ ] **Step 1: Write the failing tests**

Create `ui/orb/lib/format.test.js`:

```js
import { describe, it, expect } from "vitest";
import { fmtK, fmtModel, fmtUSD, cmpVer } from "./format.js";

describe("fmtK", () => {
  it("keeps small numbers exact", () => { expect(fmtK(0)).toBe("0"); expect(fmtK(999)).toBe("999"); });
  it("uses one decimal under 100k", () => { expect(fmtK(36000)).toBe("36.0k"); });
  it("rounds at/above 100k", () => { expect(fmtK(200000)).toBe("200k"); });
  it("treats nullish as 0", () => { expect(fmtK(undefined)).toBe("0"); });
});

describe("fmtModel", () => {
  it("shortens claude ids", () => { expect(fmtModel("claude-haiku-4-5")).toBe("haiku-4.5"); });
  it("passes locals through", () => { expect(fmtModel("qwen3:8b")).toBe("qwen3:8b"); });
  it("handles nullish", () => { expect(fmtModel(null)).toBe("—"); });
});

describe("fmtUSD", () => {
  it("zero or less", () => { expect(fmtUSD(0)).toBe("$0.00"); expect(fmtUSD(-1)).toBe("$0.00"); });
  it("tiny nonzero", () => { expect(fmtUSD(0.00005)).toBe("<$0.0001"); });
  it("sub-dollar uses 4 decimals", () => { expect(fmtUSD(0.1234)).toBe("$0.1234"); });
  it("dollar+ uses 2 decimals", () => { expect(fmtUSD(2.5)).toBe("$2.50"); });
});

describe("cmpVer", () => {
  it("orders core versions", () => { expect(cmpVer("1.2.0", "1.1.9")).toBe(1); expect(cmpVer("1.0.0", "1.0.1")).toBe(-1); expect(cmpVer("1.0.0", "1.0.0")).toBe(0); });
  it("strips a leading v", () => { expect(cmpVer("v2.0.0", "1.9.9")).toBe(1); });
  it("ignores prerelease suffix", () => { expect(cmpVer("1.2.3-beta", "1.2.3")).toBe(0); });
});
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `npm --prefix ui test`
Expected: FAIL — `Cannot find module './format.js'`.

- [ ] **Step 3: Implement `ui/orb/lib/format.js`**

```js
/** Pure display formatters for the chat header (token counts, model name, cost) and version compare. */

/** Compact token counts: 36000 -> "36.0k", 200000 -> "200k", <1000 stays exact. */
export function fmtK(n) {
  n = n || 0;
  if (n < 1000) return n.toLocaleString();
  const k = n / 1000;
  return (k >= 100 ? Math.round(k) : k.toFixed(1)) + "k";
}

/** Friendlier model label: "claude-haiku-4-5" -> "haiku-4.5"; locals pass through. */
export function fmtModel(m) {
  return (m || "—").replace(/^claude-/, "").replace(/-(\d+)-(\d+)$/, "-$1.$2");
}

/** Session cost in USD, scaled for readability. */
export function fmtUSD(v) {
  if (v <= 0) return "$0.00";
  if (v < 0.0001) return "<$0.0001";
  if (v < 1) return "$" + v.toFixed(4);
  return "$" + v.toFixed(2);
}

/** Compare dotted version cores. Strips a leading "v" and any "-prerelease" suffix. */
export function cmpVer(a, b) {
  const core = (v) => String(v).replace(/^v/i, "").split("-")[0].split(".");
  const pa = core(a), pb = core(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = parseInt(pa[i] || "0", 10), y = parseInt(pb[i] || "0", 10);
    if (x > y) return 1;
    if (x < y) return -1;
  }
  return 0;
}
```

- [ ] **Step 4: Delete the sanity test**

```bash
git rm ui/orb/lib/_sanity.test.js
```

- [ ] **Step 5: Run tests — verify they pass**

Run: `npm --prefix ui test`
Expected: PASS (all `format.test.js` cases).

- [ ] **Step 6: Commit**

```bash
git add ui/orb/lib/format.js ui/orb/lib/format.test.js
git commit -s -m "refactor(ui): extract pure formatters into lib/format.js with tests"
```

### Task 3: `lib/markdown.js` — chat markdown renderer

**Files:**
- Create: `ui/orb/lib/markdown.js`, `ui/orb/lib/markdown.test.js`
- Source: `ui/orb/chat.html:311-343` (`escapeHtml` + `renderMarkdown`)

**Interfaces:**
- Produces:
  - `escapeHtml(s: string): string` — escapes `&`, `<`, `>`.
  - `renderMarkdown(src: string): string` — returns HTML string. Escapes first (so model text cannot inject markup), then renders fenced code blocks, inline code, `[text](http(s)://…)` links as `<a class="mdlink">`, `**bold**`, `*italic*`, `-`/`*` unordered lists, `N.` ordered lists, paragraphs.

- [ ] **Step 1: Write the failing tests**

Create `ui/orb/lib/markdown.test.js`:

```js
import { describe, it, expect } from "vitest";
import { escapeHtml, renderMarkdown } from "./markdown.js";

describe("escapeHtml", () => {
  it("escapes the three dangerous chars", () => {
    expect(escapeHtml('<a href="x">&')).toBe("&lt;a href=\"x\"&gt;&amp;");
  });
});

describe("renderMarkdown", () => {
  it("escapes HTML before rendering (no injection)", () => {
    expect(renderMarkdown("<script>")).toContain("&lt;script&gt;");
  });
  it("wraps a plain line in a paragraph", () => {
    expect(renderMarkdown("hello")).toBe("<p>hello</p>");
  });
  it("renders bold and italic", () => {
    expect(renderMarkdown("**b**")).toContain("<strong>b</strong>");
    expect(renderMarkdown("a *i*")).toContain("<em>i</em>");
  });
  it("renders inline code", () => {
    expect(renderMarkdown("`x`")).toContain("<code>x</code>");
  });
  it("renders a fenced code block", () => {
    const out = renderMarkdown("```\nline\n```");
    expect(out).toContain("<pre><code>");
    expect(out).toContain("line");
  });
  it("renders an unordered list", () => {
    const out = renderMarkdown("- one\n- two");
    expect(out).toContain("<ul><li>one</li><li>two</li></ul>");
  });
  it("renders an ordered list", () => {
    const out = renderMarkdown("1. one\n2. two");
    expect(out).toContain("<ol><li>one</li><li>two</li></ol>");
  });
  it("renders http links as mdlink anchors", () => {
    expect(renderMarkdown("[gh](https://github.com)")).toContain('<a class="mdlink" href="https://github.com">gh</a>');
  });
});
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `npm --prefix ui test`
Expected: FAIL — `Cannot find module './markdown.js'`.

- [ ] **Step 3: Implement `ui/orb/lib/markdown.js`**

Move `escapeHtml` and `renderMarkdown` **verbatim** from `chat.html:311-343`, converting to named exports. The body is unchanged:

```js
/** Minimal, dependency-free Markdown for Jack's replies. Everything is HTML-escaped
 *  first, so model text can't inject markup; we only add our own tags for code,
 *  lists, bold/italic, and http(s) links. */

export function escapeHtml(s) {
  return s.replace(/[&<>]/g, function (c) { return c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;"; });
}

export function renderMarkdown(src) {
  const blocks = [];
  src = src.replace(/```[a-zA-Z0-9]*\n?([\s\S]*?)```/g, function (_m, code) {
    blocks.push("<pre><code>" + escapeHtml(code.replace(/\n$/, "")) + "</code></pre>");
    return " B" + (blocks.length - 1) + " ";
  });
  src = escapeHtml(src);
  src = src.replace(/`([^`]+)`/g, function (_m, c) { return "<code>" + c + "</code>"; });
  src = src.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (_m, t, u) { return '<a class="mdlink" href="' + u + '">' + t + "</a>"; });
  src = src.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  src = src.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  const lines = src.split("\n"); const out = []; let i = 0;
  while (i < lines.length) {
    const ph = lines[i].match(/^ B(\d+) $/);
    if (ph) { out.push(blocks[+ph[1]]); i++; continue; }
    if (/^\s*[-*]\s+/.test(lines[i])) {
      const ul = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) { ul.push("<li>" + lines[i].replace(/^\s*[-*]\s+/, "") + "</li>"); i++; }
      out.push("<ul>" + ul.join("") + "</ul>"); continue;
    }
    if (/^\s*\d+\.\s+/.test(lines[i])) {
      const ol = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { ol.push("<li>" + lines[i].replace(/^\s*\d+\.\s+/, "") + "</li>"); i++; }
      out.push("<ol>" + ol.join("") + "</ol>"); continue;
    }
    if (lines[i].trim() === "") { i++; continue; }
    out.push("<p>" + lines[i] + "</p>"); i++;
  }
  return out.join("");
}
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `npm --prefix ui test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/orb/lib/markdown.js ui/orb/lib/markdown.test.js
git commit -s -m "refactor(ui): extract markdown renderer into lib/markdown.js with tests"
```

---

## Phase 2 — Shared infrastructure libs

### Task 4: `lib/daemon.js` — the single daemon client (WS + fetch)

**Files:**
- Create: `ui/orb/lib/daemon.js`, `ui/orb/lib/daemon.test.js`
- Source patterns: `chat.html:254-255` + `725-741` (WS connect/reconnect + dispatch), `index.html:113-114` + `215-244` (orb WS + override), `settings.html:304` + `321` (api/ws derivation), and every `fetch(API + "/…")` call across the three files.

**Interfaces:**
- Produces a singleton `daemon` with:
  - `daemon.base: string` — HTTP base, e.g. `http://127.0.0.1:8765`. Derived once: prefer `?api=` query param, else derive from `?ws=`, else default `http://127.0.0.1:8765`.
  - `daemon.wsUrl: string` — e.g. `ws://127.0.0.1:8765/ws`.
  - `daemon.connect(): void` — idempotent; opens the WebSocket and auto-reconnects (1.5s) on close/error.
  - `daemon.on(type: string, handler: (msg) => void): () => void` — subscribe to WS messages of `msg.type === type`; returns an unsubscribe fn.
  - `daemon.onOpen(fn)`, `daemon.onClose(fn)` — connection lifecycle hooks (orb uses these for the `conn` hint).
  - `daemon.get(path): Promise<any>` — GET + parse JSON; throws on network/parse failure.
  - `daemon.post(path, body?): Promise<any>` — POST JSON (or empty) + parse JSON; throws on failure.
  - Named convenience wrappers (thin over get/post), used by pages:
    `chat(text)`, `confirm(body)` *(POST `/confirm` with the given object — chat passes `{value}`, orb passes `{answer}`)*, `action(tool, args)`, `newSession()`, `workspace()`, `setWorkspace(path)`, `voiceStatus()`, `voiceDownload()`, `settings()`, `setSettings(patch)`, `report()`, `reportConcise()`, `reportFile()`, `healthz()`, `permissions()`, `openPermission(key)`, `models()`, `access()`, `grantAccess(path, write)`, `revokeAccess(path)`, `secret(name, value)`.

- [ ] **Step 1: Write the failing tests**

Create `ui/orb/lib/daemon.test.js`. (happy-dom lacks a real WebSocket/fetch; we stub them.)

```js
import { describe, it, expect, vi, beforeEach } from "vitest";

// Import fresh each test so the singleton's derived base reflects the stubbed location.
async function freshDaemon() {
  vi.resetModules();
  return (await import("./daemon.js")).daemon;
}

beforeEach(() => {
  // Default location: no query params.
  Object.defineProperty(window, "location", { value: new URL("http://tauri.localhost/chat.html"), writable: true });
});

describe("base/ws derivation", () => {
  it("defaults to loopback when no params", async () => {
    const d = await freshDaemon();
    expect(d.base).toBe("http://127.0.0.1:8765");
    expect(d.wsUrl).toBe("ws://127.0.0.1:8765/ws");
  });
  it("honors ?api= (settings)", async () => {
    window.location = new URL("http://tauri.localhost/settings.html?api=http://127.0.0.1:9000");
    const d = await freshDaemon();
    expect(d.base).toBe("http://127.0.0.1:9000");
    expect(d.wsUrl).toBe("ws://127.0.0.1:9000/ws");
  });
  it("honors ?ws= (orb)", async () => {
    window.location = new URL("http://tauri.localhost/index.html?ws=ws://localhost:8765/ws");
    const d = await freshDaemon();
    expect(d.base).toBe("http://localhost:8765");
  });
});

describe("get/post", () => {
  it("get parses JSON", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({ ok: true }) });
    await expect(d.get("/healthz")).resolves.toEqual({ ok: true });
    expect(global.fetch).toHaveBeenCalledWith("http://127.0.0.1:8765/healthz");
  });
  it("post sends JSON body", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.post("/chat", { text: "hi" });
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8765/chat");
    expect(opts.method).toBe("POST");
    expect(JSON.parse(opts.body)).toEqual({ text: "hi" });
  });
  it("confirm posts the caller's exact payload shape", async () => {
    const d = await freshDaemon();
    global.fetch = vi.fn().mockResolvedValue({ json: async () => ({}) });
    await d.confirm({ value: "yes" });
    await d.confirm({ answer: true });
    expect(JSON.parse(global.fetch.mock.calls[0][1].body)).toEqual({ value: "yes" });
    expect(JSON.parse(global.fetch.mock.calls[1][1].body)).toEqual({ answer: true });
  });
});

describe("on() dispatch", () => {
  it("routes a parsed message to its type handler and unsubscribes", async () => {
    const d = await freshDaemon();
    const seen = [];
    const off = d.on("context", (m) => seen.push(m));
    d._dispatch({ data: JSON.stringify({ type: "context", pct: 50 }) });
    expect(seen).toEqual([{ type: "context", pct: 50 }]);
    off();
    d._dispatch({ data: JSON.stringify({ type: "context", pct: 60 }) });
    expect(seen.length).toBe(1);
  });
  it("ignores non-JSON frames", async () => {
    const d = await freshDaemon();
    expect(() => d._dispatch({ data: "not json" })).not.toThrow();
  });
});
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `npm --prefix ui test`
Expected: FAIL — `Cannot find module './daemon.js'`.

- [ ] **Step 3: Implement `ui/orb/lib/daemon.js`**

```js
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
    this._handlers = new Map();   // type -> Set<fn>
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

  // --- named wrappers (thin; preserve each call site's exact payload) ---
  chat(text) { return this.post("/chat", { text }); }
  confirm(body) { return this.post("/confirm", body); }   // chat: {value}; orb: {answer}
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
  healthz() { return fetch(this.base + "/healthz"); }   // caller checks .ok
  permissions() { return this.get("/permissions"); }
  openPermission(key) { return this.post("/permissions/open", { key }); }
  models() { return this.get("/models"); }
  access() { return this.get("/access"); }
  grantAccess(path, write) { return this.post("/access/grant", { path, write }); }
  revokeAccess(path) { return this.post("/access/revoke", { path }); }
  secret(name, value) { return this.post("/secret", { name, value }); }
}

export const daemon = new Daemon();
```

> Note: `healthz()` returns the raw `Response` (not parsed) because callers check `.ok` only — preserving `chat.html`'s `(await fetch(API + "/healthz")).ok` behavior. The `/voice/download` progress socket (`startVoiceDownload`) opens its **own** short-lived WebSocket; keep that in the `voice-download` component (Task 16), not in `daemon`, since it must capture early frames before posting.

- [ ] **Step 4: Run tests — verify they pass**

Run: `npm --prefix ui test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/orb/lib/daemon.js ui/orb/lib/daemon.test.js
git commit -s -m "refactor(ui): add lib/daemon.js — single WS + fetch client"
```

### Task 5: `lib/tauri.js` — Tauri bridge

**Files:**
- Create: `ui/orb/lib/tauri.js`, `ui/orb/lib/tauri.test.js`
- Source: the `tauri()` helper (`chat.html:839`), `window.__TAURI__.core.invoke(...)` calls across chat/settings/about, and `window.__TAURI__.window` access in the orb.

**Interfaces:**
- Produces:
  - `hasTauri(): boolean` — true when `window.__TAURI__.core` exists.
  - `invoke(cmd: string, args?: object): Promise<any>` — calls `window.__TAURI__.core.invoke`; resolves `undefined` (never throws) when Tauri is absent.
  - Convenience wrappers (all no-op/resolve when Tauri absent): `openExternal(url)`, `revealInFinder(path)`, `copyToClipboard(text): Promise<boolean>`, `pickFolder(): Promise<string|null>`, `appVersion(): Promise<string>`, `closeChat()`, `hideChat()`, `openSettingsVoice()`.
  - `tauriWindow(): object|null` — returns `window.__TAURI__.window` or null (orb window mgmt).

- [ ] **Step 1: Write the failing tests**

Create `ui/orb/lib/tauri.test.js`:

```js
import { describe, it, expect, vi, beforeEach } from "vitest";
import { hasTauri, invoke, openExternal, appVersion } from "./tauri.js";

beforeEach(() => { delete window.__TAURI__; });

describe("without Tauri", () => {
  it("hasTauri is false", () => { expect(hasTauri()).toBe(false); });
  it("invoke resolves undefined and does not throw", async () => {
    await expect(invoke("anything")).resolves.toBeUndefined();
  });
  it("appVersion falls back to 0.0.0", async () => { expect(await appVersion()).toBe("0.0.0"); });
});

describe("with Tauri", () => {
  it("invoke delegates to core.invoke", async () => {
    const core = { invoke: vi.fn().mockResolvedValue("ok") };
    window.__TAURI__ = { core };
    expect(hasTauri()).toBe(true);
    await expect(invoke("ping", { a: 1 })).resolves.toBe("ok");
    expect(core.invoke).toHaveBeenCalledWith("ping", { a: 1 });
  });
  it("openExternal forwards the url", async () => {
    const core = { invoke: vi.fn().mockResolvedValue(undefined) };
    window.__TAURI__ = { core };
    await openExternal("https://x.test");
    expect(core.invoke).toHaveBeenCalledWith("open_external", { url: "https://x.test" });
  });
});
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `npm --prefix ui test`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `ui/orb/lib/tauri.js`**

```js
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
```

> Note: `appVersion()` uses `__TAURI__.app.getVersion()` (chat's update check) — `about.html` uses the `app_version` invoke command instead. Keep both behaviors: `about.js` (Task 19) calls `invoke("app_version")` directly; chat (Task 14) calls `appVersion()`. Do not collapse them.

- [ ] **Step 4: Run tests — verify they pass**

Run: `npm --prefix ui test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/orb/lib/tauri.js ui/orb/lib/tauri.test.js
git commit -s -m "refactor(ui): add lib/tauri.js bridge wrappers"
```

### Task 6: `lib/clipboard.js` — copyText

**Files:**
- Create: `ui/orb/lib/clipboard.js`, `ui/orb/lib/clipboard.test.js`
- Source: `chat.html:794-797` and `settings.html:660-667` (identical `copyText`).

**Interfaces:**
- Consumes: `copyToClipboard` from `lib/tauri.js`.
- Produces: `copyText(txt: string): Promise<boolean>` — try native invoke first; fall back to `navigator.clipboard.writeText`; resolve `true`/`false`.

- [ ] **Step 1: Write the failing tests**

Create `ui/orb/lib/clipboard.test.js`:

```js
import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("./tauri.js", () => ({ copyToClipboard: vi.fn() }));
import { copyToClipboard } from "./tauri.js";
import { copyText } from "./clipboard.js";

beforeEach(() => { vi.clearAllMocks(); });

it("returns true when native copy succeeds", async () => {
  copyToClipboard.mockResolvedValue(true);
  expect(await copyText("hi")).toBe(true);
});

it("falls back to navigator.clipboard when native fails", async () => {
  copyToClipboard.mockResolvedValue(false);
  const writeText = vi.fn().mockResolvedValue();
  Object.assign(navigator, { clipboard: { writeText } });
  expect(await copyText("hi")).toBe(true);
  expect(writeText).toHaveBeenCalledWith("hi");
});

it("returns false when both fail", async () => {
  copyToClipboard.mockResolvedValue(false);
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("no")) } });
  expect(await copyText("hi")).toBe(false);
});
```

- [ ] **Step 2: Run — verify fail.** Run: `npm --prefix ui test` → module not found.

- [ ] **Step 3: Implement `ui/orb/lib/clipboard.js`**

```js
import { copyToClipboard } from "./tauri.js";

/** Copy text reliably: navigator.clipboard is unreliable under Tauri's custom
 *  protocol, so prefer the native command and fall back to the web API. */
export async function copyText(txt) {
  try { if (await copyToClipboard(txt)) return true; } catch (e) { /* fall through */ }
  try { await navigator.clipboard.writeText(txt); return true; } catch (e) { return false; }
}
```

- [ ] **Step 4: Run — verify pass.** Run: `npm --prefix ui test` → PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/orb/lib/clipboard.js ui/orb/lib/clipboard.test.js
git commit -s -m "refactor(ui): add lib/clipboard.js (dedup copyText)"
```

### Task 7: `lib/earcons.js` — WebAudio cues (parameterized)

**Files:**
- Create: `ui/orb/lib/earcons.js`, `ui/orb/lib/earcons.test.js`
- Source: `chat.html:844-869` and `index.html:432-483` (near-identical audio code; differ only in master gain — chat 0.16, orb 0.30 — and which cues exist).

**Interfaces:**
- Produces a factory: `createEarcons({ gain = 0.16 } = {}): { enabled, blip, playMode, playState, resumeOnGesture }`
  - `enabled(): boolean` — reads `localStorage["jackEarcons"] !== "0"`.
  - `blip(freq, start, dur, peak, type)` — schedule one oscillator blip (lazy AudioContext).
  - `playMode(mode: "voice"|"chat")` — ascending pair for voice, descending for chat; resumes a suspended context first.
  - `playState(state: "listening"|"thinking"|...)` — per-state cue with 1.5s debounce; no cue for idle/talking.
  - `resumeOnGesture()` — wire one-shot pointerdown/keydown listeners to resume a suspended context.

**Note:** orb master gain = 0.30, chat master gain = 0.16. Each page constructs its own instance with the right gain (Tasks 13/14). Frequencies/cues are copied verbatim from the sources.

- [ ] **Step 1: Write the failing tests**

Create `ui/orb/lib/earcons.test.js` (stub AudioContext + localStorage; we test the logic, not real sound):

```js
import { describe, it, expect, vi, beforeEach } from "vitest";
import { createEarcons } from "./earcons.js";

beforeEach(() => {
  localStorage.clear();
  // Minimal AudioContext stub
  const osc = { type: "", frequency: { value: 0 }, connect: vi.fn(), start: vi.fn(), stop: vi.fn() };
  const gainNode = { gain: { value: 0, setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() }, connect: vi.fn() };
  globalThis.AudioContext = vi.fn(() => ({
    currentTime: 0, state: "running",
    createOscillator: () => osc, createGain: () => gainNode,
    destination: {}, resume: vi.fn(),
  }));
});

it("enabled() honors the localStorage opt-out", () => {
  const e = createEarcons();
  expect(e.enabled()).toBe(true);
  localStorage.setItem("jackEarcons", "0");
  expect(e.enabled()).toBe(false);
});

it("playState is a no-op for idle (no cue) and debounces repeats", () => {
  const e = createEarcons({ gain: 0.3 });
  const spy = vi.spyOn(AudioContext.prototype ?? Object.prototype, "createOscillator");
  // idle has no cue → does not throw, no oscillator demanded beyond context creation
  expect(() => e.playState("idle")).not.toThrow();
});

it("does nothing when disabled", () => {
  localStorage.setItem("jackEarcons", "0");
  const e = createEarcons();
  expect(() => e.playMode("voice")).not.toThrow();
});
```

> The earcon tests assert *guard logic* (enabled flag, idle no-op, no throw under a stubbed context) — not audio output, which is verified manually. Keep them light.

- [ ] **Step 2: Run — verify fail.**

- [ ] **Step 3: Implement `ui/orb/lib/earcons.js`**

Build the factory by moving the audio code from the sources verbatim into closure scope. Use `gain` for the master gain node value. Include `blip`, `playMode` (from chat's `playModeEarcon` + orb's), and `playState` (from orb's `_EARCON`/`playEarcon` with the 1.5s debounce). Full module:

```js
/** WebAudio earcons, on-device, no asset. Factory so each surface (orb/chat) sets
 *  its own master gain. Disabled via localStorage "jackEarcons" = "0". */
export function createEarcons({ gain = 0.16 } = {}) {
  let ac = null, acGain = null;
  let lastEar = { s: "", t: 0 };

  function enabled() { try { return localStorage.getItem("jackEarcons") !== "0"; } catch (e) { return true; } }

  function audioCtx() {
    if (ac) return ac;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext; if (!Ctx) return null;
      ac = new Ctx(); acGain = ac.createGain(); acGain.gain.value = gain; acGain.connect(ac.destination);
    } catch (e) { ac = null; }
    return ac;
  }

  function blip(freq, start, dur, peak, type) {
    const c = audioCtx(); if (!c) return;
    const o = c.createOscillator(), g = c.createGain();
    o.type = type || "sine"; o.frequency.value = freq; o.connect(g); g.connect(acGain);
    const t0 = c.currentTime + start;
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(peak, t0 + 0.015);
    g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
    o.start(t0); o.stop(t0 + dur + 0.02);
  }

  // Ascending pair = voice on; descending = back to chat.
  function playMode(mode) {
    if (!enabled()) return;
    const c = audioCtx(); if (!c) return;
    const go = () => {
      if (mode === "voice") { blip(587.33, 0, 0.14, 0.85, "sine"); blip(880, 0.10, 0.20, 0.8, "sine"); }
      else { blip(880, 0, 0.12, 0.7, "sine"); blip(587.33, 0.10, 0.16, 0.7, "sine"); }
    };
    if (c.state === "suspended") { c.resume().then(go).catch(go); } else { go(); }
  }

  const STATE_CUES = {
    listening: () => { blip(659.25, 0, 0.14, 0.9, "sine"); blip(987.77, 0.09, 0.18, 0.8, "sine"); },
    thinking: () => { blip(440, 0, 0.07, 0.95, "triangle"); blip(440, 0.18, 0.08, 0.9, "triangle"); },
  };
  function playState(next) {
    if (!enabled()) return;
    const fn = STATE_CUES[next]; if (!fn) return;     // idle/talking: no cue
    const now = Date.now();
    if (lastEar.s === next && (now - lastEar.t) < 1500) return;   // debounce VAD flutter
    const c = audioCtx(); if (!c) return;
    if (c.state === "suspended") { try { c.resume(); } catch (e) {} }
    lastEar = { s: next, t: now }; fn();
  }

  function resumeOnGesture() {
    ["pointerdown", "keydown"].forEach((ev) =>
      window.addEventListener(ev, () => { const c = audioCtx(); if (c && c.state === "suspended") { try { c.resume(); } catch (e) {} } }, { once: true, passive: true }));
  }

  return { enabled, blip, playMode, playState, resumeOnGesture };
}
```

> `Date.now()` is used for the debounce, exactly as the source. happy-dom provides it.

- [ ] **Step 4: Run — verify pass.**

- [ ] **Step 5: Commit**

```bash
git add ui/orb/lib/earcons.js ui/orb/lib/earcons.test.js
git commit -s -m "refactor(ui): add lib/earcons.js (dedup orb+chat audio cues)"
```

### Task 8: `lib/dom.js` — DOM helpers

**Files:**
- Create: `ui/orb/lib/dom.js`, `ui/orb/lib/dom.test.js`
- Source: the repeated `$ = (id) => document.getElementById(id)` and ad-hoc element creation.

**Interfaces:**
- Produces:
  - `$(id: string): HTMLElement|null` — `getElementById`.
  - `el(tag, props?, children?): HTMLElement` — create element; `props` sets `className`, `textContent`, `id`, and any other property/attribute; `children` is a node or array of nodes/strings.
  - `on(target, type, handler, opts?)` — `addEventListener` returning an unsubscribe fn.

- [ ] **Step 1: Write the failing tests**

Create `ui/orb/lib/dom.test.js`:

```js
import { describe, it, expect, vi } from "vitest";
import { $, el, on } from "./dom.js";

it("$ finds by id", () => {
  document.body.innerHTML = '<div id="x">hi</div>';
  expect($("x").textContent).toBe("hi");
});

it("el builds an element with props and children", () => {
  const node = el("button", { className: "btn", id: "go" }, "Click");
  expect(node.tagName).toBe("BUTTON");
  expect(node.className).toBe("btn");
  expect(node.id).toBe("go");
  expect(node.textContent).toBe("Click");
});

it("el accepts an array of children", () => {
  const node = el("div", {}, [el("span", {}, "a"), "b"]);
  expect(node.childNodes.length).toBe(2);
  expect(node.textContent).toBe("ab");
});

it("on adds and the returned fn removes the listener", () => {
  const node = el("button");
  const handler = vi.fn();
  const off = on(node, "click", handler);
  node.click(); expect(handler).toHaveBeenCalledTimes(1);
  off(); node.click(); expect(handler).toHaveBeenCalledTimes(1);
});
```

- [ ] **Step 2: Run — verify fail.**

- [ ] **Step 3: Implement `ui/orb/lib/dom.js`**

```js
/** Tiny DOM helpers shared across pages/components. No framework. */
export const $ = (id) => document.getElementById(id);

export function el(tag, props = {}, children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "className") node.className = v;
    else if (k === "textContent") node.textContent = v;
    else if (k in node) node[k] = v;
    else node.setAttribute(k, v);
  }
  if (children != null) {
    for (const c of [].concat(children)) {
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
  }
  return node;
}

export function on(target, type, handler, opts) {
  target.addEventListener(type, handler, opts);
  return () => target.removeEventListener(type, handler, opts);
}
```

- [ ] **Step 4: Run — verify pass.**

- [ ] **Step 5: Commit**

```bash
git add ui/orb/lib/dom.js ui/orb/lib/dom.test.js
git commit -s -m "refactor(ui): add lib/dom.js helpers"
```

### Task 9: `lib/orb-renderer.js` — WebGL orb (with a testable state-style map)

**Files:**
- Create: `ui/orb/lib/orb-renderer.js`, `ui/orb/lib/orb-renderer.test.js`
- Source: `index.html:116-211` (canvas setup, shaders `vs`/`fs`, `SC`/`SP` maps, `overlay`, `frame`) and the `size()` function.

**Interfaces:**
- Produces:
  - `SC: Record<state, [r,g,b]>` and `SP: Record<state, {speed,energy}>` — exported for tests + for the page to know valid states.
  - `createOrbRenderer(glCanvas, overlayCanvas): { setState(s), setAmplitude(v), start(), resize() }` — encapsulates the WebGL program, the rAF loop, and the 2D overlay. `setState`/`setAmplitude` are driven by the page from WS frames.

**Note:** WebGL is unavailable in happy-dom, so `createOrbRenderer` is **not** unit-tested (verified manually). Only the pure `SC`/`SP` maps get a test (guards against typos in state keys).

- [ ] **Step 1: Write the failing test**

Create `ui/orb/lib/orb-renderer.test.js`:

```js
import { describe, it, expect } from "vitest";
import { SC, SP } from "./orb-renderer.js";

it("defines the four orb states with matching keys", () => {
  const states = ["idle", "listening", "thinking", "talking"];
  expect(Object.keys(SC).sort()).toEqual([...states].sort());
  expect(Object.keys(SP).sort()).toEqual([...states].sort());
});

it("colors are RGB triples", () => {
  for (const c of Object.values(SC)) { expect(c).toHaveLength(3); }
});
```

- [ ] **Step 2: Run — verify fail.**

- [ ] **Step 3: Implement `ui/orb/lib/orb-renderer.js`**

Move the orb's WebGL/animation code from `index.html:116-211` into this module. Export `SC` and `SP` as module constants and wrap the rest in `createOrbRenderer(c, ov)` which returns `{ setState, setAmplitude, start, resize }`. Keep the shader strings, `overlay`, and `frame` bodies **verbatim**; the only change is that `state`/`liveAmp` are set via the returned setters instead of closure-level WS handling (the page wires WS → setters in Task 13). `onStateChange` side effects (earcons, window mgmt) stay in the page, not the renderer — the renderer only renders.

(Full shader/animation source is in `index.html:116-211`; reproduce it inside `createOrbRenderer`, exposing `SC`/`SP` at module scope.)

- [ ] **Step 4: Run — verify pass.** Run: `npm --prefix ui test` → the `SC`/`SP` test passes.

- [ ] **Step 5: Commit**

```bash
git add ui/orb/lib/orb-renderer.js ui/orb/lib/orb-renderer.test.js
git commit -s -m "refactor(ui): extract orb WebGL renderer into lib/orb-renderer.js"
```

---

## Phase 3 — CSS architecture

### Task 10: Shared CSS — tokens, reset, base, and per-page entry sheets

**Files:**
- Create: `ui/orb/styles/tokens.css`, `reset.css`, `base.css`, `orb.css`, `chat.css`, `settings.css`, `about.css`
- Source: the `<style>` blocks of all four HTML files.

**Interfaces:**
- Produces per-page entry sheets each page `<link>`s (Tasks 13–19 wire the `<link>`). No JS, no test — verified visually.

- [ ] **Step 1: Create `styles/tokens.css`** — the shared semantic tokens + dark mode.

Collect the **union** of the semantic custom properties used by chat + settings + about (`--bg`, `--panel`, `--text`, `--muted`, `--line`, `--field`, `--field-line`, `--accent`, `--accent-bg`, `--danger`, `--danger-bg`, `--bubble`, `--seg`, `--seg-bg`, `--seg-active`, `--switch-off`, `--note-bg`, `--note-text`, `--btn-bg`, `--btn-line`, `--ok`, `--warn`) into one `:root` + one `@media (prefers-color-scheme: dark)` block, using the **light/dark values currently in `settings.html` and `about.html`** as the base. Add `:root { color-scheme: light dark; }`.

> Important: chat uses **translucent** `--bg`/`--panel` (for the drawer's blur) while settings/about use **opaque** values. Do NOT force one value. Put the shared/opaque values in `tokens.css`; chat's translucent overrides go in `chat.css` (Step 4). This keeps token *names* shared, values surface-specific.

- [ ] **Step 2: Create `styles/reset.css`**

```css
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
```

- [ ] **Step 3: Create `styles/base.css`** — element defaults common across pages (the body `font` stack, `-webkit-font-smoothing`). Move the shared `body { font: 13px/… }` declaration here; page-specific body layout (flex/justify, background) stays in the per-page sheet.

- [ ] **Step 4: Create the per-page entry sheets**

Each declares the layer order and imports shared + page CSS. Example `styles/chat.css`:

```css
@layer reset, base, components, utilities;
@import url("./tokens.css");
@import url("./reset.css") layer(reset);
@import url("./base.css") layer(base);

/* Chat-surface token overrides (translucent drawer bg + blur), moved verbatim
   from chat.html's :root / dark block. */
@layer base {
  :root { --bg: rgba(245,245,247,.86); --panel: #ffffff; /* …chat's values… */ }
  @media (prefers-color-scheme: dark) { :root { --bg: rgba(30,30,30,.82); /* … */ } }
}

/* Component styles are @imported by the component tasks (13–18) appending lines here,
   OR inline the page's remaining body/layout CSS below, moved verbatim. */
@layer components { /* page chrome: header, #log, footer, .inbar, etc. — moved verbatim */ }
```

Create `orb.css`, `settings.css`, `about.css` the same way, each moving that file's `<style>` body **verbatim** into the appropriate layer, and pulling shared tokens from `tokens.css`. Component-specific blocks (e.g. `.confirm`, `.choices`, `.card`, `.switch`) will be relocated into co-located component CSS during Tasks 13–18 and `@import`ed here; for this task, it is acceptable to land all of a page's CSS in its entry sheet first and split it out as components are built.

- [ ] **Step 5: Commit**

```bash
git add ui/orb/styles/
git commit -s -m "refactor(ui): extract shared CSS tokens + per-page entry sheets"
```

> No automated test. Visual parity is verified in Phase 5 (`make run`).

---

## Phase 4 — Components (light-DOM custom elements)

**Pattern for every component task:** create `components/<name>/<name>.js` (a `class extends HTMLElement` that binds existing markup in `connectedCallback`, cleans up document-level listeners in `disconnectedCallback`, registers via `customElements.define`), move its CSS into `components/<name>/<name>.css` (scoped by tag name) and `@import` it from the page entry sheet, and write a happy-dom `*.test.js` for its logic. Each task: write test → fail → implement → pass → commit.

Because the existing markup uses plain elements (e.g. `<div id="log">`), the migration wraps that markup in the new custom-element tag (e.g. `<chat-log id="log">…</chat-log>`) in the page's HTML (done in Phase 5), and the component enhances its own light-DOM children. Where a component is **purely rendered by JS today** (confirm/choices cards created via `createElement`), the custom element instead exposes a method/factory the page calls — no markup change needed.

> Tasks 11–18 each follow the standard 5-step TDD cycle. For brevity the shared cycle is stated once here; each task lists its specific test assertions, the source lines to move, and the exact public interface. **Do not skip writing the test code** — derive the test from the listed assertions.

### Task 11: `components/confirm-card`

**Files:** Create `components/confirm-card/confirm-card.{js,css,test.js}`. Source: `chat.html:438-473` (`showConfirm`/`clearConfirm`/`answer`) for the chat variant; `index.html:254-298` (`showCard`/`sendConfirm`/`clearCards`) for the orb variant.

**Interface (Produces):** A factory used by both surfaces, since the POST differs and the visuals differ:
- `lib/daemon.js` already owns the POST (`daemon.confirm({value})` chat / `daemon.confirm({answer})` orb).
- Chat: `<confirm-card>` custom element with a static `ConfirmCard.show(logEl, { text, kind, options, onAnswer })` that builds the inline card (read/write/danger tiers, optional `<select>`), appends to the log, and calls `onAnswer(value)` then removes itself. Move the chat card DOM + classes verbatim.
- Orb: keep as `orb-cards` (Task 17) since it also does window resizing + a11y; `confirm-card` here is **chat-only**. The orb's confirm rendering lives in `orb-cards`.

**Test assertions (happy-dom):**
- `ConfirmCard.show` appends a `.confirm` card with the right header text per `kind` (`read`→"Allow access", `write`→"Allow change", `danger`→"⚠️ Just checking").
- Clicking the yes button calls `onAnswer("yes")` (or the selected option value when `options` given) and removes the card.
- Clicking no calls `onAnswer("no")`.

**CSS:** move `.confirm*` rules from `chat.html`'s style block into `confirm-card.css`, scoped (`confirm-card .confirm { … }` or keep `.confirm` and import under `@layer components`).

Commit: `refactor(ui): add chat confirm-card component`.

### Task 12: `components/choices-card`

**Files:** Create `components/choices-card/choices-card.{js,css,test.js}`. Source: `chat.html:479-522` (`runAction`/`showChoices`). Orb's `showChoicesCard` stays in `orb-cards` (Task 17).

**Interface (Produces):** `<choices-card>` (chat) with `ChoicesCard.show(logEl, msg, { onCopy })` — builds the title + up-to-5 items, each with action buttons; a `copy` action copies client-side (via `lib/clipboard.js`), any other action calls `daemon.action(tool, args)` and shows status. Move the DOM build verbatim; replace inline `fetch(API + "/action"…)` with `daemon.action`, and `copyText` with the `lib/clipboard.js` import.

**Test assertions:** given a `msg` with 7 items, renders 5 + a "+2 more" footer; a copy action invokes `copyText`; a tool action calls `daemon.action` and disables buttons while pending.

Commit: `refactor(ui): add chat choices-card component`.

### Task 13: `components/context-meter`

**Files:** Create `components/context-meter/context-meter.{js,css,test.js}`. Source: `chat.html:645-712` (`renderContext`, `fmtK`/`fmtModel`/`fmtUSD` now from `lib/format.js`, `renderCtxDetail`, `resetCtx`, the ring math `CTX_CIRC`).

**Interface (Produces):** `<context-meter>` custom element wrapping the header ring markup. Methods: `update(msg)` (sets ring offset/color by `pct`, updates `%`, stores last ctx), `reset()` (clears), and toggles the detail card on click. Uses `fmtK/fmtModel/fmtUSD` from `lib/format.js`.

**Test assertions (happy-dom, no SVG layout needed):**
- `update({pct: 90, used: 1000, window: 2000, model: "claude-haiku-4-5"})` sets the percent text to `90%` and applies the danger color class/attr.
- color thresholds: ≤60 green, 61–85 amber, >85 danger (assert the chosen stroke value).
- `reset()` hides the meter and zeroes the percent.

Commit: `refactor(ui): add context-meter component`.

### Task 14: `components/folder-chip`

**Files:** Create `components/folder-chip/folder-chip.{js,css,test.js}`. Source: `chat.html:524-620` (`refreshWorkspace`, `renderWorkspace`, open/close/toggle modal, `revealWorkspace`, `changeFolder`, outside-click + Escape handling).

**Interface (Produces):** `<folder-chip>` wrapping the chip + modal markup. Methods: `refresh()` (GET `/workspace` via `daemon.workspace()`, update chip + modal), `renderFromEvent(msg)` (WS `workspace` frame → chip only). Uses `lib/tauri.js` (`revealInFinder`, `pickFolder`) and `daemon.setWorkspace`. Document-level outside-click/Escape listeners added in `connectedCallback`, removed in `disconnectedCallback`.

**Test assertions:** `renderFromEvent({path:"/a/b", name:"b"})` shows the chip with name "b"; an empty path hides it; toggling open then an outside click closes the modal.

Commit: `refactor(ui): add folder-chip component`.

### Task 15: `components/update-banner`

**Files:** Create `components/update-banner/update-banner.{js,css,test.js}`. Source: `chat.html:743-786` (`cmpVer` now from `lib/format.js`, `appVersion` from `lib/tauri.js`, `showUpdateBanner`, `checkForUpdate`).

**Interface (Produces):** `<update-banner>` with `check()` — fetches GitHub latest release, compares with `appVersion()` via `cmpVer`, shows the banner if newer and not dismissed (localStorage `jackUpdateDismissed`). "What's new" opens the release via `lib/tauri.js openExternal`. The GitHub fetch URL and `UPDATE_REPO` are preserved verbatim (allowed by CSP `connect-src https://api.github.com`).

**Test assertions:** with `appVersion`→"1.0.0" and a fetch stub returning `{tag_name:"v1.1.0", html_url:"…"}`, `check()` reveals the banner with text "Jack 1.1.0 is available"; if `cmpVer` says current, banner stays hidden; a previously dismissed version stays hidden.

Commit: `refactor(ui): add update-banner component`.

### Task 16: `components/chat-log`

**Files:** Create `components/chat-log/chat-log.{js,css,test.js}`. Source: `chat.html:256-364` (scroll/jump, `clampIfLong`, empty/welcome template + chips, `bubble`, typing indicator) and `622-643` (`renderStep`/`clearSteps`).

**Interface (Produces):** `<chat-log>` wrapping `#log`. Methods: `bubble(cls, text, md)` (renders a message; `md` uses `lib/markdown.js`; user pastes clamp via `clampIfLong`), `showTyping()`/`hideTyping()`, `renderStep(msg)`/`clearSteps()`, `showEmpty()`/`removeEmpty()` (welcome + chips, chips dispatch a `chip-send` CustomEvent the page listens for), `showInitializing()`, smart-scroll + jump button. Markdown links open via `lib/tauri.js openExternal`.

**Test assertions:** `bubble("jack","**hi**",true)` appends a `.msg.jack` whose HTML contains `<strong>`; `bubble("me", longText)` adds `.clamped` + a "Show more" button when scrollHeight>160 (stub `scrollHeight`); `showTyping` adds exactly one `.typing` and a second call does not duplicate; `renderStep({index:0,label:"x",status:"done"})` adds a row with the done class.

Commit: `refactor(ui): add chat-log component`.

### Task 17: `components/orb-cards`

**Files:** Create `components/orb-cards/orb-cards.{js,css,test.js}`. Source: `index.html:241-417` (notification confirm card, voice choices card, window sizing `enterCardMode`/`sizeWindowForCards`/`restoreSize`, keyboard focus trap, `announce`).

**Interface (Produces):** `<orb-cards>` managing the card stack under the orb. Methods: `showConfirm(text, kind, { onAnswer })`, `showChoices(msg)`, `clear()`. Uses `daemon.confirm({answer})` and `daemon.action`, `lib/tauri.js` window helpers for sizing. a11y live-region `announce` + Tab focus-trap + Esc handling moved verbatim.

**Test assertions (DOM only; window sizing is Tauri-gated and skipped in tests):** `showConfirm("Delete?","danger",{onAnswer})` appends a `.card.danger` with title "Confirm" and a hint mentioning "proceed"; clicking yes calls `onAnswer(true)` (and the page POSTs); `showChoices({title:"Top",items:[…4…]})` renders 3 + a "+1 more" hint; Esc on a confirm card answers false.

Commit: `refactor(ui): add orb-cards component`.

### Task 18: Settings components — `settings-tabs`, `model-picker`, `voice-download`, `permissions-list`, `access-list`, `report-sheet`

> These six are settings-only. Each is its own component folder + test, following the standard cycle. Group-committed per component (six commits) OR one commit per logical pair if small. Build them in this order; each is independently testable.

**18a — `components/settings-tabs`** — Source: `settings.html:308-317`. `<settings-tabs>` toggles `.panel`/`.active`; dispatches a `tab-change` CustomEvent with the tab name (page loads perms/voice lazily on change). Test: clicking a tab button activates the matching panel and fires `tab-change`.

**18b — `components/model-picker`** — Source: `settings.html:436-510`, `setProviderUI` (456-464), `populateClaudeModels`, `populateSttModels`, `selectedClaudeModel`, `selectedSttModel`, `populateModels` (592-607). `<model-picker>` owns provider switch + Claude/Ollama/STT selects with the `__custom__` escape hatch. Methods: `load(settings)`, `value()` → `{llm_provider, llm_model, anthropic_model, stt_engine, stt_model}`. Uses `daemon.models()`. Test: selecting `Custom…` reveals the custom input; `value()` returns the custom string when chosen; populating with a non-suggested current model preselects custom.

**18c — `components/voice-download`** — Source: `settings.html:319-403` (`loadVoiceStatus`, `startVoiceDownload` with its own progress WebSocket, watchdog, poll fallback, `finishVoiceDownload`, `_voiceCleanup`). `<voice-download>` owns the badge + progress bar. Method: `loadStatus()`, `start()`. **Keeps its own short-lived `new WebSocket(daemon.wsUrl)`** (must open before POSTing to catch early frames) — do not route through `daemon.on`. Uses `daemon.voiceStatus()` + `daemon.voiceDownload()`. Test (stub WebSocket + fetch + timers): a `voice_download` frame with `pct:50` sets the bar width to 50%; a `{done:true}` frame finishes and re-checks status; an `{error}` frame shows "Failed: …".

**18d — `components/permissions-list`** — Source: `settings.html:405-434`. `<permissions-list>` renders rows from `daemon.permissions()`, each "Open Settings" calls `daemon.openPermission(key)`. Has the `_permsLoading` guard against overlapping refreshes. Test: renders one `.perm-row` per permission with the right badge class; clicking a button calls `openPermission` with the key.

**18e — `components/access-list`** — Source: `settings.html:556-590` (`loadAccess`, `grantFolder`, `revokeFolder`). `<access-list>` lists granted folders with revoke, grants a new one by path + write checkbox. Uses `daemon.access()/grantAccess()/revokeAccess()`. Test: renders granted rows; revoke calls `revokeAccess(path)`; grant with empty path is a no-op.

**18f — `components/report-sheet`** — Source: `settings.html:650-747` (`buildReport`, `copyReport`, `openReport`/`closeReport`, `raiseIssue`, `reportIssue`, `revealReport`, `flashHint`). `<report-sheet>` owns the bottom-sheet + backdrop + issue flow. Uses `daemon.report()/reportFile()`, `lib/clipboard.js copyText`, `lib/tauri.js openExternal/revealInFinder`. Exposes `open()` (for the tray `#report` deep-link). Test: `open()` adds `.open` to pane + backdrop; copy calls `copyText` with the report text and flips the tip.

Commit (per sub-task): `refactor(ui): add <name> settings component`.

---

## Phase 5 — Page entries + thin HTML shells

Each page task: create `pages/<page>.js` that imports the lib + components it needs, registers nothing extra (components self-register on import), and contains the page-level glue that wasn't a component (composer, mode switch, global shortcuts, window drag, startup gate, WS wiring to components). Then rewrite the `.html` to a thin shell. Verify by loading the page (`make run`) against the parity checklist for that page.

### Task 19: `pages/about.js` + thin `about.html` (smallest first)

**Files:** Create `pages/about.js`; modify `about.html`. Source: `about.html:62-132`.

- [ ] **Step 1: Create `pages/about.js`** — move the about script, using `lib/dom.js` `$`, `lib/tauri.js` `invoke` (note: keep `invoke("app_version")` and `invoke("open_external", …)` as-is), and `lib/format.js` `cmpVer` in place of the local `cmp`/`parts`. Wire `#check` click → `checkUpdates`, call `loadVersion()`.

- [ ] **Step 2: Rewrite `about.html` as a thin shell** — keep the body markup (lines 50-60) verbatim; replace the `<style>` with `<link rel="stylesheet" href="./styles/about.css">` and the `<script>` with `<script type="module" src="./pages/about.js"></script>`.

- [ ] **Step 3: Verify** — `make run`, open About (tray → About), confirm version loads, "Check for Updates…" works (up-to-date + update-available paths), download link opens.

- [ ] **Step 4: Commit** — `refactor(ui): convert about.html to module + lib (thin shell)`.

### Task 20: `pages/settings.js` + thin `settings.html`

**Files:** Create `pages/settings.js`; modify `settings.html`. Source: the settings IIFE glue not already in components — `load()` (523-554), `save()` (616-639), `setStatus`, `setEnabled`, `saveSecret`, `updateWebUI`, the tray hooks (`window.__openReport`, `window.__openVoice`), hash deep-links, and wiring the components (`settings-tabs`, `model-picker`, `voice-download`, `permissions-list`, `access-list`, `report-sheet`).

- [ ] **Step 1: Create `pages/settings.js`** — orchestrate: on load, fetch settings via `daemon.settings()`, hand to `model-picker.load()`, set the checkbox states (`CHECKS`), web config, secrets state; `save()` collects `model-picker.value()` + checkboxes + listening fields and POSTs via `daemon.setSettings()` then `saveSecret` for keys. Wire `settings-tabs` `tab-change` → lazy `permissions-list.load()`/`voice-download.loadStatus()`. Preserve `window.__openReport`/`window.__openVoice` and the `#report`/`#voice` hash deep-links.

- [ ] **Step 2: Rewrite `settings.html`** — keep body markup (139-300), wrapping the relevant regions in the new component tags (`<settings-tabs>`, `<model-picker>`, `<voice-download>`, `<permissions-list>`, `<access-list>`, `<report-sheet>`) per each component's expected light-DOM children. Replace `<style>`/`<script>` with the `<link>` + module `<script>`.

- [ ] **Step 3: Verify** — `make run`, open Settings; run the **Settings** parity checklist from the spec (all tabs, load/save with retry, provider switch + model pickers + custom, secrets keep-blank, voice download progress/watchdog/poll/badge, permissions + open-pane, access grant/revoke, report sheet build/copy/reveal/issue, `#voice`/`#report` deep-links).

- [ ] **Step 4: Commit** — `refactor(ui): convert settings.html to modules + components (thin shell)`.

### Task 21: `pages/orb.js` + thin `index.html`

**Files:** Create `pages/orb.js`; modify `index.html`. Source: orb IIFE glue not in `orb-renderer`/`orb-cards` — WS wiring (215-234), `onStateChange` (485-503), auto-hide/`showOrb`/`hideOrb`/`ensureOnScreen`/`scheduleIdleHide`/`requestHide` (505-563), come-to-me glide (564-591), Tauri shell init (601-653), `playMode`/`playState` via `lib/earcons.js`, and the `window.__showOrb`/`window.__modeEarcon` hooks.

- [ ] **Step 1: Create `pages/orb.js`** — construct `createOrbRenderer($("gl"), $("ov"))`, `createEarcons({gain:0.30})`, and an `<orb-cards>` instance; subscribe via `daemon.on("state"…)`, `on("amplitude"…)`, `on("visibility"…)`, `on("confirm"…)` (mode≠chat → orb-cards.showConfirm), `on("confirm_clear"…)`, `on("choices"…)` (mode==voice → showChoices); call `daemon.connect()`. Move all window-management + auto-hide + glide glue verbatim, using `lib/tauri.js tauriWindow()`/`tauriApi`. Preserve `window.__showOrb`, `window.__modeEarcon`, the `conn` hint (driven by `daemon.onOpen/onClose`), and the `?ws=` override (now handled in `daemon`).

- [ ] **Step 2: Rewrite `index.html`** — keep body markup (94-101) verbatim; add `<orb-cards>` around the `#cards` container; replace `<style>`/`<script>` with the `<link href="./styles/orb.css">` + module `<script src="./pages/orb.js">`.

- [ ] **Step 3: Verify** — `make run`; with voice enabled, run the **Orb** parity checklist (four states + colors + overlays, amplitude, reconnect, notification confirm card via voice/click/keyboard with focus on safe choice, voice choices card, window sizing, auto-hide + wake re-show, come-to-me glide opt-in, state/mode earcons, on-screen clamp).

- [ ] **Step 4: Commit** — `refactor(ui): convert index.html (orb) to modules + components (thin shell)`.

### Task 22: `pages/chat.js` + thin `chat.html`

**Files:** Create `pages/chat.js`; modify `chat.html`. Source: the chat IIFE glue not in components — `submit()` (376-395), composer `resize`/`updateCounter`/placeholder hints (397-427), `lockInput`/`busy`, `newChat` (716-723), `connect`/WS wiring (725-741) now via `daemon.on`, debug button (788-821), mode switch `setMode`/`enableVoice`/`closeDrawer`/`voiceReady` (824-902), Escape/⌘W + header drag (898-915), focus mode-set, and `waitForReady` startup gate (926-938).

- [ ] **Step 1: Create `pages/chat.js`** — construct the `<chat-log>`, `<confirm-card>` usage, `<choices-card>` usage, `<context-meter>`, `<folder-chip>`, `<update-banner>`. Wire `daemon.on("confirm"…)` → `ConfirmCard.show(log, {…, onAnswer: v => daemon.confirm({value:v})})`, `on("confirm_clear")`, `on("context")` → `context-meter.update`, `on("choices", mode≠voice)` → `ChoicesCard.show`, `on("step")` → `chat-log.renderStep`, `on("workspace")` → `folder-chip.renderFromEvent`; `daemon.connect()`. Composer `submit()` calls `daemon.chat(text)`. Mode switch uses `daemon.setSettings({interaction_mode})` + `lib/tauri.js` (`closeChat`/`hideChat`/`openSettingsVoice`) + `createEarcons({gain:0.16})`. `waitForReady` uses `daemon.healthz()`. Listen for `chat-log`'s `chip-send` event → fill + submit. Preserve `window.__enableVoice`.

- [ ] **Step 2: Rewrite `chat.html`** — keep body markup (175-251) verbatim, wrapping regions in component tags: `<context-meter>`/`<folder-chip>`/`<update-banner>` around their existing markup, and `<chat-log id="log">` around `#log`. Replace `<style>`/`<script>` with `<link href="./styles/chat.css">` + module `<script src="./pages/chat.js">`.

- [ ] **Step 3: Verify** — `make run`; run the full **Chat** parity checklist (send turn, markdown, typing dots, smart scroll + jump, long-paste clamp, confirm card tiers + select, choices card copy + tool actions, folder chip + modal reveal/change/grants, context meter thresholds + detail + cost, new-chat reset, update banner, debug button in dev, mode switch + earcons, ⌘W/Esc close, header drag, "Starting…" gate).

- [ ] **Step 4: Commit** — `refactor(ui): convert chat.html to modules + components (thin shell)`.

---

## Phase 6 — Full verification + cleanup

### Task 23: Full parity pass + dead-code/CSP check

**Files:** none created; verification + small cleanups only.

- [ ] **Step 1:** Run `npm --prefix ui test` — all unit tests green.
- [ ] **Step 2:** Run `make run` and walk the **entire** parity checklist (all four pages) from the spec §9. Note any drift; fix in the owning component/page and re-commit.
- [ ] **Step 3:** Grep the four `.html` files to confirm no inline `<script>` logic or `<style>` blocks remain (only `<link>` + `<script type="module" src>`). Confirm `frontendDist`/`tauri.conf.json` unchanged.
- [ ] **Step 4:** Confirm no duplicated token blocks, earcon code, WS loops, `copyText`, `tauri()` accessor, or base-URL derivation remain outside `lib/`/`styles/` (grep for `new WebSocket`, `audioCtx`, `__TAURI__`, `127.0.0.1:8765`).
- [ ] **Step 5:** Run `make check` (Python lint/type/tests) to confirm the daemon side is untouched and green.
- [ ] **Step 6: Commit** any cleanups — `refactor(ui): final parity cleanup`.

### Task 24: Docs + PR

- [ ] **Step 1:** Fold the durable architecture decisions into `docs/architecture/design-reference.md` (the new `ui/orb/{lib,components,pages,styles}` layout + the "light-DOM HTML web components, no build, Vitest" conventions).
- [ ] **Step 2: Commit** — `docs: record UI architecture in design-reference`.
- [ ] **Step 3:** Open the tracking Issue (`[task]: Refactor UI into modular vanilla web components`) if not already open, and the PR with `Closes #NN`, squash-merge with a Conventional Commit title (e.g. `refactor(ui): modularize the webview into vanilla web components`).

---

## Self-Review

**Spec coverage** (spec §→task):
- §1 problem / duplication → Tasks 2–8 (each named duplicate has an owning lib module) ✓
- §4 decisions (vanilla, dev-only toolchain, big-bang, light-DOM, relative imports, tag-scoped CSS) → Task 1 (toolchain), Phase 4 (components), Phase 3 (CSS), Global Constraints ✓
- §5 structure → Tasks create exactly the `lib/`, `components/`, `pages/`, `styles/` layout + `ui/package.json` at `ui/` ✓
- §6 lib responsibilities → Tasks 2 (format), 3 (markdown), 4 (daemon), 5 (tauri), 6 (clipboard), 7 (earcons), 8 (dom), 9 (orb-renderer) — all eight present ✓
- §7 component model + inventory → Tasks 11–18 (chat: chat-log, confirm-card, choices-card, context-meter, folder-chip, update-banner; orb: orb-cards; settings: tabs, model-picker, voice-download, permissions-list, access-list, report-sheet) ✓
- §8 CSS architecture → Task 10 ✓
- §9 migration + parity checklist → Phase 5 per-page verify steps + Task 23 ✓
- §10 risks (node_modules out of frontendDist, verbatim moves) → Global Constraints + Task 1 + per-task "move verbatim" ✓
- §11 success criteria → Task 23 ✓
- §12 follow-ups (CSP tighten, @web/test-runner) → explicitly out of scope; design-reference note in Task 24 ✓

**Placeholder scan:** lib tasks (1–9) and CSS task (10) contain complete code/configs. Component/page tasks (11–22) intentionally use "move source lines X–Y verbatim + adapt these specific imports" rather than re-pasting hundreds of existing lines — the source exists in-repo and the exact interface, source line ranges, and test assertions are specified per task. This is the correct granularity for a behavior-preserving refactor; it is not a placeholder (no "TBD"/"handle edge cases"/undefined references).

**Type/name consistency:** `daemon` method names used in Tasks 11–22 (`confirm`, `action`, `chat`, `workspace`, `setWorkspace`, `settings`, `setSettings`, `voiceStatus`, `voiceDownload`, `permissions`, `openPermission`, `models`, `access`, `grantAccess`, `revokeAccess`, `secret`, `report`, `reportFile`, `reportConcise`, `healthz`, `on`, `onOpen`, `onClose`, `connect`, `wsUrl`, `base`) all match the interface defined in Task 4. `lib/format.js` exports (`fmtK`, `fmtModel`, `fmtUSD`, `cmpVer`), `lib/tauri.js` exports (`invoke`, `openExternal`, `revealInFinder`, `copyToClipboard`, `pickFolder`, `appVersion`, `closeChat`, `hideChat`, `openSettingsVoice`, `tauriWindow`, `hasTauri`), `lib/clipboard.js` (`copyText`), `lib/earcons.js` (`createEarcons`), `lib/dom.js` (`$`, `el`, `on`), `lib/orb-renderer.js` (`createOrbRenderer`, `SC`, `SP`), `lib/markdown.js` (`escapeHtml`, `renderMarkdown`) are referenced consistently by the consuming tasks. The `/confirm` payload divergence (`{value}` chat / `{answer}` orb) is preserved by passing the body object through `daemon.confirm(body)` (Tasks 11/17/22).
