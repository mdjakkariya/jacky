import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({
  daemon: {
    meetingLast: vi.fn(),
    meetingStop: vi.fn(),
    meetingPause: vi.fn(),
    meetingResume: vi.fn(),
    meetingReveal: vi.fn().mockResolvedValue({ ok: true }),
    settings: vi.fn().mockResolvedValue({ llm_provider: "ollama", llm_model: "qwen3:8b" }),
  },
}));
vi.mock("../../lib/clipboard.js", () => ({ copyText: vi.fn().mockResolvedValue(true) }));

import { daemon } from "../../lib/daemon.js";
import { parseMinutes, renderMeeting, renderMinutes, clearMeeting } from "./meeting-card.js";

const SAMPLE_MD = `# Weekly Sync
- **Date:** Jun 30, 2026
- **Duration:** 06:12
- **Attendees:** You + Priya + Carlos

## Summary
The team reviewed launch readiness. The release is targeted for the 14th, gated on completing the API documentation and resolving the onboarding blocker.

## Decisions
- Release date set to July 14th
- Go/no-go call owned by the PM

## Action items
- Priya — Finish the API documentation by Thursday EOD
- Carlos — Resolve the onboarding blocker
- You — Own the go/no-go launch call

## Open questions
- Is the recording feature covered by the existing legal review?
`;

function makeLog() {
  const log = document.createElement("div");
  log.scroll = () => {};
  document.body.appendChild(log);
  return log;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.useRealTimers();
});

// ── parseMinutes ─────────────────────────────────────────────────────────────

describe("parseMinutes", () => {
  it("extracts the title from H1", () => {
    const r = parseMinutes(SAMPLE_MD);
    expect(r.title).toBe("Weekly Sync");
  });

  it("extracts meta fields", () => {
    const r = parseMinutes(SAMPLE_MD);
    expect(r.date).toBe("Jun 30, 2026");
    expect(r.duration).toBe("06:12");
    expect(r.attendees).toBe("You + Priya + Carlos");
  });

  it("extracts summary paragraph", () => {
    const r = parseMinutes(SAMPLE_MD);
    expect(r.summary).toContain("launch readiness");
  });

  it("extracts decisions", () => {
    const r = parseMinutes(SAMPLE_MD);
    expect(r.decisions.length).toBe(2);
    expect(r.decisions[0]).toContain("July 14th");
  });

  it("extracts actions with owner split on em dash", () => {
    const r = parseMinutes(SAMPLE_MD);
    expect(r.actions.length).toBe(3);
    expect(r.actions[0].owner).toBe("Priya");
    expect(r.actions[0].task).toContain("API documentation");
    expect(r.actions[2].owner).toBe("You");
  });

  it("action without em dash gets empty owner", () => {
    const md = "# T\n## Action items\n- Just a bare task\n";
    const r = parseMinutes(md);
    expect(r.actions[0].owner).toBe("");
    expect(r.actions[0].task).toBe("Just a bare task");
  });

  it("lone '- None' produces empty list", () => {
    const md = "# T\n## Decisions\n- None\n## Action items\n- None\n## Open questions\n- None\n";
    const r = parseMinutes(md);
    expect(r.decisions).toEqual([]);
    expect(r.actions).toEqual([]);
    expect(r.openQuestions).toEqual([]);
  });

  it("extracts open questions", () => {
    const r = parseMinutes(SAMPLE_MD);
    expect(r.openQuestions.length).toBe(1);
    expect(r.openQuestions[0]).toContain("legal review");
  });
});

// ── recording card ────────────────────────────────────────────────────────────

describe("renderMeeting — recording state", () => {
  it("renders exactly one card with the meeting title", () => {
    const log = makeLog();
    renderMeeting(log, { state: "recording", elapsed_s: 0, mic_only: false, paused: false, title: "Sprint Review" });
    const cards = log.querySelectorAll("#meeting-card");
    expect(cards.length).toBe(1);
    expect(log.querySelector(".ttl").textContent).toContain("Sprint Review");
    clearMeeting(log);
  });

  it("Stop button calls daemon.meetingStop", () => {
    const log = makeLog();
    renderMeeting(log, { state: "recording", elapsed_s: 0, mic_only: false, paused: false, title: "T" });
    log.querySelector(".stop-btn").click();
    expect(daemon.meetingStop).toHaveBeenCalledOnce();
    clearMeeting(log);
  });

  it("a second recording event updates the same card (no duplicate)", () => {
    const log = makeLog();
    renderMeeting(log, { state: "recording", elapsed_s: 0, mic_only: false, paused: false, title: "First" });
    renderMeeting(log, { state: "recording", elapsed_s: 5, mic_only: false, paused: false, title: "Second" });
    const cards = log.querySelectorAll("#meeting-card");
    expect(cards.length).toBe(1);
    expect(log.querySelector(".ttl").textContent).toContain("Second");
    clearMeeting(log);
  });

  it("paused event shows PAUSED pill and pause class", () => {
    const log = makeLog();
    renderMeeting(log, { state: "recording", elapsed_s: 0, mic_only: false, paused: false, title: "T" });
    renderMeeting(log, { state: "paused",    elapsed_s: 10, mic_only: false, paused: true,  title: "T" });
    const card = log.querySelector("#meeting-card");
    expect(card.classList.contains("paused")).toBe(true);
    clearMeeting(log);
  });

  it("miconly adds miconly class to the card", () => {
    const log = makeLog();
    renderMeeting(log, { state: "recording", elapsed_s: 0, mic_only: true, paused: false, title: "T" });
    expect(log.querySelector("#meeting-card").classList.contains("miconly")).toBe(true);
    clearMeeting(log);
  });
});

