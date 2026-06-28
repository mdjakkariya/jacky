import { describe, it, expect, vi, beforeEach } from "vitest";
import { createEarcons } from "./earcons.js";

beforeEach(() => {
  localStorage.clear();
  // Minimal AudioContext stub so we test guard logic, not real sound.
  const osc = { type: "", frequency: { value: 0 }, connect: vi.fn(), start: vi.fn(), stop: vi.fn() };
  const gainNode = { gain: { value: 0, setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() }, connect: vi.fn() };
  window.AudioContext = vi.fn(() => ({
    currentTime: 0, state: "running",
    createOscillator: () => osc, createGain: () => gainNode,
    destination: {}, resume: vi.fn().mockResolvedValue(),
  }));
});

it("enabled() honors the localStorage opt-out", () => {
  const e = createEarcons();
  expect(e.enabled()).toBe(true);
  localStorage.setItem("jackEarcons", "0");
  expect(e.enabled()).toBe(false);
});

it("playState is a no-op for idle and does not throw", () => {
  const e = createEarcons({ gain: 0.3 });
  expect(() => e.playState("idle")).not.toThrow();
});

it("playState plays a known state without throwing", () => {
  const e = createEarcons();
  expect(() => e.playState("listening")).not.toThrow();
});

it("does nothing when disabled", () => {
  localStorage.setItem("jackEarcons", "0");
  const e = createEarcons();
  expect(() => e.playMode("voice")).not.toThrow();
  expect(() => e.playState("listening")).not.toThrow();
});
