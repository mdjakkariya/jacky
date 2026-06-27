# Multi-step plans — chain tools in one turn (design)

Design reference for **GitHub issue #6 — "Multi-step plans (chain tools in one
turn)"** (Track 1, capability growth). Records *how* the multi-step tool loop
works after this change and *why* it's built this way. Status/tracking lives in
the issue, not here.

> Goal in one line: let Jack call a tool, look at its result, then decide the
> next action — all within a single user turn — on **both** the local (Ollama)
> and cloud (Anthropic) backends, and show the user what's happening as it runs.

## 1. Context — the problem

The pipeline hands the `LanguageModel` an **executor** (wired to the
`PermissionGate`); the model plans tool calls, the executor runs them, results
feed back. Today the two backends behave differently:

| | **Ollama (local, default)** | **Anthropic (cloud, opt-in)** |
|---|---|---|
| Tool loop | **One round only** — runs the first round of tool calls, then forces a final reply; any tool calls in the follow-up response are **silently dropped** (`ollama_llm.py` `run_turn`) | Real loop, `_MAX_TOOL_ROUNDS = 8` |
| Sequential chaining | ❌ (round 2's tool calls discarded) | ✅ loops until no tool calls |
| Anti-thrash (don't re-run a failed call) | ❌ none | ✅ `failed` dict + `all_repeat` early stop |
| At round cap | n/a | canned "Sorry, that took too many steps." |
| Multi-round tests | ❌ none | ✅ thorough in `tests/unit/test_anthropic_llm.py` |

So on the **default** backend, "find my latest screenshot and open it" can't
work: it lists files (round 1) but the call to open the chosen file (round 2) is
thrown away. The issue's framing — "the engine already supports multi-step tool
calls; this covers exercising, hardening, and prompting" — is only true for the
cloud path. The local path needs the loop built; both paths need hardening; and
the user should *see/hear* progress instead of waiting through dead-air.

## 2. Goals / non-goals

**Goals**

1. Local Ollama `run_turn` gains a true multi-round loop at parity with the
   cloud path (cap, anti-thrash, clean per-call tool messages, stop-when-done).
2. Both backends, at the round cap, **force one final answer** synthesized from
   what happened (not a canned apology).
3. Surface each step as it runs, in **chat** (a live step trace) and **voice**
   (a short spoken cue per step) — to remove dead-air on multi-step turns.
4. A single general prompt principle that teaches multi-step behavior without
   over-calling tools on trivial requests.
5. Tests that exercise chaining, anti-thrash, the cap, and step emission.

**Non-goals (tracked separately)**

- An OpenRouter / OpenAI-compatible provider backend — a separate, privacy-gated
  integration (see §8). The cloud-LLM plan already anticipates it.
- Extracting a shared loop-policy runner across backends ("2b") — deferred until
  a 3rd backend lands, so it's factored from real working loops (see §8).
- Reducing the tool surface (~40 registered tools vs the ~20–30 where selection
  accuracy degrades on small models) — real, but distinct from the loop.
- An explicit up-front "plan" object the model emits and narrates.

## 3. Approach

**Approach 1 — two layers, no protocol change.** Each backend keeps owning its
own `run_turn` loop (their message shapes and context/caching logic genuinely
differ); step-surfacing happens at the one orchestrator seam every tool call
already flows through. The `LanguageModel` protocol is untouched.

### 3.1 Part 1 — loop parity + hardening (`src/autobot/llm/`)

**`OllamaLanguageModel.run_turn`** replaces its single-round block with a bounded
multi-round loop mirroring the cloud path:

```text
_MAX_TOOL_ROUNDS = 8                       # parity with the cloud backend
messages = self._assemble(user_msg)        # existing assembly + preflight compaction
failed: dict[str, str] = {}                # anti-thrash: name+args -> failure text
for _ in range(_MAX_TOOL_ROUNDS):
    resp  = self._chat(messages)           # non-streaming, with tools (unchanged)
    msg   = _get(resp, "message")
    calls = normalize_tool_calls(msg)
    messages.append(_to_message_dict(msg)) # record assistant turn faithfully
    if not calls:
        reply = message_content(msg); break          # stop-when-no-calls
    all_repeat, last_fail = True, ""
    for call in calls:                                # in call order (native API has no tool_call_id)
        key = call.name + "\0" + json.dumps(call.arguments, sort_keys=True, default=str)
        if key in failed:
            out = failed[key]; last_fail = out         # reuse, don't re-run
        else:
            all_repeat = False
            result = execute(call)                     # through the permission gate
            out = result.content
            if not result.ok:
                failed[key] = out; last_fail = out
        messages.append({"role": "tool", "tool_name": call.name, "content": out})
    if all_repeat:                                     # model only retried failures
        reply = last_fail or "I couldn't complete that, so I stopped."; break
else:
    reply = self._final_answer_no_tools(messages)      # force a final answer at the cap
# existing post-turn: persist (append-only), trim_history, _compact_if_needed, _report_usage
```

Design notes:

- **Anti-thrash is mandatory, not optional.** The dominant multi-step failure of
  the default model (qwen3:8b) is *re-emitting the same call* (~32.5% of
  multi-turn failures — see §6). The exact-`(name,args)` dedupe + `all_repeat`
  stop is the same mechanism the cloud path already proves out.
- **Message shape is already correct.** Native Ollama tool results are
  `{"role":"tool","tool_name":…,"content":…}` (no `tool_call_id`); results are
  paired to calls by **order**, so we append one per call, in call order — which
  the existing code already does. Non-streaming is kept (safer for tool loops).
- **History** stays append-only and faithful (assistant `tool_calls` + each tool
  result), so a later turn ("close it") can resolve what an earlier turn did, and
  the KV-cache prefix stays stable. Pre/post compaction is unchanged.
- **Errors** propagate as today: a mid-loop `_chat` failure surfaces to the
  orchestrator, which already maps `ConnectionError` to "Ollama isn't running."
  Tools that already ran stay run (same as the cloud path).

**`AnthropicLanguageModel.run_turn`** — the loop's `else` (cap-exhausted) branch
stops returning the canned "Sorry, that took too many steps." and instead makes
**one final no-tools call** to synthesize a reply from history (the
smolagents/LangChain `"generate"` pattern). The canned line remains the fallback
if that final call fails.

Both backends get a small `_final_answer_no_tools(...)` helper: one model call
over the current history with **tools disabled**, returning its text (or the
canned fallback if it errors). Ollama already supports this via
`self._chat(messages, with_tools=False)`; Anthropic makes a `messages.create`
with no `tools` parameter — the same tool-free call shape `_summarize` already
uses — reusing the existing window-trim + cache-breakpoint handling. The history
at the cap ends with a complete `tool_use`/`tool_result` pairing, so a tool-free
call yields a clean final text reply.

### 3.2 Part 2 — step surfacing in chat + voice

**The seam:** `Orchestrator._execute(call)` — every tool call on **both** backends
flows through it, so emission here is backend-agnostic and needs no change in
`llm/`.

- **New event.** Add `StepEvent` to `core/events.py` and `EventBus.publish_step`.
  Wire shape:
  ```json
  {"type": "step", "index": 0, "tool": "search_files",
   "label": "Searching files…", "status": "running"}
  ```
  `status` is `running` | `done` | `failed`. A new `on_step` callback is added to
  `Orchestrator.__init__` and wired `daemon/runner.py → app.build() → Orchestrator`
  exactly like the existing `on_context` callback.
- **Emit in `_execute`.** Emit a `running` step *before* the gate runs (instant
  feedback), then a `done`/`failed` step *after* (from `result.ok`). The label
  reuses the tool's own `ack` template via `_format_ack`/`ack_of` (e.g. "Opening
  Spotify…"), falling back to a humanized tool name. A per-turn step index is
  reset at the start of each turn.
- **Chat (`ui/orb/chat.html`).** Add an `else if (msg.type === "step")` branch to
  the WebSocket handler (the same `msg.type` switch the orb's `index.html` uses)
  → render a small live step trace ("Searching files… ✓ → Opening… ✓") that
  collapses when the final reply arrives.
- **Voice.** Speak a **short cue per step, deduped**: a brief spoken line as each
  step starts, *skipping* tools whose `ack` is `""` (silent by design, e.g.
  `dismiss`) and *not repeating* the same phrase back-to-back. This replaces the
  current once-per-turn filler (`self._acknowledged`). It is gated by
  `speak_acknowledgements` and suppressed in text mode, as today.

### 3.3 Part 3 — prompting (one general principle)

Per the repo convention (keep `SYSTEM_PROMPT` to short, general principles;
per-tool guidance lives in each `ToolSpec.description`), add **one** principle to
`SYSTEM_PROMPT` in `ollama_llm.py` (shared by both backends):

> *"You can take several steps in one turn: call a tool, look at its result, then
> decide the next action — use one tool's output to inform the next. Only call a
> tool when you actually need to act or look something up; otherwise just answer.
> Don't repeat a call that already failed. Once you have what you need, give your
> final answer."*

This addresses, at the principle level, the three documented qwen3 multi-step
failure modes: wrong-params (→ "look at the result"), redundant-loop (→ "don't
repeat a failed call"), and premature-stop / over-calling (→ "only when you need
… then give your final answer").

The qwen3 **thinking-mode** tool-loop hazard (§6) is *noted, not changed* here:
anti-thrash + the cap absorb the instability; toggling `llm_think` is a separate
tuning decision.

## 4. Decisions (locked)

- **Architecture:** Approach 1 (per-backend loops now; shared "2b" runner later).
- **Round cap:** `8` on both backends (parity; everyday Mac chores are short
  chains; sits within the 8–25 industry range — see §6).
- **At cap:** force one final no-tools answer; canned line only as fallback.
- **Voice cadence:** short cue per step, deduped, skipping silent tools.
- **OpenRouter / OpenAI-compatible provider:** out of scope; its own issue.

## 5. Testing

- **New `tests/unit/test_ollama_llm.py`** (mirrors the `FakeClient` pattern in
  `test_anthropic_llm.py`, fully offline): multi-round chain (tool → result →
  second tool → reply); anti-thrash (a repeated failing call runs **once**); cap
  → force-final-answer; no-tools → text; history keeps tool messages across
  turns.
- **Cloud:** at the cap, a final answer is synthesized (not the canned line).
- **Events:** `EventBus.publish_step` / `StepEvent.message()` serialization.
- **Orchestrator:** `_execute` emits `running` then `done`/`failed` to a fake
  `on_step` sink, for both an ok and a failing tool; voice dedupe skips silent
  tools and back-to-back repeats.
- `make check` (ruff, ruff-format, mypy strict, pytest) stays green.

## 6. Research & prior art (cited)

Findings from a multi-source review of how production systems run multi-step
tool loops, and how the default local model behaves.

**The loop shape is universal.** `call model → if tool calls: execute all, append
one result per call, recall → repeat → stop when the response has no tool calls.`
Verified across Ollama
([docs](https://docs.ollama.com/capabilities/tool-calling)), OpenAI
([function-calling guide](https://developers.openai.com/api/docs/guides/function-calling)),
Anthropic
([how tool use works](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works) —
loop while `stop_reason == "tool_use"`), LangGraph (`tools_condition`),
smolagents, and Pydantic AI.

**Iteration caps across the field** — `8` is well within range:

| System | Cap | At cap |
|---|---|---|
| OpenAI Agents SDK | 10 | raises `MaxTurnsExceeded` |
| LangChain `AgentExecutor` | 15 | `"force"` string / `"generate"` 1 more LLM call |
| smolagents | 20 | **forces final answer** (1 more LLM call) |
| LangGraph `recursion_limit` | 25 → 1000 (v1.0.6) | `GraphRecursionError` (prebuilt degrades gracefully) |
| Pydantic AI | 50 requests | `UsageLimitExceeded` |
| Open Interpreter (Py) | none (`while True`) | — (known infinite-loop bugs w/ local models) |

Sources: [OpenAI Agents SDK `run_config.py`](https://raw.githubusercontent.com/openai/openai-agents-python/main/src/agents/run_config.py),
[LangChain `AgentExecutor`](https://github.com/langchain-ai/langchain/blob/master/libs/langchain/langchain_classic/agents/agent.py),
[smolagents `agents.py`](https://github.com/huggingface/smolagents/blob/main/src/smolagents/agents.py),
[LangGraph recursion limit](https://docs.langchain.com/oss/python/langgraph/graph-api#recursion-limit),
[Pydantic AI `usage.py`](https://raw.githubusercontent.com/pydantic/pydantic-ai/main/pydantic_ai_slim/pydantic_ai/usage.py).

**Force-a-final-answer at the cap** is the better-UX pattern (smolagents
`_handle_max_steps_reached`, LangChain `early_stopping_method="generate"`) vs. a
canned string — hence decision §4.

**Anti-thrash isn't built into the mainstream frameworks** (LangChain / smolagents
/ Open Interpreter rely on caps only); newer agents add it by hashing
`(tool, args)` and stopping after N repeats (Kilocode pauses after 3 identical
consecutive calls; OpenFang/hermes SHA-256 signatures; OpenClaw aborts on a
recurring `(tool,args,result)`; Pydantic-AI `StuckLoopDetection`). The cloud path
already does exact-repeat dedup; we mirror it on the local path.
Refs: [stop LLM agent looping](https://dev.to/alanwest/how-to-stop-your-llm-agent-from-looping-itself-into-oblivion-27eh),
[AWS prevent reasoning loops](https://dev.to/aws/how-to-prevent-ai-agent-reasoning-loops-from-wasting-tokens-2652).

**The real risk — qwen3:8b (the default) at multi-step.** It is the best-in-class
*small* tool-caller (F1 0.933 ≈ claude-3-haiku;
[Docker eval](https://www.docker.com/blog/local-llm-tool-calling-a-practical-evaluation/)),
but single-turn ~95% vs **multi-turn ~22–34%** (BFCL), error-compounding. Failure
breakdown: wrong params 39.5%, **redundant-loop / re-emits same call 32.5%**,
premature give-up 13.2%
([BFCL multi-turn thread](https://forums.developer.nvidia.com/t/help-fine-tuned-qwen3-8b-for-tool-calling-single-turn-is-95-but-multi-turn-bfcl-is-stuck-at-10-22-out-of-ideas/373441)).
Thinking mode is a tool-loop hazard: Qwen warns against ReAct/stopword templates,
and community consensus disables thinking for loop stability
([Qwen function-call docs](https://qwen.readthedocs.io/en/latest/framework/function_call.html)).
Non-streaming is safer for tool loops (Jack already non-streams); native tool
results have **no `tool_call_id`**, so result *ordering* matters (Jack already
orders).

**Prompting (general principles).** Plan→act→observe one tool at a time; answer
directly when no tool is needed; don't guess args; persist until resolved then
stop; don't repeat a failed call. Tool *descriptions* are the primary behavioral
lever (kept per-tool). 8B models exhibit "eager invocation" (tools on greetings)
— Jack's prompt already guards this. Refs:
[Anthropic — building effective agents](https://www.anthropic.com/engineering/building-effective-agents),
[ReAct](https://arxiv.org/abs/2210.03629),
[OpenAI GPT-4.1 prompting guide](https://developers.openai.com/cookbook/examples/gpt4-1_prompting_guide).

## 7. Risks

- **qwen3 multi-step weakness** — mitigated by anti-thrash + cap + the prompt
  principle + force-final-answer. Accepted: the local model will sometimes still
  mis-sequence; the loop degrades gracefully rather than failing hard.
- **Voice chattiness** — bounded by per-step dedupe and skipping silent tools.
- **Context growth across rounds** — existing compaction handles it; cap 8
  bounds worst-case growth.

## 8. Future work (separate issues)

- **OpenRouter / OpenAI-compatible provider backend** — a third LLM backend
  (OpenAI-style `tool_calls` + `tool_call_id`; a generic "OpenAI-compatible"
  client would also cover gateways and local OpenAI-compat servers). **Privacy
  gate:** off-device; needs the same opt-in / off-by-default / disclosed
  treatment as the Anthropic exception, with a stronger disclosure that data
  transits a third-party relay. Key in Keychain (`autobot.secrets`).
- **Shared loop-policy runner ("2b")** — extract cap + anti-thrash + stop +
  force-final + step-emission into one helper that each backend calls via small
  hooks (keeping context/caching inside each backend). Do this when backend #3
  lands, factored from the real working loops.
- **Tool-surface reduction** — ~40 tools exceeds the ~20–30 where small-model
  selection accuracy degrades; consider consolidation/namespacing.
