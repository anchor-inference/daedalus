// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { History, blocks, codeEnter, enterAt, inCode, renderBlocks } from "./codetext";
import { readField } from "./codefield";
import { codeBlock, copyButton } from "./md";

const field = (text: string) => {
  const root = document.createElement("div");
  root.innerHTML = renderBlocks(text, copyButton()).map((d) => d.html).join("");
  document.body.replaceChildren(root);
  return root;
};

describe("the composer's blocks", () => {
  it("cuts prose and fenced code, a fence still open running to the end", () => {
    expect(blocks("hi\n```py\nx = 1\n```\nbye")).toEqual([
      { kind: "prose", from: 0, lines: ["hi"] },
      { kind: "code", from: 1, open: "```py", label: "py", body: ["x = 1"], close: "```" },
      { kind: "prose", from: 4, lines: ["bye"] },
    ]);
    expect(blocks("```\na\nb")).toEqual([{ kind: "code", from: 0, open: "```", label: "", body: ["a", "b"], close: null }]);
  });

  it("draws every character exactly once, so the text reads back from the drawing", () => {
    for (const text of ["", "one", "one\n\nthree", "say\n```python\ndef f(x):\n    return x < 2 & 1\n```\n", "```js\nconst a = '<b>'", "a\n```\n\n```"]) {
      expect(readField(field(text)).text).toBe(text);
    }
  });

  it("draws the code as the answer's card does, coloured by its label", () => {
    const root = field("```python\nprint('hi')\n```");
    const card = root.querySelector(".codecard.cf-card")!;
    expect(card.getAttribute("data-lang")).toBe("python");
    expect(card.querySelector(".cf-lang")?.textContent).toBe("python");
    expect(card.querySelector(".tk-s")?.textContent).toBe("'hi'");
    expect(card.querySelector("button[data-copy]")?.getAttribute("data-copy-text")).toBe("print('hi')");
    // The answer colours the same code the same way.
    expect(codeBlock("print('hi')", "python")).toContain('<span class="tk-s">\'hi\'</span>');
  });

  it("knows when the caret is inside code", () => {
    const text = "a\n```\nx\n```\nb";
    expect(inCode(text, 0)).toBe(false);
    expect(inCode(text, text.indexOf("x"))).toBe(true);
    expect(inCode(text, text.length)).toBe(false);
  });
});

describe("Enter in the composer", () => {
  it("is a new line of code on an opening fence and inside the block, Send elsewhere", () => {
    const text = "hi\n```py\nx\n```";
    expect(codeEnter(text, 2)).toBe(false);
    expect(codeEnter(text, text.indexOf("py") + 2)).toBe(true);
    expect(codeEnter(text, text.indexOf("x") + 1)).toBe(true);
    expect(codeEnter(text, text.length)).toBe(false);
  });

  it("closes a fence just opened and puts the caret inside", () => {
    const next = enterAt("look:\n```python", 15, 15);
    expect(next.text).toBe("look:\n```python\n\n```");
    expect(next.caret).toBe(16);
  });

  it("keeps the indentation inside code and none in prose", () => {
    const text = "```\n    if x:\n```";
    const at = text.indexOf(":") + 1;
    expect(enterAt(text, at, at)).toEqual({ text: "```\n    if x:\n    \n```", caret: at + 5 });
    expect(enterAt("  hi", 4, 4)).toEqual({ text: "  hi\n", caret: 5 });
  });
});

describe("the field's Undo", () => {
  it("takes back a word typed in one step, with the space before it", () => {
    const h = new History({ text: "", start: 0, end: 0 });
    let at = 1000;
    for (const text of ["h", "hi", "hi ", "hi y", "hi yo", "hi yo\n"]) h.record({ text, start: text.length, end: text.length }, (at += 50));
    expect(h.undo()?.text).toBe("hi yo");
    expect(h.undo()?.text).toBe("hi");
    expect(h.undo()?.text).toBe("");
    expect(h.redo()?.text).toBe("hi");
  });

  it("starts a new step after a pause", () => {
    const h = new History({ text: "", start: 0, end: 0 });
    h.record({ text: "a", start: 1, end: 1 }, 1000);
    h.record({ text: "ab", start: 2, end: 2 }, 5000);
    expect(h.undo()?.text).toBe("a");
  });
});
