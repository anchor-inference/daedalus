import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./md";

describe("renderMarkdown", () => {
  it("escapes markup from the model", () => {
    const html = renderMarkdown('<img src=x onerror=alert(1)> **b** [x](javascript:alert(1)) `<b>`');
    expect(html).not.toContain("<img");
    expect(html).toContain("<b>b</b>");
    expect(html).not.toContain('href="javascript');
    expect(html).toContain("<code>&lt;b&gt;</code>");
  });
  it("opens an internal diagram link without accepting arbitrary app paths", () => {
    const id = "0123456789abcdef0123456789abcdef";
    expect(renderMarkdown(`[Open diagram](/app/diagrams/${id})`)).toContain(`<a href="/app/diagrams/${id}">Open diagram</a>`);
    expect(renderMarkdown("[bad](/api/settings)")).not.toContain('<a href=');
  });
  it("survives a header row followed by a bare rule", () => {
    expect(() => renderMarkdown("| a | b |\n---\nrest")).not.toThrow();
    expect(renderMarkdown("| a | b |\n---\nrest")).toContain("<hr/>");
  });
  it("renders tables, lists, code and details", () => {
    const html = renderMarkdown("| a | b |\n|---|--:|\n| 1 | 2 |\n\n- x\n  - y\n\n1. z\n\n```py\nprint(1)\n```\n<details><summary>s</summary>\n\nhidden\n\n</details>");
    expect(html).toContain("<table>");
    expect(html).toContain('<th style="text-align:right">b</th>');
    expect(html).toContain("<ul><li>x<ul><li>y</li></ul></li></ul>");
    expect(html).toContain("<ol><li>z</li></ol>");
    expect(html).toContain("codecard");
    expect(html).toContain("<details><summary>s</summary>");
  });
  it("turns cited files and runs into chips", () => {
    const html = renderMarkdown('the fix is in <file path="daedalus/tools/web.py" lines="20-40"/> and the tests pass <run id="v12" label="14 passed"/>.');
    expect(html).toContain('data-evidence="file"');
    expect(html).toContain('data-path="daedalus/tools/web.py"');
    expect(html).toContain('data-lines="20-40"');
    expect(html).toContain('data-evidence="run"');
    expect(html).toContain('data-run="v12"');
    expect(html).toContain("14 passed");
    expect(html).not.toContain("&lt;file");
  });
  it("leaves a quoted tag quoted and an incomplete one as text", () => {
    const quoted = renderMarkdown('write `<file path="a.py"/>` to cite it');
    expect(quoted).toContain("<code>&lt;file path=&quot;a.py&quot;/&gt;</code>");
    expect(quoted).not.toContain("data-evidence");
    const bare = renderMarkdown('<file lines="1-2"/> and <run label="x"/> point nowhere');
    expect(bare).not.toContain("data-evidence");
    expect(bare).toContain("&lt;file lines=&quot;1-2&quot;/&gt;");
  });
  it("keeps an attribute from escaping its quotes", () => {
    const html = renderMarkdown('<file path="a" onmouseover="alert(1)"/> <file path="&lt;b&gt;.py"/>');
    expect(html).not.toContain("onmouseover");
    expect(html).toContain('data-path="a"');
    expect(html).toContain("data-path=\"&amp;lt;b&amp;gt;.py\"");
  });
  it("closes unbalanced details", () => {
    const html = renderMarkdown("<details><summary>s</summary>\ntext");
    expect((html.match(/<\/details>/g) ?? []).length).toBe(1);
  });
});

it("the stash sentinel in model text cannot address a stashed element", () => {
  const html = renderMarkdown("`a` \uE0000\uE000 and \uE0007\uE000");
  expect(html).not.toContain("undefined");
  expect((html.match(/<code>/g) ?? []).length).toBe(1);
});
