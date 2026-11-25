import { describe, expect, it } from "vitest";
import { diffFileName, diffRows, lineAnchor, looksLikeDiff, parseDiff, splitRows } from "./diff";

describe("unified diffs", () => {
  it("numbers both sides and does not invent a context line from the final newline", () => {
    const files = parseDiff("--- a/a.py\n+++ b/a.py\n@@ -2,2 +2,2 @@\n keep\n-old\n+new\n\\ No newline at end of file\n");
    expect(files[0].added).toBe(1);
    expect(files[0].removed).toBe(1);
    expect(files[0].hunks[0].lines.map((l) => [l.oldNo, l.newNo])).toEqual([[2, 2], [3, null], [null, 3], [null, null]]);
  });
  it("separates consecutive files without git headers and keeps header-like payload lines", () => {
    const files = parseDiff("--- a/one\n+++ b/one\n@@ -1 +1 @@\n--- old\n+++ new\n--- a/two\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n");
    expect(files).toHaveLength(2);
    expect(diffFileName(files[1])).toBe("two");
    expect(files[0].hunks[0].lines[0].text).toBe("-- old");
  });
  it("recognises a diff carried by a receipt and a headerless hunk", () => {
    expect(looksLikeDiff("check output\n@@ -0,0 +1 @@\n+hello\n")).toBe(true);
    expect(parseDiff("@@ -0,0 +1 @@\n+hello\n")[0].added).toBe(1);
    expect(looksLikeDiff("ordinary output")).toBe(false);
  });
});

describe("the line a review note is pinned to", () => {
  const patch = "diff --git a/api/notify.py b/api/notify.py\n--- a/api/notify.py\n+++ b/api/notify.py\n@@ -1,3 +1,4 @@\n def notify(order):\n-    send(order)\n+    if order.paid:\n+        send(order)\n     return True\n@@ -9,2 +10,1 @@\n keep\n-gone\n\\ No newline at end of file\n"
    + "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n";
  const [file, deleted] = parseDiff(patch);

  it("uses the head line of an added or unchanged row", () => {
    expect(lineAnchor(file, file.hunks[0], 0)).toEqual({ path: "api/notify.py", line: 1 });
    expect(lineAnchor(file, file.hunks[0], 2)).toEqual({ path: "api/notify.py", line: 2 });
    expect(lineAnchor(file, file.hunks[0], 4)).toEqual({ path: "api/notify.py", line: 4 });
  });

  it("pins a removed row to the head line it sits before, or after at the end of a hunk", () => {
    expect(lineAnchor(file, file.hunks[0], 1)).toEqual({ path: "api/notify.py", line: 2 });
    expect(lineAnchor(file, file.hunks[1], 1)).toEqual({ path: "api/notify.py", line: 10 });
  });

  it("gives nothing for a no-newline mark or a deleted file", () => {
    expect(lineAnchor(file, file.hunks[1], 2)).toBeNull();
    expect(lineAnchor(deleted, deleted.hunks[0], 0)).toBeNull();
  });
});

describe("what a file's header says", () => {
  const patch = "diff --git a/new.txt b/new.txt\nnew file mode 100644\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+hi\n"
    + "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    + "diff --git a/old.txt b/moved.txt\nsimilarity index 100%\nrename from old.txt\nrename to moved.txt\n"
    + "diff --git a/gone.txt b/gone.txt\ndeleted file mode 100644\n--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n";
  const files = parseDiff(patch);

  it("tells a made file, a binary one, a moved one and a removed one from a changed one", () => {
    expect(files.map((f) => [diffFileName(f), f.kind, f.binary])).toEqual([["new.txt", "added", false], ["logo.png", "modified", true], ["moved.txt", "renamed", false], ["gone.txt", "deleted", false]]);
  });
  it("reads a patch without git headers through /dev/null", () => {
    expect(parseDiff("--- /dev/null\n+++ b/a\n@@ -0,0 +1 @@\n+x\n")[0].kind).toBe("added");
  });
});

describe("the rows of a file", () => {
  const ctx = (n: number) => Array.from({ length: n }, (_, i) => ` c${i}`).join("\n");
  const file = parseDiff(`--- a/x\n+++ b/x\n@@ -1,24 +1,24 @@\n${ctx(10)}\n-old\n+new\n${ctx(2)}\n@@ -40,16 +40,16 @@\n${ctx(3)}\n-x\n+y\n${ctx(12)}\n`)[0];

  it("folds a long unchanged run to the lines beside a change and opens it on request", () => {
    const rows = diffRows(file);
    const folds = rows.filter((r) => r.type === "fold");
    expect(folds).toHaveLength(2);
    // Ten lines before the change keep the last three; twelve at the hunk's end keep the first three.
    expect(folds.map((r) => r.type === "fold" && r.lines.length)).toEqual([7, 9]);
    const opened = diffRows(file, new Set(["0:0"]));
    expect(opened.filter((r) => r.type === "fold")).toHaveLength(1);
    expect(opened.filter((r) => r.type === "line")).toHaveLength(rows.filter((r) => r.type === "line").length + 7);
  });
  it("counts the lines the patch leaves out between two hunks", () => {
    const gaps = diffRows(parseDiff("--- a/x\n+++ b/x\n@@ -3,2 +3,2 @@\n a\n-b\n+c\n@@ -20,2 +20,2 @@\n d\n-e\n+f\n")[0]).filter((r) => r.type === "gap");
    // The first hunk's old side ends at line 4 (3 and 4); the second starts at 20.
    expect(gaps).toEqual([{ type: "gap", count: 20 - 5 }]);
  });
  it("lays removed lines beside the added ones that replace them", () => {
    const rows = splitRows(diffRows(parseDiff("--- a/x\n+++ b/x\n@@ -1,3 +1,4 @@\n keep\n-a\n-b\n+c\n+d\n+e\n tail\n")[0]));
    expect(rows.filter((r) => r.type === "pair").map((r) => r.type === "pair" && [r.left?.text ?? null, r.right?.text ?? null])).toEqual([["keep", "keep"], ["a", "c"], ["b", "d"], [null, "e"], ["tail", "tail"]]);
  });
});
