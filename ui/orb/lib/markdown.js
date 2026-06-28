/** Minimal, dependency-free Markdown for Jack's replies. Everything is HTML-escaped
 *  first, so model text can't inject markup; we only add our own tags for code,
 *  lists, bold/italic, and http(s) links. */

export function escapeHtml(s) {
  return s.replace(/[&<>]/g, function (c) { return c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;"; });
}

export function renderMarkdown(src) {
  const blocks = [];
  src = src.replace(/```[a-zA-Z0-9]*\n?([\s\S]*?)```/g, function (_m, code) {
    blocks.push("<pre><code>" + escapeHtml(code.replace(/\n$/, "")) + "</code></pre>");
    return " B" + (blocks.length - 1) + " ";
  });
  src = escapeHtml(src);
  src = src.replace(/`([^`]+)`/g, function (_m, c) { return "<code>" + c + "</code>"; });
  // URL chars exclude quotes/angle-brackets: escapeHtml above does not escape `"`, so a
  // crafted link (e.g. `[x](https://e"onmouseover="x)`) must not be able to break out of
  // the href attribute. A quote terminates the URL match, so no malicious anchor is built.
  // (Hardening over the original chat.html renderer — closes an attribute-breakout XSS in
  // LLM-rendered replies.)
  src = src.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)"'<>]+)\)/g, function (_m, t, u) { return '<a class="mdlink" href="' + u + '">' + t + "</a>"; });
  src = src.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  src = src.replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
  const lines = src.split("\n"); const out = []; let i = 0;
  while (i < lines.length) {
    const ph = lines[i].match(/^ B(\d+) $/);
    if (ph) { out.push(blocks[+ph[1]]); i++; continue; }
    if (/^\s*[-*]\s+/.test(lines[i])) {
      const ul = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) { ul.push("<li>" + lines[i].replace(/^\s*[-*]\s+/, "") + "</li>"); i++; }
      out.push("<ul>" + ul.join("") + "</ul>"); continue;
    }
    if (/^\s*\d+\.\s+/.test(lines[i])) {
      const ol = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { ol.push("<li>" + lines[i].replace(/^\s*\d+\.\s+/, "") + "</li>"); i++; }
      out.push("<ol>" + ol.join("") + "</ol>"); continue;
    }
    if (lines[i].trim() === "") { i++; continue; }
    out.push("<p>" + lines[i] + "</p>"); i++;
  }
  return out.join("");
}
