# Jack UI architecture refactor — design

**Date:** 2026-06-28
**Status:** Approved design (pre-implementation)
**Scope:** The Tauri webview frontend under `ui/orb/` only. No daemon/engine changes.

> Tracking note (per `CONTRIBUTING.md`): planning/status lives in **GitHub Issues +
> Project #1**, not in markdown. This file is *durable design reference*, not a tracker.
> A `[task]:` issue should be opened so the implementation PR can `Closes #NN`; the
> durable decisions here should also be folded into
> `docs/architecture/design-reference.md`.

## 1. Problem

The UI grew organically from plain HTML/CSS into four monolithic documents, each a
single `<style>` block plus one large IIFE that mixes styling, DOM templating,
daemon networking, Tauri calls, audio, and business logic:

| File | Lines | Concerns crammed into one IIFE |
|---|---|---|
| `ui/orb/chat.html` | 942 | WS client, markdown render, scroll mgmt, confirm cards, choices cards, folder modal, context meter, update notifier, earcons, debug report, mode switching, window drag |
| `ui/orb/settings.html` | 763 | 14 fetch calls, tabs, voice-download progress (WS), permissions, access grants, model pickers, report sheet |
| `ui/orb/index.html` (orb) | 657 | WebGL plasma shader + 2D overlay, WS client, notification cards, window mgmt, earcons, auto-hide, "come-to-me" glide |
| `ui/orb/about.html` | 135 | small |

**Confirmed duplication across files** (the concrete waste this refactor removes):

- **Design tokens** — `:root` CSS custom properties + `prefers-color-scheme` dark
  block are redefined in `chat.html` and `settings.html`; the orb has its own.
- **Earcons** — `audioCtx`/`blip`/`earconsEnabled`/`playModeEarcon` (~40 lines) are
  near-identical in **both** `chat.html` and `index.html`.
- **WebSocket connect + reconnect loop** — hand-rolled separately in chat, orb, and
  settings.
- **API/WS base derivation**, the **`$` helper**, the **`tauri()` accessor**, and
  **`copyText`** (native + web fallback) — reimplemented per file.
- **`fetch(API + "/…")`** boilerplate inlined ~30× (14× in settings alone).
- **Confirm cards** and **choices/action cards** — same shape and same POST logic,
  reimplemented for orb vs chat.

There is **no test setup** for any of this; pure logic (markdown renderer, version
compare, token/cost formatting) is untestable while embedded in the IIFEs.

## 2. Goals / non-goals

**Goals**
- Eliminate the duplication above via a shared module layer.
- Make the UI modular, with small single-purpose files (good for humans *and* for an
  LLM working on one component without loading the whole codebase).
- Add a real, but dev-only, test setup for pure logic and component behavior.
- Preserve **100% of existing functionality and visuals** — this is a structural
  refactor, not a redesign.

**Non-goals**
- No UI/UX redesign, no new features.
- No runtime framework (React/Vue/Angular/Alpine/Lit/Stimulus) — see §4.
- No bundler / build step at runtime — Tauri keeps loading raw files.
- No change to the daemon's API or the headless-engine boundary (`CLAUDE.md`).
- Tightening the CSP to drop `unsafe-inline` is noted but **out of scope** for this pass.

## 3. Hard constraints that shaped the design

1. **No runtime build step.** `tauri.conf.json` sets `frontendDist: "../../orb"`,
   and windows load files directly (`WebviewUrl::App("chat.html")`). There is no
   `package.json`/bundler/node toolchain in the UI today.
