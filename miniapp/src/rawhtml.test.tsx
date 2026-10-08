// @vitest-environment jsdom
// Rendered markup keeps its nodes while its string is unchanged. Under React 19 an inline
// `dangerouslySetInnerHTML={{ __html }}` rewrote the element on every render, and during a run that
// was every second: the running command's code card jumped back to its start and a file open in the
// panel lost its scroll and the operator's selection. See `rawhtml.tsx`.

/// <reference types="vite/client" />
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { codeBlock } from "./md";
import { RawHtml } from "./rawhtml";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SOURCES = import.meta.glob("./**/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

describe("markup rendered from a string", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  it("keeps its nodes, scroll and selection across renders with the same string", () => {
    const html = codeBlock("for f in *.html; do echo \"$f\"; done", "bash");
    // A parent that re-renders with a fresh string of the same text, the way a clock tick did.
    act(() => root.render(<RawHtml html={`${html}`} />));
    const pre = host.querySelector("pre")!;
    pre.scrollLeft = 120;
    const text = pre.querySelector("code")!.firstChild!;
    const range = document.createRange();
    range.setStart(text, 2);
    range.setEnd(text, 8);
    document.getSelection()!.removeAllRanges();
    document.getSelection()!.addRange(range);
    for (let tick = 0; tick < 3; tick++) act(() => root.render(<RawHtml html={html.slice(0)} className={tick % 2 ? "streaming" : undefined} />));
    expect(host.querySelector("pre")).toBe(pre);
    expect(pre.scrollLeft).toBe(120);
    expect(document.getSelection()!.toString()).toBe("r f in");
  });

  it("writes the new markup when the string changes", () => {
    act(() => root.render(<RawHtml as="span" className="linebody" html="<b>one</b>" />));
    const before = host.querySelector("b");
    act(() => root.render(<RawHtml as="span" className="linebody" html="<b>two</b>" />));
    expect(host.querySelector("span.linebody")!.innerHTML).toBe("<b>two</b>");
    expect(host.querySelector("b")).not.toBe(before);
  });

  it("is the only way the app sets inner HTML", () => {
    // An inline `dangerouslySetInnerHTML={{ __html }}` is a new object on every render, which React 19
    // writes into the page every time; one that slips back in brings the reset back with it.
    const inline = Object.entries(SOURCES)
      .filter(([path]) => path !== "./rawhtml.tsx" && !path.endsWith(".test.tsx"))
      .filter(([, source]) => source.includes("dangerouslySetInnerHTML"))
      .map(([path]) => path);
    expect(inline).toEqual([]);
  });
});
