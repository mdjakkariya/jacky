import { describe, it, expect, vi } from "vitest";

// Fresh module per test so the shared Set doesn't leak between cases.
async function fresh() {
  vi.resetModules();
  return import("./popover.js");
}

it("closeOtherPopovers closes every registered popover except the given one", async () => {
  const { registerPopover, closeOtherPopovers } = await fresh();
  const a = vi.fn(), b = vi.fn();
  registerPopover(a);
  registerPopover(b);
  closeOtherPopovers(a);
  expect(a).not.toHaveBeenCalled();
  expect(b).toHaveBeenCalledTimes(1);
});

it("the returned unregister removes a closer", async () => {
  const { registerPopover, closeOtherPopovers } = await fresh();
  const a = vi.fn(), b = vi.fn();
  const off = registerPopover(a);
  registerPopover(b);
  off();
  closeOtherPopovers(b); // a is unregistered, so it must not be called
  expect(a).not.toHaveBeenCalled();
});
