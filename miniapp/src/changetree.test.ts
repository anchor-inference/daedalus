import { describe, expect, it } from "vitest";
import { groupByFolder, nextToView } from "./changetree";
import { diffFileName, parseDiff } from "./diff";

const files = parseDiff(["order/form.js", "order/validate.js", "site/order.html", "README.md"].map((p) => `--- a/${p}\n+++ b/${p}\n@@ -1 +1 @@\n-a\n+b\n`).join(""));

describe("the file column", () => {
  it("groups files under their folder and keeps the patch's order", () => {
    expect(groupByFolder(files).map((g) => [g.dir, g.files.map(diffFileName)])).toEqual([
      ["order/", ["order/form.js", "order/validate.js"]],
      ["site/", ["site/order.html"]],
      ["", ["README.md"]],
    ]);
  });
  it("narrows by any part of the path, whatever the case", () => {
    expect(groupByFolder(files, "ORDER").map((g) => g.files.length)).toEqual([2, 1]);
    expect(groupByFolder(files, "nothing")).toEqual([]);
  });
  it("walks to the next file not yet viewed, round the end", () => {
    const paths = files.map(diffFileName);
    expect(nextToView(paths, "order/form.js", new Set(["order/validate.js"]))).toBe("site/order.html");
    expect(nextToView(paths, "README.md", new Set(["order/form.js"]))).toBe("order/validate.js");
    expect(nextToView(paths, "README.md", new Set(paths))).toBeNull();
  });
});