// ── done state ────────────────────────────────────────────────────────────────

describe("renderMeeting — done state", () => {
  it("calls daemon.meetingLast", async () => {
    daemon.meetingLast.mockResolvedValue({ ok: true, dir: "/tmp/mtg", minutes_md: SAMPLE_MD });
    const log = makeLog();
    renderMeeting(log, { state: "done", elapsed_s: 0, mic_only: false, paused: false, title: "T" });
    expect(daemon.meetingLast).toHaveBeenCalledOnce();
    // Flush the promise so renderMinutes runs
    await vi.runAllTimersAsync();
    const minCard = log.querySelector("#meeting-minutes");
    expect(minCard).not.toBeNull();
  });

  it("minutes card contains parsed summary text", async () => {
    daemon.meetingLast.mockResolvedValue({ ok: true, dir: "/tmp/mtg", minutes_md: SAMPLE_MD });
    const log = makeLog();
    renderMeeting(log, { state: "done", elapsed_s: 0, mic_only: false, paused: false, title: "T" });
    await vi.runAllTimersAsync();
    const sum = log.querySelector(".sum");
    expect(sum).not.toBeNull();
    expect(sum.textContent).toContain("launch readiness");
  });

  it("minutes card has a decisions stat chip", async () => {
    daemon.meetingLast.mockResolvedValue({ ok: true, dir: "/tmp/mtg", minutes_md: SAMPLE_MD });
    const log = makeLog();
    renderMeeting(log, { state: "done", elapsed_s: 0, mic_only: false, paused: false, title: "T" });
    await vi.runAllTimersAsync();
    const stats = log.querySelectorAll(".stat");
    const decisionsChip = Array.from(stats).find((s) => s.textContent.includes("decision"));
    expect(decisionsChip).not.toBeNull();
    expect(decisionsChip.querySelector("b").textContent).toBe("2");
  });

  it("does not render minutes card when meetingLast returns ok:false", async () => {
    daemon.meetingLast.mockResolvedValue({ ok: false });
    const log = makeLog();
    renderMeeting(log, { state: "done", elapsed_s: 0, mic_only: false, paused: false, title: "T" });
    await vi.runAllTimersAsync();
    expect(log.querySelector("#meeting-minutes")).toBeNull();
  });
});

// ── minutes card interactions ─────────────────────────────────────────────────