2. **Strict CSP** (from `tauri.conf.json`):
   `script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'` — crucially
   **no `unsafe-eval`** and **no external CDN**. This *blocks* any library that
   evaluates expression strings via `eval`/`new Function()` (Alpine standard build,
   petite-vue, htmx's eval features) and forbids CDN-loaded code. Everything must be
   vendored locally — which aligns with the project's on-device-only rule.
3. **Modern system WebView (WKWebView).** Native ES modules, custom elements, CSS
   nesting, `@layer`, and `prefers-color-scheme` all work natively — no transpilation.
4. **Four separate documents**, not a SPA. Each is its own webview loading its own
   HTML file.

## 4. Decisions (chosen with the user)

| Decision | Choice | Rationale |
|---|---|---|
| Component/behavior layer | **Pure vanilla, zero runtime deps** | Matches the project's on-device/minimal-dependency ethos; most future-proof; most LLM-legible (standard JS, nothing to learn). The CSP already rules out eval-based libs; Lit's ergonomics need a transpile step we've ruled out; ~2,500 lines don't justify a framework. |
| Tooling | **Dev-only toolchain OK** | Runtime stays 100% buildless (Tauri loads raw files). A small `package.json` adds Vitest + happy-dom for tests/lint only — never required to run or ship the app. |
| Migration | **Big-bang rewrite** | All four pages restructured in one pass, protected by a parity checklist + pure-logic tests + manual verification (§9). |
| Component model | **Light-DOM "HTML web components"** | Custom elements that *wrap and enhance* existing markup. No Shadow DOM (so shared tokens + regular CSS reach in; forms/ARIA stay simple). Industry-convergent pattern (GitHub Catalyst, GOV.UK Frontend, the "HTML web components" canon). |
| Module wiring | **Relative ES-module imports, no import map** | Only 4 shallow pages; relative imports need zero maintenance. Import maps revisited only if depth grows. |
| Styling | **Shared `tokens.css` + native `@layer` + tag-name scoping** | One token source kills duplication; native CSS removes any preprocessor/build need. |

### Research basis (for the durable record)
- The CSP-lacks-`unsafe-eval` finding is the load-bearing constraint: it eliminates
  Alpine(standard)/petite-vue/htmx-eval and leaves vanilla, Web Components, Lit, and
  Stimulus as the only CSP-clean options.
- "HTML web components" (light-DOM, wrap-and-enhance) is the converged best practice
  for an app you fully control: GitHub (`@github/*` elements + Catalyst), GOV.UK
  Frontend (`data-module` + plain `.mjs` classes), and the canonical essays
  (Keith, Nielsen, Lazaroff, Ferdinandi). Skip Shadow DOM except for purely
  JS-generated widgets with no HTML fallback.
- Lit/Shoelace/Material/FAST are strong *organizational* references (one folder per
  component, co-located styles+tests, shared base class) but are TS/SCSS-authored
  with a build, and their buildless modes are CDN-based (blocked by our CSP).
- Testing: **Vitest + happy-dom** is the high-ROI baseline (pure functions +
  custom-element logic). `@web/test-runner` with a WebKit launcher is an optional
  later add for real-browser fidelity. There is **no driver for the real macOS
  WKWebView**, so final visual/behavioral parity is verified manually.

## 5. Target structure

```
ui/
  package.json            # dev-only: vitest + happy-dom (+ lint). Not shipped; not needed to run the app.
  vitest.config.js
  orb/                    # unchanged frontendDist
    index.html chat.html settings.html about.html   # thin shells: body markup + <link> css + <script type=module>
    styles/
      tokens.css          # ★ tiered tokens (primitive → semantic) + dark mode — the de-duplication win
      reset.css  base.css
      orb.css chat.css settings.css about.css         # per-page entry sheets: @layer order + @imports
    lib/                  # shared, framework-free, mostly pure → unit-tested
      daemon.js           # WS singleton (connect + reconnect + on(type,handler)) + typed fetch API client
      tauri.js            # __TAURI__ accessor + invoke wrappers (openExternal, revealInFinder, copyToClipboard, window)
      clipboard.js        # copyText (native + web fallback)
      earcons.js          # WebAudio cues (parameterized per surface)
      markdown.js         # chat markdown → escaped HTML (pure)
      format.js           # fmtK, fmtModel, fmtUSD, cmpVer (pure)
      dom.js              # $, el(), on(), delegate()
      orb-renderer.js     # WebGL plasma + 2D overlay (orb-only, isolated)
    components/           # one folder per component: <name>/<name>.{js,css,test.js}
      confirm-card/ choices-card/ context-meter/ folder-chip/ update-banner/
      chat-log/ orb-cards/ settings-tabs/ voice-download/ permissions-list/
      access-list/ model-picker/ report-sheet/
    pages/
      orb.js chat.js settings.js about.js
```

**Notes**
- `package.json`/`vitest.config.js` live at `ui/` (not `ui/orb/`) so the test
  toolchain and `node_modules` stay out of the served `frontendDist`. Vitest globs
  `orb/**/*.test.js`. `node_modules` is gitignored.
- The four `.html` files keep their current paths and the body markup they already
  have (light-DOM components enhance it). Only the inline `<style>`/`<script>` move
  out, so the documents become thin shells.

## 6. Shared core (`lib/`) — responsibilities and what each replaces

| Module | Responsibility | Replaces |
|---|---|---|
| `daemon.js` | Single source for the daemon connection. One auto-reconnecting WebSocket exposing `daemon.on(type, handler)`; a typed fetch client with named methods (`chat`, `confirm`, `runAction`, `workspace`, `settings`, `voiceStatus`, `permissions`, `access`, `report`, `healthz`, …); single `API`/`WS_URL` derivation. | The 3 hand-rolled `connect()`/reconnect loops, ~30 inline `fetch(API + "/…")` calls, and the repeated base-URL derivation. |
| `tauri.js` | `tauri()` accessor + thin wrappers around `invoke` (`openExternal`, `revealInFinder`, `copyToClipboard`, `pickFolder`, window helpers, `closeChat`/`hideChat`). | Scattered `window.__TAURI__.core`/`.window` access in all 4 files. |
| `clipboard.js` | `copyText(text)` — native invoke first, `navigator.clipboard` fallback. | Duplicated copy logic in chat + settings. |
| `earcons.js` | WebAudio context + `blip()` + mode/state earcons, gated by `localStorage` + enabled flag, parameterized so orb and chat pass their own gains/frequencies. | ~40 near-identical lines duplicated in orb + chat. |
| `markdown.js` | Dependency-free, HTML-escaping markdown → safe HTML for chat replies. **Pure.** | Inline renderer in chat (now testable). |
| `format.js` | `fmtK`, `fmtModel`, `fmtUSD`, `cmpVer`. **Pure.** | Inline formatters/version compare in chat (now testable). |
| `dom.js` | `$(id)`, `el(tag, props, children)`, `on(...)`, `delegate(...)`. | The repeated `$` helper and ad-hoc `createElement` chains. |
| `orb-renderer.js` | WebGL plasma core + 2D state overlay (idle/listening/thinking/talking), `requestAnimationFrame` loop, resize. Orb-only but isolated from page glue. | The ~150 lines of shader/animation embedded in the orb IIFE. |

## 7. Component model

Each component is a light-DOM custom element: `class extends HTMLElement` that
queries/binds **markup already in the page** in `connectedCallback`, removes
document-level listeners in `disconnectedCallback`, communicates outward via bubbling
`CustomEvent`s, and is registered with `customElements.define()`. No Shadow DOM, no
templating framework; dynamic DOM is built with `dom.js` helpers or `<template>`
clones. Pattern:

```js
// components/confirm-card/confirm-card.js
import { daemon } from "../../lib/daemon.js";
export class ConfirmCard extends HTMLElement {
  connectedCallback() {
    this.querySelector("[data-yes]")?.addEventListener("click", () => this.#answer("yes"));
    this.querySelector("[data-no]")?.addEventListener("click", () => this.#answer("no"));
  }
  #answer(v) { daemon.confirm(v); this.remove(); }
}
customElements.define("confirm-card", ConfirmCard);
```

**Shared logic vs. surface rendering.** The orb and chat both show confirm and
choices cards, but with different visuals (orb = macOS-notification card + window
resize + a11y live region + voice hint; chat = inline bubble in `#log`). The POST
logic lives **once** in `daemon.js` (`daemon.confirm`, `daemon.runAction`). Rendering
stays surface-specific. Where markup genuinely matches, a single component takes a
`surface`/variant attribute; where it doesn't, two components share the `lib/` logic.
The exact split is decided per component during implementation and recorded in the
plan.

### Indicative component inventory (per page)
- **Chat** (`pages/chat.js`): `chat-log` (bubbles, typing indicator, smart scroll,
  step trace, long-message clamp, empty/welcome state), `confirm-card`,
  `choices-card`, `context-meter` (token ring + detail card), `folder-chip` (active
  folder chip + modal), `update-banner`; plus page glue for the composer, mode
  switch, and global shortcuts.
- **Orb** (`pages/orb.js`): `orb-renderer` (lib), `orb-cards` (notification card
  stack: confirm + voice-search choices, window sizing, focus trap, a11y),
  earcons/auto-hide/come-to-me glue.
- **Settings** (`pages/settings.js`): `settings-tabs`, `model-picker` (provider +
  Claude/Ollama/STT model selects with custom escape hatch), `voice-download`
  (WS-driven progress + watchdog + poll fallback), `permissions-list`, `access-list`
  (grant/revoke folders), `report-sheet` (debug report bottom sheet + issue flow).
- **About** (`pages/about.js`): small; version + links.

## 8. CSS architecture

- **`styles/tokens.css`** — tiered tokens: primitive values (`--blue-500`, spacing,
  radii) → semantic tokens components reference (`--bg`, `--panel`, `--text`,
  `--muted`, `--line`, `--field`, `--accent`, `--danger`, …) → dark mode reassigns
  **only** semantic tokens in a `@media (prefers-color-scheme: dark)` block. Set
  `color-scheme: light dark` on `:root`. `<link>`ed by every page. **Defined once.**
- **Per-page entry sheet** (`chat.css`, etc.) declares layer order
  `@layer reset, base, components, utilities;` then `@import`s `tokens.css`,
  `reset.css`, `base.css`, and the co-located component CSS the page uses
  (`@import url("../components/confirm-card/confirm-card.css") layer(components);`).
- **Component CSS** is co-located (`components/<name>/<name>.css`) and scoped by tag
  name with native nesting (`confirm-card { … &[data-state="…"] { … } }`).
- **Surface-specific values** (chat's translucent blur bg, settings' opaque bg, the
  orb's dark gradient + notification cards) are per-page overrides of the shared
  semantic token *names* — the names are shared, the values differ by surface.
- No preprocessor, no build; `@import`/`@layer`/nesting are native and CSP-safe
  (same-origin CSS).

## 9. Migration plan (big-bang, parity-protected)

One pass, all four pages, with guardrails so nothing breaks:

1. **Scaffold** `lib/`, `styles/`, `components/`, `pages/` and the dev toolchain
   (`ui/package.json`, `vitest.config.js`, a `make ui-test` target).
2. **Extract `lib/` modules** with logic moved **verbatim** (same behavior), starting
   with the pure ones (`markdown.js`, `format.js`) so their tests land first.
3. **Move CSS verbatim** into `tokens.css` + base + co-located component CSS; collapse
   the duplicated token blocks into the single `tokens.css`.
4. **Build components** that wrap the **same markup** already in each HTML file, so
   rendering is pixel-identical.
5. **Thin the HTML shells**: replace inline `<style>`/`<script>` with `<link>` +
   `<script type="module" src="./pages/<page>.js">`; keep body markup.
6. **Verify** against the parity checklist below via `make run`.

### Parity checklist (manual, every interaction)
- **Chat:** send a turn; markdown rendering; typing dots; smart scroll + jump button;
  long-paste clamp ("Show more"); confirm card (read/write/danger tiers + select
  options); choices/action card (copy + tool actions); folder chip + modal (reveal,
  change folder, grants); context meter (ring color thresholds, detail card, cost
  row); new-chat reset; update banner; debug button (dev); mode switch (Voice/Chat) +
  earcons; ⌘W/Esc close; header drag; "Starting…" gate.
- **Orb:** four states render (idle/listening/thinking/talking) with correct colors +
  overlays; amplitude reactivity; reconnect; notification confirm card (voice
  + click + keyboard, focus on safe choice); voice choices card; window sizing for
  cards; auto-hide + wake re-show; come-to-me glide (opt-in); state/mode earcons;
  on-screen clamp.
- **Settings:** all tabs; load/save with daemon-starting retry; provider switch
  (local/cloud) + model pickers + custom escape hatch; secrets save (keep-blank);
  voice download (progress bar, watchdog slide, poll fallback, badge refresh);
  permissions list + open-pane; access grant/revoke; report sheet (build, copy,
  reveal, open issue); deep links (`#voice`, `#report`).
- **About:** renders; version; links.

### Regression safety
- Pure-logic Vitest tests (`markdown.js`, `format.js`) lock behavior.
- happy-dom tests cover custom-element attribute/lifecycle/event wiring.
- Manual `make run` pass over the full checklist before the PR.

## 10. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Big-bang misses a behavior | Move logic verbatim; explicit parity checklist; pure-logic tests; manual `make run`. |
| Earcon/card behavior differs orb vs chat after unifying | Parameterize `earcons.js`; keep surface-specific card rendering; verify both surfaces. |
| `node_modules` leaking into the shipped app | Toolchain lives at `ui/` (outside `frontendDist`); `node_modules` gitignored. |
| Module load order / waterfall | Relative imports; add `<link rel="modulepreload">` for hot-path modules if needed (local origin makes this negligible). |
| Visual drift from CSS reorg | Move CSS verbatim first; single `tokens.css`; diff rendering against current build. |

## 11. Success criteria

- No duplicated design tokens, earcon code, WS/reconnect loops, `copyText`, `tauri()`
  accessor, or `fetch` base-URL derivation remain — each exists once in `lib/`/`styles/`.
- Each page's logic is split into small single-purpose component + lib files; no file
  is a monolithic mixed-concern IIFE.
- `make ui-test` runs Vitest green (pure-logic + component-logic tests).
- Every item on the parity checklist behaves identically to `main`, verified via
  `make run`.
- Tauri config, daemon API, and the headless-engine boundary are unchanged.

## 12. Follow-ups (out of scope here)
- Tighten CSP to drop `unsafe-inline` once all scripts/styles are external.
- Optional `@web/test-runner` + WebKit launcher for real-browser component fidelity.
- Fold these decisions into `docs/architecture/design-reference.md`.
