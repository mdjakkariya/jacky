import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("./tauri.js", () => ({ copyToClipboard: vi.fn() }));
import { copyToClipboard } from "./tauri.js";
import { copyText } from "./clipboard.js";

beforeEach(() => { vi.clearAllMocks(); });

// happy-dom exposes navigator.clipboard as a getter-only prop, so swap it via defineProperty.
function stubClipboard(writeText) {
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
}

it("returns true when native copy succeeds", async () => {
  copyToClipboard.mockResolvedValue(true);
  expect(await copyText("hi")).toBe(true);
});

it("falls back to navigator.clipboard when native fails", async () => {
  copyToClipboard.mockResolvedValue(false);
  const writeText = vi.fn().mockResolvedValue();
  stubClipboard(writeText);
  expect(await copyText("hi")).toBe(true);
  expect(writeText).toHaveBeenCalledWith("hi");
});

it("returns false when both fail", async () => {
  copyToClipboard.mockResolvedValue(false);
  stubClipboard(vi.fn().mockRejectedValue(new Error("no")));
  expect(await copyText("hi")).toBe(false);
});
