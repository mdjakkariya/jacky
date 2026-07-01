import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../../lib/daemon.js", () => ({
  daemon: {
    meetingLast: vi.fn(),
    meetingStop: vi.fn(),
    meetingPause: vi.fn(),
    meetingResume: vi.fn(),
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
