// @vitest-environment jsdom
// A file's diff says what each kind of file is: lines with both numbers, a fold that opens, a word for
// the files with no lines, and the patch's cut named under the last line rather than passed over.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { parseDiff } from "./diff";
import { FileDiffBody, FileDiffHeader } from "./diffpane";
import { setLang } from "./i18n";

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  setLang("en");
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

const ctx = (n: number) => Array.from({ length: n }, (_, i) => ` keep ${i}`).join("\n");

describe("the lines of a file", () => {
  it("draws each line with its two numbers and its sign, and a folded run that opens on a press", () => {
    const [file] = parseDiff(`--- a/x\n+++ b/x\n@@ -1,13 +1,13 @@\n${ctx(10)}\n-old\n+new\n keep\n`);
    act(() => root.render(<FileDiffBody file={file} mode="unified" />));
    expect(host.querySelectorAll(".dp-line.add .dp-text")[0].textContent).toBe("new");
    expect(host.querySelector(".dp-line.del .dp-sign")?.textContent).toBe("−");
    const before = host.querySelectorAll(".dp-line").length;
    const fold = host.querySelector<HTMLButtonElement>("button.dp-fold")!;
    expect(fold.textContent).toContain("7 unchanged lines");
    act(() => fold.click());
    expect(host.querySelectorAll(".dp-line").length).toBe(before + 7);
    expect(host.querySelector("button.dp-fold")).toBeNull();
  });

  it("pairs the removed line with the added one side by side", () => {
    const [file] = parseDiff("--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n");
    act(() => root.render(<FileDiffBody file={file} mode="split" />));
    const pairs = [...host.querySelectorAll(".dp-pair")].map((p) => [...p.querySelectorAll(".dp-text")].map((t) => t.textContent));
    expect(pairs).toEqual([["keep", "keep"], ["old", "new"]]);
  });

  it("says what a file with no lines is instead of drawing nothing", () => {
    const files = parseDiff("diff --git a/i.png b/i.png\nBinary files a/i.png and b/i.png differ\ndiff --git a/o b/n\nsimilarity index 100%\nrename from o\nrename to n\n");
    act(() => root.render(<FileDiffBody file={files[0]} mode="unified" />));
    expect(host.querySelector(".dp-none")?.textContent).toContain("binary");
    act(() => root.render(<FileDiffBody file={files[1]} mode="unified" />));
    expect(host.querySelector(".dp-none")?.textContent).toBe("Moved from o to n; the contents did not change.");
  });

  it("names a cut patch under the lines", () => {
    const [file] = parseDiff("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n");
    act(() => root.render(<FileDiffBody file={file} mode="unified" cut="Cut here." />));
    expect(host.querySelector(".dp-cut")?.textContent).toBe("Cut here.");
  });
});

describe("a file's header", () => {
  it("shows the folder dimmed, the counts that exist and the controls it is given", () => {
    const [file] = parseDiff("--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\n x\n+y\n");
    let viewed = false;
    act(() => root.render(<FileDiffHeader file={file} viewed={false} onViewed={(on) => { viewed = on; }} />));
    expect(host.querySelector(".dp-dir")?.textContent).toBe("src/");
    expect(host.querySelector(".dp-name b")?.textContent).toBe("a.py");
    expect(host.querySelector(".dp-counts")?.textContent).toBe("+1");
    expect(host.querySelector(".dp-mode")).toBeNull();
    act(() => host.querySelector<HTMLButtonElement>(".dp-viewed")!.click());
    expect(viewed).toBe(true);
  });
});