describe("renderMinutes — footer + expand", () => {
  const R = { ok: true, id: "2026-07-01-1508-standup", dir: "/x/meetings/2026-07-01-1508-standup", minutes_md: SAMPLE_MD };

  it("has no 'Open minutes' button", () => {
    const log = makeLog();
    renderMinutes(log, R);
    const labels = Array.from(log.querySelectorAll(".min-foot .fbtn")).map((b) => b.textContent);
    expect(labels.some((t) => /open minutes/i.test(t))).toBe(false);
  });

  it("Reveal folder calls daemon.meetingReveal with the meeting id", () => {
    const log = makeLog();
    renderMinutes(log, R);
    const reveal = Array.from(log.querySelectorAll(".min-foot .fbtn")).find((b) => /reveal/i.test(b.textContent));
    reveal.click();
    expect(daemon.meetingReveal).toHaveBeenCalledWith("2026-07-01-1508-standup");
  });

  it("crumb shows the unique folder name from dir", () => {
    const log = makeLog();
    renderMinutes(log, R);
    expect(log.querySelector(".min-h .crumb").textContent).toContain("2026-07-01-1508-standup");
  });

  it("expand toggle flips the .expanded class on the card", () => {
    const log = makeLog();
    renderMinutes(log, R);
    const card = log.querySelector("#meeting-minutes");
    const btn = log.querySelector(".min-expand");
    expect(card.classList.contains("expanded")).toBe(false);
    btn.click();
    expect(card.classList.contains("expanded")).toBe(true);
    btn.click();
    expect(card.classList.contains("expanded")).toBe(false);
  });

  it("renders all action items in the actions section", () => {
    const log = makeLog();
    renderMinutes(log, R); // SAMPLE_MD has 3 action items
    expect(log.querySelectorAll(".mtg-section[data-section='actions'] .ai").length).toBe(3);
  });

  it("renders the open questions as a list (not just a count)", () => {
    const log = makeLog();
    renderMinutes(log, R); // SAMPLE_MD has 1 open question
    const items = log.querySelectorAll(".mtg-section[data-section='openq'] .oq");
    expect(items.length).toBe(1);
    expect(items[0].textContent).toContain("legal review");
  });

  it("renders the decisions as a list", () => {
    const log = makeLog();
    renderMinutes(log, R); // SAMPLE_MD has 2 decisions
    expect(log.querySelectorAll(".mtg-section[data-section='decisions'] .dec").length).toBe(2);
  });

  it("clicking a count pill reveals ONLY that section — not the whole card", () => {
    const log = makeLog();
    renderMinutes(log, R);
    const card = log.querySelector("#meeting-minutes");
    const oqPill = log.querySelector(".mtg-stats .stat[data-section='openq']");
    const oqSec = log.querySelector(".mtg-section[data-section='openq']");
    expect(oqSec.classList.contains("open")).toBe(false);
    oqPill.click();
    expect(oqSec.classList.contains("open")).toBe(true);
    // The full-card expand belongs to the chevron, not the pills (issue #38).
    expect(card.classList.contains("expanded")).toBe(false);
  });

  it("clicking the decisions pill reveals the decisions list", () => {
    const log = makeLog();
    renderMinutes(log, R);
    const decPill = log.querySelector(".mtg-stats .stat[data-section='decisions']");
    const decSec = log.querySelector(".mtg-section[data-section='decisions']");
    decPill.click();
    expect(decSec.classList.contains("open")).toBe(true);
    expect(decSec.querySelectorAll(".dec").length).toBe(2);
  });

  it("opening one section closes the others (shows only the clicked one)", () => {
    const log = makeLog();
    renderMinutes(log, R);
    log.querySelector(".mtg-stats .stat[data-section='decisions']").click();
    log.querySelector(".mtg-stats .stat[data-section='openq']").click();
    expect(log.querySelector(".mtg-section[data-section='decisions']").classList.contains("open")).toBe(false);
    expect(log.querySelector(".mtg-section[data-section='openq']").classList.contains("open")).toBe(true);
  });

  it("highlights the clicked pill as active (and clears it on the others)", () => {
    const log = makeLog();
    renderMinutes(log, R);
    const decPill = log.querySelector(".mtg-stats .stat[data-section='decisions']");
    const oqPill = log.querySelector(".mtg-stats .stat[data-section='openq']");
    decPill.click();
    expect(decPill.classList.contains("active")).toBe(true);
    oqPill.click();
    expect(oqPill.classList.contains("active")).toBe(true);
    expect(decPill.classList.contains("active")).toBe(false);
    // Clicking the active pill again closes its section and clears the highlight.
    oqPill.click();
    expect(oqPill.classList.contains("active")).toBe(false);
  });

  it("a zero-count pill is disabled and opens no section", () => {
    const md = "# T\n## Decisions\n- None\n## Action items\n- None\n## Open questions\n- Only one\n";
    const log = makeLog();
    renderMinutes(log, { ok: true, id: "m", dir: "/x/m", minutes_md: md });
    const actPill = log.querySelector(".mtg-stats .stat[data-section='actions']");
    expect(actPill.disabled).toBe(true);
    expect(log.querySelector(".mtg-section[data-section='actions']")).toBeNull();
  });
});

// ── processing card — model label (bug #38) ────────────────────────────────────

describe("showProcessingCard — model label", () => {
  it("shows the cloud model from settings, not a hardcoded local model", async () => {
    daemon.settings.mockResolvedValue({
      llm_provider: "anthropic",
      anthropic_model: "claude-haiku-4-5",
      llm_model: "qwen2.5:3b",
    });
    const log = makeLog();
    renderMeeting(log, { state: "summarizing", elapsed_s: 0, mic_only: false, paused: false, title: "T" });
    await vi.runAllTimersAsync();
    const steps = log.querySelectorAll("#meeting-processing li");
    const writeStep = steps[steps.length - 1];
    expect(writeStep.textContent).toContain("Writing minutes");
    expect(writeStep.querySelector(".meta").textContent).toBe("haiku-4.5");
  });

  it("shows the local model name when provider is ollama", async () => {
    daemon.settings.mockResolvedValue({ llm_provider: "ollama", llm_model: "qwen2.5:3b" });
    const log = makeLog();
    renderMeeting(log, { state: "summarizing", elapsed_s: 0, mic_only: false, paused: false, title: "T" });
    await vi.runAllTimersAsync();
    const steps = log.querySelectorAll("#meeting-processing li");
    const writeStep = steps[steps.length - 1];
    expect(writeStep.querySelector(".meta").textContent).toBe("qwen2.5:3b");
  });
});

// ── clearMeeting ──────────────────────────────────────────────────────────────

describe("clearMeeting", () => {
  it("removes the recording card", () => {
    const log = makeLog();
    renderMeeting(log, { state: "recording", elapsed_s: 0, mic_only: false, paused: false, title: "T" });
    clearMeeting(log);
    expect(log.querySelector("#meeting-card")).toBeNull();
  });

  it("removes the minutes card", () => {
    const log = makeLog();
    renderMinutes(log, { ok: true, dir: "/tmp/mtg", minutes_md: SAMPLE_MD });
    clearMeeting(log);
    expect(log.querySelector("#meeting-minutes")).toBeNull();
  });
});
