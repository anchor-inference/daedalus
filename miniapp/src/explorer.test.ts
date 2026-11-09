import { describe, expect, it } from "vitest";
import { collapseAll, emptyTree, groupGrep, markMatches, splitPath, filterLoaded, keyMove, loadedRows, matchesName, readGrep, readNameSearch, setListing, setLoading, toggleFolder, typeAhead, visibleRows } from "./explorer";

const entry = (name: string, dir = false) => ({ name, dir, size: 12, mtime: 0 });
const root = () => setListing(emptyTree(), "", [entry("z.py"), entry("src", true), entry(".env"), entry("node_modules", true)]);

describe("the lazy file tree", () => {
  it("requests a folder once, keeps its cached listing while closed, and hides ignored entries", () => {
    let tree = root();
    expect(Object.keys(tree)).toEqual([""]);
    expect(visibleRows(tree, { showHidden: false }).map((r) => r.path)).toEqual(["src", "z.py"]);
    const opened = toggleFolder(tree, "src");
    expect(opened.load).toBe(true);
    tree = setLoading(opened.tree, "src");
    expect(visibleRows(tree, { showHidden: false })[0].loading).toBe(true);
    tree = setListing(tree, "src", [entry("main.py")]);
    expect(visibleRows(tree, { showHidden: false })[1].depth).toBe(1);
    tree = toggleFolder(tree, "src").tree;
    expect(toggleFolder(tree, "src").load).toBe(false);
    expect(filterLoaded(loadedRows(tree, false), "*.py").map((r) => r.path)).toEqual(["src/main.py", "z.py"]);
    expect(visibleRows(tree, { showHidden: true }).some((r) => r.ignored)).toBe(true);
  });
  it("moves through visible rows and never steps out of an empty open folder with Right", () => {
    let tree = toggleFolder(root(), "src").tree;
    tree = setListing(tree, "src", [entry("main.py")]);
    const rows = visibleRows(tree, { showHidden: false });
    expect(keyMove(rows, 0, "ArrowRight")).toEqual({ index: 1, do: null });
    expect(keyMove(rows, 1, "ArrowLeft")?.index).toBe(0);
    expect(keyMove(rows, 1, "Backspace")?.index).toBe(0);
    expect(keyMove(rows, 0, "ArrowLeft")?.do).toBe("collapse");
    expect(keyMove(rows, 1, "Enter")?.do).toBe("open");
    expect(keyMove(rows, 2, "ArrowDown")?.index).toBe(2);
    expect(keyMove(rows, 1, "ArrowUp")?.index).toBe(0);
    expect(typeAhead(rows, 0, "z")).toBe(2);
    expect(typeAhead(rows, 2, "ma")).toBe(1);
    tree = setListing(tree, "src", []);
    expect(keyMove(visibleRows(tree, { showHidden: false }), 0, "ArrowRight")?.index).toBe(0);
  });
  it("reads both search contracts and keeps grep line numbers", () => {
    expect(readNameSearch({ query: "py", results: [{ path: "src/main.py", kind: "file", size: 7 }], truncated: true })?.truncated).toBe(true);
    expect(readGrep({ query: "hello", hits: [{ path: "src/main.py", line: 42, text: "hello()" }] })?.hits[0]).toEqual({ path: "src/main.py", line: 42, text: "hello()" });
    expect(readNameSearch({})).toBeNull();
    expect(readGrep({})).toBeNull();
    expect(matchesName("src/**/*.py", "src/main.py")).toBe(true);
    expect(matchesName("src/**/*.py", "src/lib/main.py")).toBe(true);
    expect(matchesName("[z-a]", "main.py")).toBe(false);
    expect(matchesName("MAIN", "src/main.py")).toBe(true);
  });
});

describe("the redesigned pane's helpers", () => {
  it("closes every folder but the root and keeps what was listed", () => {
    let tree = toggleFolder(root(), "src").tree;
    tree = setListing(tree, "src", [entry("main.py")]);
    const closed = collapseAll(tree);
    expect(closed[""].open).toBe(true);
    expect(closed.src.open).toBe(false);
    expect(closed.src.entries).toHaveLength(1);
    expect(visibleRows(closed, { showHidden: false }).map((r) => r.path)).toEqual(["src", "z.py"]);
  });
  it("cuts a path before its last part, whatever the separator", () => {
    expect(splitPath("/srv/workspaces/s_example")).toEqual(["/srv/workspaces/", "s_example"]);
    expect(splitPath("/srv/workspaces/s_example/")).toEqual(["/srv/workspaces/", "s_example"]);
    expect(splitPath("C:\\work\\bakery")).toEqual(["C:\\work\\", "bakery"]);
    expect(splitPath("/")).toEqual(["", "/"]);
    expect(splitPath("bakery")).toEqual(["", "bakery"]);
  });
  it("groups content hits by file in the order they were found", () => {
    const groups = groupGrep([{ path: "a.html", line: 4, text: "x" }, { path: "b.csv", line: 1, text: "y" }, { path: "a.html", line: 9, text: "z" }]);
    expect(groups.map((g) => [g.path, g.hits.map((h) => h.line)])).toEqual([["a.html", [4, 9]], ["b.csv", [1]]]);
  });
  it("marks every match ignoring case, and brings a far match into view", () => {
    expect(markMatches("  <h3>Gluten-free gluten</h3>", "GLUTEN")).toEqual([
      { text: "<h3>", hit: false }, { text: "Gluten", hit: true }, { text: "-free ", hit: false }, { text: "gluten", hit: true }, { text: "</h3>", hit: false },
    ]);
    expect(markMatches("plain line", "absent")).toEqual([{ text: "plain line", hit: false }]);
    const far = markMatches(`${"x".repeat(80)} needle`, "needle", 4);
    expect(far[0].text.startsWith("…")).toBe(true);
    expect(far.find((part) => part.hit)?.text).toBe("needle");
  });
});
