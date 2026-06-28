import { describe, it, expect } from "vitest";
import { escapeHtml, renderMarkdown } from "./markdown.js";

describe("escapeHtml", () => {
  it("escapes the three dangerous chars", () => {
    expect(escapeHtml('<a href="x">&')).toBe("&lt;a href=\"x\"&gt;&amp;");
  });
});

describe("renderMarkdown", () => {
  it("escapes HTML before rendering (no injection)", () => {
    expect(renderMarkdown("<script>")).toContain("&lt;script&gt;");
  });
  it("wraps a plain line in a paragraph", () => {
    expect(renderMarkdown("hello")).toBe("<p>hello</p>");
  });
  it("renders bold and italic", () => {
    expect(renderMarkdown("**b**")).toContain("<strong>b</strong>");
    expect(renderMarkdown("a *i*")).toContain("<em>i</em>");
  });
  it("renders inline code", () => {
    expect(renderMarkdown("`x`")).toContain("<code>x</code>");
  });
  it("renders a fenced code block", () => {
    const out = renderMarkdown("```\nline\n```");
    expect(out).toContain("<pre><code>");
    expect(out).toContain("line");
  });
  it("renders an unordered list", () => {
    const out = renderMarkdown("- one\n- two");
    expect(out).toContain("<ul><li>one</li><li>two</li></ul>");
  });
  it("renders an ordered list", () => {
    const out = renderMarkdown("1. one\n2. two");
    expect(out).toContain("<ol><li>one</li><li>two</li></ol>");
  });
  it("renders http links as mdlink anchors", () => {
    expect(renderMarkdown("[gh](https://github.com)")).toContain('<a class="mdlink" href="https://github.com">gh</a>');
  });
  it("does not build an anchor from a URL with an attribute-breakout quote (no XSS)", () => {
    const out = renderMarkdown('[x](https://evil.com"onmouseover="alert)');
    expect(out).not.toContain("<a ");          // a quote terminates the URL match → no anchor
    expect(out).not.toMatch(/<a [^>]*onmouseover/i);
  });
});
