import { describe, expect, it } from "vitest";
import { diffFileName, lineAnchor, looksLikeDiff, parseDiff } from "./diff";

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
