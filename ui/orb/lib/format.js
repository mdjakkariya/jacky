/** Pure display formatters for the chat header (token counts, model name, cost) and version compare. */

/** Compact token counts: 36000 -> "36.0k", 200000 -> "200k", <1000 stays exact. */
export function fmtK(n) {
  n = n || 0;
  if (n < 1000) return n.toLocaleString();
  const k = n / 1000;
  return (k >= 100 ? Math.round(k) : k.toFixed(1)) + "k";
}

/** Friendlier model label: "claude-haiku-4-5" -> "haiku-4.5"; locals pass through. */
export function fmtModel(m) {
  return (m || "—").replace(/^claude-/, "").replace(/-(\d+)-(\d+)$/, "-$1.$2");
}

/** Session cost in USD, scaled for readability. */
export function fmtUSD(v) {
  if (v <= 0) return "$0.00";
  if (v < 0.0001) return "<$0.0001";
  if (v < 1) return "$" + v.toFixed(4);
  return "$" + v.toFixed(2);
}

/** Compare dotted version cores. Strips a leading "v" and any "-prerelease" suffix. */
export function cmpVer(a, b) {
  const core = (v) => String(v).replace(/^v/i, "").split("-")[0].split(".");
  const pa = core(a), pb = core(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = parseInt(pa[i] || "0", 10), y = parseInt(pb[i] || "0", 10);
    if (x > y) return 1;
    if (x < y) return -1;
  }
  return 0;
}
