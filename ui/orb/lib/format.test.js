import { describe, it, expect } from "vitest";
import { fmtK, fmtModel, fmtUSD, cmpVer } from "./format.js";

describe("fmtK", () => {
  it("keeps small numbers exact", () => { expect(fmtK(0)).toBe("0"); expect(fmtK(999)).toBe("999"); });
  it("uses one decimal under 100k", () => { expect(fmtK(36000)).toBe("36.0k"); });
  it("rounds at/above 100k", () => { expect(fmtK(200000)).toBe("200k"); });
  it("treats nullish as 0", () => { expect(fmtK(undefined)).toBe("0"); });
});

describe("fmtModel", () => {
  it("shortens claude ids", () => { expect(fmtModel("claude-haiku-4-5")).toBe("haiku-4.5"); });
  it("passes locals through", () => { expect(fmtModel("qwen3:8b")).toBe("qwen3:8b"); });
  it("handles nullish", () => { expect(fmtModel(null)).toBe("—"); });
});

describe("fmtUSD", () => {
  it("zero or less", () => { expect(fmtUSD(0)).toBe("$0.00"); expect(fmtUSD(-1)).toBe("$0.00"); });
  it("tiny nonzero", () => { expect(fmtUSD(0.00005)).toBe("<$0.0001"); });
  it("sub-dollar uses 4 decimals", () => { expect(fmtUSD(0.1234)).toBe("$0.1234"); });
  it("dollar+ uses 2 decimals", () => { expect(fmtUSD(2.5)).toBe("$2.50"); });
});

describe("cmpVer", () => {
  it("orders core versions", () => { expect(cmpVer("1.2.0", "1.1.9")).toBe(1); expect(cmpVer("1.0.0", "1.0.1")).toBe(-1); expect(cmpVer("1.0.0", "1.0.0")).toBe(0); });
  it("strips a leading v", () => { expect(cmpVer("v2.0.0", "1.9.9")).toBe(1); });
  it("ignores prerelease suffix", () => { expect(cmpVer("1.2.3-beta", "1.2.3")).toBe(0); });
});
