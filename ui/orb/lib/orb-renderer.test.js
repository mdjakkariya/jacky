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
