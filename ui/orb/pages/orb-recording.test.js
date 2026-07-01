import { describe, it, expect, vi } from "vitest";

// Mock daemon before importing orb.js
const daemonMock = {
  mcpServers: vi.fn().mockResolvedValue({ servers: [] }),
  on: vi.fn(),
  onOpen: vi.fn(),
  onClose: vi.fn(),
  connect: vi.fn(),
};

vi.mock("../lib/daemon.js", () => ({
  daemon: daemonMock,
}));

const setMeetingStateMock = vi.fn();

vi.mock("../lib/orb-renderer.js", () => ({
  createOrbRenderer: vi.fn(() => ({
    state: "idle",
    start: vi.fn(),
    resize: vi.fn(),
    setState: vi.fn(),
    setAmplitude: vi.fn(),
    setMeetingState: setMeetingStateMock,
  })),
  SC: { idle: true, listening: true, thinking: true, talking: true },
}));

vi.mock("../lib/earcons.js", () => ({
  createEarcons: vi.fn(() => ({ resumeOnGesture: vi.fn(), playMode: vi.fn(), playState: vi.fn() })),
}));

vi.mock("../components/orb-cards/orb-cards.js", () => ({}));

// Set up the DOM before the module-level import so orb.js can find its elements.
document.body.innerHTML = `
  <div class="stage">
    <canvas id="gl"></canvas>
    <canvas id="ov"></canvas>
    <div id="conn">connecting…</div>
    <div id="net-ring" class="net-ring" aria-hidden="true"></div>
    <div id="net-conn" class="net-conn" aria-hidden="true">↗</div>
    <div id="net-cap" class="orbcap" aria-hidden="true"></div>
  </div>
  <orb-cards id="cards"></orb-cards>
  <div id="a11y-live" class="a11y-live" role="status" aria-live="assertive"></div>
`;

// Import orb.js once (module is cached; handlers are registered at import time).
await import("./orb.js");

// Extract the "meeting" handler registered by orb.js
const meetingCallback = daemonMock.on.mock.calls.find((c) => c[0] === "meeting")?.[1];

describe("Orb Recording Indicator", () => {
  it("calls setMeetingState with active=true, paused=false for recording state", () => {
    expect(meetingCallback).toBeDefined();
    setMeetingStateMock.mockClear();

    meetingCallback({ state: "recording", elapsed_s: 12, recorded_s: 12, mic_only: false, paused: false, title: "Test" });

    expect(setMeetingStateMock).toHaveBeenCalledOnce();
    expect(setMeetingStateMock).toHaveBeenCalledWith({ active: true, paused: false, elapsedS: 12 });
  });

  it("calls setMeetingState with active=true, paused=true for paused state", () => {
    expect(meetingCallback).toBeDefined();
    setMeetingStateMock.mockClear();

    meetingCallback({ state: "paused", elapsed_s: 30, recorded_s: 30, mic_only: false, paused: true, title: "Test" });

    expect(setMeetingStateMock).toHaveBeenCalledOnce();
    expect(setMeetingStateMock).toHaveBeenCalledWith({ active: true, paused: true, elapsedS: 30 });
  });

  it("calls setMeetingState with active=true for transcribing state", () => {
    expect(meetingCallback).toBeDefined();
    setMeetingStateMock.mockClear();

    meetingCallback({ state: "transcribing", elapsed_s: 60, recorded_s: 60, mic_only: false, paused: false, title: "Test" });

    expect(setMeetingStateMock).toHaveBeenCalledOnce();
    expect(setMeetingStateMock).toHaveBeenCalledWith({ active: true, paused: false, elapsedS: 60 });
  });

  it("calls setMeetingState with active=true for summarizing state", () => {
    expect(meetingCallback).toBeDefined();
    setMeetingStateMock.mockClear();

    meetingCallback({ state: "summarizing", elapsed_s: 65, recorded_s: 65, mic_only: false, paused: false, title: "Test" });

    expect(setMeetingStateMock).toHaveBeenCalledOnce();
    expect(setMeetingStateMock).toHaveBeenCalledWith({ active: true, paused: false, elapsedS: 65 });
  });

  it("calls setMeetingState with active=false for done state", () => {
    expect(meetingCallback).toBeDefined();
    setMeetingStateMock.mockClear();

    meetingCallback({ state: "done", elapsed_s: 70, recorded_s: 70, mic_only: false, paused: false, title: "Test" });

    expect(setMeetingStateMock).toHaveBeenCalledOnce();
    expect(setMeetingStateMock).toHaveBeenCalledWith({ active: false, paused: false, elapsedS: 70 });
  });

  it("calls setMeetingState with active=false for idle state", () => {
    expect(meetingCallback).toBeDefined();
    setMeetingStateMock.mockClear();

    meetingCallback({ state: "idle", elapsed_s: 0, recorded_s: 0, mic_only: false, paused: false, title: "" });

    expect(setMeetingStateMock).toHaveBeenCalledOnce();
    expect(setMeetingStateMock).toHaveBeenCalledWith({ active: false, paused: false, elapsedS: 0 });
  });

  it("uses 0 for elapsedS when elapsed_s is undefined", () => {
    expect(meetingCallback).toBeDefined();
    setMeetingStateMock.mockClear();

    meetingCallback({ state: "recording" });

    expect(setMeetingStateMock).toHaveBeenCalledWith({ active: true, paused: false, elapsedS: 0 });
  });
});
