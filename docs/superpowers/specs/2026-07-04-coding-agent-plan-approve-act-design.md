# Coding agent — plan → approve → act autonomy (design)

Part of the coding-agent epic (**#53**, later slice). Umbrella spec:
`docs/superpowers/specs/2026-07-03-coding-agent-design.md` (§ autonomy, lines
160–166, 220–226). This document details the concrete flow and protocol the umbrella
spec left at a high level.

## Problem

`jack "…"` today spawns a **headless** coder daemon (stdout → a log file, no TTY, no
mic) and does one blocking `POST /chat`. Two consequences make the coder unusable as
a real coding agent:

1. **No approval path.** `run_command` is `Risk.DESTRUCTIVE`, so the shared
   `PermissionGate` tries to confirm it — but the daemon's confirmer
   (`TerminalConfirmer`) has no TTY, so the prompt gets an EOF and the command is
   **silently denied**. There is no way for the user to say "yes, run it."
2. **The autonomy dial is inert.** `settings.coding_autonomy` (`plan`/`confirm`/`auto`)
   exists in config but nothing reads it for a coding turn — deferred in #51 because
   the gate is shared with the voice assistant and must not be changed for it.
3. **Replies truncate.** `_DEFAULT_LLM_MAX_TOKENS = 120` is shared across profiles, so
   any coder plan or reply is cut off at ~120 tokens.

## Goal

A coding request becomes a **two-phase turn** — a read-only **plan** phase that
proposes a todo list, an explicit **approval** in the CLI's real terminal, then an
**act** phase that carries the plan out, auto-applying edits and *asking the CLI (not
silently stopping)* whenever a command needs a decision. The autonomy dial selects
the flow, and all of it is confined to the **coder build** — the voice assistant and
its shared gate are untouched.

Scope is inline `jack "text"` only. The bare-`jack` REPL, streaming, TUI, PTY, drawer
plan cards, and `jack undo`/`sessions`/`review` are explicitly out of scope (next
slices) — but the suspend/resume primitive built here is what those reuse.

## The core primitive: a suspending turn

A coding turn runs on a daemon **worker thread** that can **park awaiting an answer
from the CLI** and **resume** when it arrives, over a single in-process channel:

```
TurnChannel
  ask(event: dict) -> str      # producer side (worker): out_q.put(event); return in_q.get()
  done(reply: str) -> None     #   worker: out_q.put({"status": "done", "reply": reply})
  poll() -> dict               # consumer side (HTTP): out_q.get()  (blocks for next event)
  answer(value: str) -> None   #   HTTP: in_q.put(value)
```

- `out_q` carries **turn events** to the HTTP layer: `{"status": "plan", …}`,
  `{"status": "pending", …}`, or `{"status": "done", …}`.
- `in_q` carries the **user's answer** back to the parked worker.
- Two `queue.Queue`s; no asyncio in the channel itself (the worker is a plain thread).

The HTTP handlers become a producer/consumer handoff:

- `POST /coder/turn {text}` → start the worker (reject if one is already running),
  then `return channel.poll()` (the first event: a plan, a pending confirm, or done).
- `POST /coder/reply {value, text?}` → `channel.answer(value)` then `channel.poll()`
  (the next event). Repeats until `status == "done"`.

Because the worker's Python call stack *is* the continuation, no turn state has to be
serialized between requests. The CLI therefore stays **single-threaded**: one request,
then a loop of answer→poll until done.

## Components

### `agent/coder_turn.py` — `CoderTurnDriver` (new)

Owns the plan→approve→act flow for one coder turn. Built once in the coder branch of
`app.build()`, wrapping the existing `AgentHarness` and `PermissionGate`.

- `start(text) -> dict`: spawn the worker thread running `_run(text)`; return the
  first `channel.poll()`.
- `reply(value, text="") -> dict`: `channel.answer(...)`; return `channel.poll()`.
- `_run(text)` (on the worker thread), driven by `settings.coding_autonomy`:
  - **plan**: run the plan phase (read-only) → `channel.ask({"status":"plan","reply":…,"todo":…})`
    → on `"approve"` run the act phase; on `"reject"` finish with a cancelled reply;
    on `"refine"` re-run the plan phase with the feedback and ask again (loop).
  - **confirm** / **auto**: skip the plan phase; run the act phase directly.
  - Always ends with `channel.done(final_reply)`.
- Holds `current_channel` so the shared `SuspendingConfirmer` can reach the active
  turn's channel (there is only ever one — the turn lock guarantees it).

The driver serialises on the orchestrator's existing `_turn_lock`, so a coder turn
can't interleave with anything else. Concurrency rule: if the worker is **actively
running** (executing a tool, not parked), a second `POST /coder/turn` returns
`{"status":"error","reply":"a turn is already running"}`; if the worker is **parked**
awaiting an answer (a stale turn from a CLI that died), a fresh `POST /coder/turn`
reclaims it — it delivers a synthetic reject to unblock the old worker, then starts
the new turn. This guarantees no permanently-stuck thread without interrupting a turn
mid-tool.

### `SuspendingConfirmer` (new, in `agent/coder_turn.py`)

A `Confirmer` implementation — the coder daemon's gate confirmer, replacing
`TerminalConfirmer`. It lives in `agent/coder_turn.py` (not `tools/permission.py`)
because it references the driver's active channel; keeping it beside the driver avoids
a `permission.py → coder_turn.py` import cycle. It has no UI of its own; it defers to
the active turn's channel:

- `confirm(prompt, kind) -> bool`: `driver.current_channel.ask({"status":"pending","kind":kind,"prompt":prompt})`
  → returns `answer in {"yes","y","once"}`.
- `confirm_action(prompt, kind) -> str`: same, mapping the answer to `"once"`/`""`
  (no session grants in the coder — every ask is per-turn).
- `choose(...)`: returns the least-privilege default (coder tools don't use choices).

### Plan phase — read-only executor

Plan runs `harness.run_turn(planning_prompt + text, execute=read_only_executor)`:

```
def read_only_executor(call):
    if (gate.risk_of(call.name) or Risk.READ_ONLY) >= Risk.WRITE:
        return ToolResult(call.name,
            "Planning phase — not executed. Add this step to your todo list "
            "instead; you'll carry it out after the plan is approved.", ok=False)
    return gate.execute(call)          # reads (read_file/grep/glob/repo_map) run normally
```

Nothing can mutate the repo before approval. A plan-phase system-prompt addendum
instructs the coder to explore with read tools and end with a concise **numbered todo
list** of the edits/commands it will make, scaled to task size (one line for a tiny
change, several for a large one). The turn ends naturally when the model returns text
with no tool calls — that text is the plan.

`todo` in the response is a **best-effort** structured extraction of the numbered
lines (for future UIs); the authoritative plan is the reply text. Robustness over a
strict format — a local model that returns prose still works, the CLI just prints it.

### Act phase — escalate-to-ask executor

Act runs `harness.run_turn(act_prompt, execute=act_executor)` on the continued
session (the model sees its own approved todo list in history; `act_prompt` is a short
"the plan is approved — carry it out now, step by step"):

```
def act_executor(call):
    if call.name == "run_command":
        decision, reason = classify_command(cmd, allowlist, blocklist)
        if decision == "block":   return ToolResult(call.name, f"blocked for safety ({reason})", ok=False)
        if decision == "allow":   return gate.execute(call, pre_authorized=True)   # run, no ask
        # decision == "confirm":  fall through → gate asks the CLI via SuspendingConfirmer
    return gate.execute(call)
```

- Reads and in-cwd edits (`Risk.WRITE`) never trip the gate's threshold → **auto-apply**.
- `run_command` is classified by the existing `classify_command`:
  **block** → hard refuse (never asks); **allow** (user allowlist) → run unattended;
  **confirm** (anything else) → the gate confirms via the `SuspendingConfirmer`, i.e.
  **the turn suspends and asks the CLI** — the user's "ask, don't just stop."
- Bounded throughout by the **cwd jail** (broker) and the **start-of-turn checkpoint**
  (already taken by the harness `checkpoint` hook → undoable later via `jack undo`).
  The hook fires at the start of *each* `run_turn`, so the plan phase's checkpoint is a
  harmless no-op (read-only, nothing changed) and the act phase's — taken right before
  the first edit — is the meaningful undo point.

### `PermissionGate.execute(call, *, pre_authorized=False)` (small addition)

`pre_authorized=True` skips the confirmation branch but still classifies, dispatches,
and **audits** (decision `ALLOWED`, detail `"pre-authorized (allowlist)"`). Defaults
`False`, so the assistant and every existing caller are byte-for-byte unaffected. Keeps
the gate the single dispatch + audit point even for allowlisted commands.

### Autonomy dial — coder-only

`settings.coding_autonomy` selects the flow **only** inside `CoderTurnDriver` (which
only exists in the coder build). The shared gate and assistant path are never
consulted for the dial.

| mode | plan phase? | act-phase commands |
|------|-------------|--------------------|
| `plan` (default) | yes → approve/reject/refine | allow→run, confirm→**ask CLI**, block→refuse |
| `confirm` | no | every non-allow, non-block command **asks the CLI** |
| `auto` | no | allow/confirm both run unattended; only `block` refuses |

`auto` differs from `confirm` only in that `classify_command == "confirm"` commands
run without asking (still bounded by blocklist + jail + checkpoint).

### Config — coder output budget

Add `coder_llm_max_tokens: int = 4096` to `Settings`. In the coder branch of
`_build_llm`, apply it (e.g. `replace(settings, llm_max_tokens=settings.coder_llm_max_tokens)`)
so coder replies/plans don't truncate. The assistant's `llm_max_tokens = 120` is
untouched.

### Daemon protocol

Two new routes, wired **only** in the coder daemon (assistant daemon leaves them
`None` → 404/graceful), mirroring the existing `on_chat` callback style:

- `POST /coder/turn` — body `{text}` → `on_coder_turn(text)` → a status dict.
- `POST /coder/reply` — body `{value, text?}` → `on_coder_reply(value, text)` → a status dict.

Both run the driver call via `asyncio.to_thread` so the event loop stays responsive
while the worker is parked. Response union:

```
{"status": "plan",    "reply": "<numbered plan>", "todo": ["…", "…"]}   # awaiting approve/reject/refine
{"status": "pending", "kind": "command", "prompt": "Run `pytest -q`?"}   # awaiting yes/no
{"status": "done",    "reply": "<final summary>"}                        # finished
{"status": "error",   "reply": "<message>"}                             # turn busy / bad state
```

### CLI drive loop (`cli.py`)

```
resp = post("/coder/turn", {"text": text})
while resp["status"] in ("plan", "pending"):
    ans = _prompt(resp)                       # real terminal (input())
    resp = post("/coder/reply", ans)          # {"value": …, "text"?: …}
print(resp.get("reply", ""))
```

- `status == "plan"`: print the plan; prompt `Apply this plan? [y]es / [n]o / [e]dit`.
  `y`→`{"value":"approve"}`; `n`→`{"value":"reject"}`; `e`→read a line of feedback→
  `{"value":"refine","text":feedback}`.
- `status == "pending"`: print `prompt`; `[y/N]` → `{"value":"yes"|"no"}`.
- Ctrl-C at any prompt sends `{"value":"reject"}` (best-effort) and exits 130, so the
  parked worker unblocks instead of leaking.

### Abandonment / cancellation

The worker parks on `in_q.get()` with no timeout (a user may think for minutes). It is
unblocked by: (a) a `/coder/reply`, (b) Ctrl-C in the CLI sending a reject, or (c) a
fresh `/coder/turn` — which, if a turn is already parked, first delivers a synthetic
reject to the old worker before starting. This guarantees no permanently-stuck thread.

## Data flow (default `plan` mode)

```
jack "add a retry to fetch()"
  └─ POST /coder/turn {text}
       daemon worker: plan phase (read-only) → numbered todo → channel.ask(plan)
  ◄─ {status: plan, reply, todo}
  CLI prints plan, asks [y/n/e] → y
  └─ POST /coder/reply {value: approve}
       worker resumes → act phase:
         edit src/net.py            (WRITE → auto-apply)
         run_command "pytest -q"    (classify → confirm → channel.ask(pending))
  ◄─ {status: pending, prompt: "Run `pytest -q`?"}
  CLI asks [y/N] → y
  └─ POST /coder/reply {value: yes}
       worker resumes → pytest runs → model summarises → channel.done(reply)
  ◄─ {status: done, reply}
  CLI prints the summary.
```

## Error handling

- Tools still never raise out of dispatch; failures are `ToolResult(ok=False)` fed back
  to the model, exactly as today. The read-only and act executors also return
  `ToolResult`s, never raise.
- A worker exception is caught, logged (`[coder]`), and turned into
  `{"status":"done","reply":"<friendly error>"}` so the CLI always terminates.
- LLM-unavailable (Ollama down, etc.) reuses the orchestrator's existing
  `_llm_unavailable_message` mapping.
- The checkpoint hook failure is already swallowed by the harness (never aborts a turn).

## Logging

`[coder]` logger, seam events at INFO: `plan proposed steps=N`, `plan approved`,
`plan rejected`, `plan refined`, `command ask cmd=…`, `command auto-run cmd=…`,
`turn done`. No per-token / per-line logs.

## Testing (pure, hardware-free — no real LLM/socket/subprocess)

- **TurnChannel / driver**: a fake "model" whose scripted tool calls and final text
  drive the worker; assert the exact event sequence for each dial mode, and that
  approve/reject/refine transition correctly. Threading exercised with real queues but
  bounded timeouts.
- **read_only_executor**: writes/commands rejected (not dispatched), reads pass through.
- **act_executor**: `classify_command` block/allow/confirm each routed correctly
  (block→refuse, allow→pre_authorized dispatch, confirm→gate/confirmer ask).
- **PermissionGate.execute(pre_authorized=True)**: dispatches + audits without calling
  the confirmer; `pre_authorized=False` path unchanged (regression).
- **SuspendingConfirmer**: `confirm`/`confirm_action` map channel answers correctly.
- **CLI drive loop**: against a fake `post` returning scripted plan→pending→done;
  assert prompts and the `value` payloads; Ctrl-C path sends reject.
- **Config**: coder build raises `llm_max_tokens`; assistant build stays at 120.

## Touch-points (files)

- `src/autobot/config.py` — add `coder_llm_max_tokens`.
- `src/autobot/agent/coder_turn.py` — **new**: `TurnChannel`, `SuspendingConfirmer`,
  `CoderTurnDriver`, the two executors, the plan/act prompt addenda.
- `src/autobot/tools/permission.py` — `execute(..., pre_authorized=False)`.
- `src/autobot/orchestrator/state_machine.py` — `start_coder_turn` / `reply_coder_turn`
  delegating to the driver (coder build only).
- `src/autobot/app.py` — coder branch: build the driver + `SuspendingConfirmer`; apply
  `coder_llm_max_tokens`; wire the daemon callbacks.
- `src/autobot/daemon/server.py` — `/coder/turn`, `/coder/reply` routes (coder only).
- `src/autobot/daemon/runner.py` — wire `on_coder_turn`/`on_coder_reply` for the coder
  profile.
- `src/autobot/cli.py` — the plan→approve→act drive loop.

## Constraints (repo conventions)

On-device only; English only; the permission gate stays the single choke point (the
`pre_authorized` path still audits). Conventional Commits, **no attribution trailer**,
**no competing-tool/product names** in code or docs, explicit `git add` paths,
`make check` green, `from __future__ import annotations`, mypy strict, Google-style
docstrings, line length 100, tools return strings and never raise. Branch off
`feat/coding-agent`; when done, squash-merge into it (integration-branch strategy),
do not open a per-slice PR to `main`.

## Deferred (not this slice)

Bare-`jack` REPL (next; reuses the drive loop + `_prompt`), token streaming, TUI, PTY,
drawer plan/diff/checkpoint cards, `jack undo`/`sessions`/`review`, repo-map context
injection at turn start, per-todo-step checkpoints, session-persisted plan objects.
