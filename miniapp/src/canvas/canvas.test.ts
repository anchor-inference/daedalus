// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { changeSummary, dayLabel, groupByDay } from "./history";
import { attachment, colorValue, edgeGeometry, facingSide, groupContents, movingSet, nearestSide, normalizeDoc, safeUrl, type CanvasDoc } from "./model";
import { UndoStack, applyOps, compact, diffDocs, invertOps, type Op } from "./ops";
import { TEMPLATES, templateDoc } from "./templates";
import { frameOf, readFrameMessage } from "./frames";
import { barPicks, barText, composeMessage, type Chip } from "./CommentBar";

afterEach(() => setLang("en"));

const card = (id: string, x: number, y: number, extra: Record<string, unknown> = {}) => ({ id, type: "text" as const, text: id, x, y, width: 100, height: 50, ...extra });

function doc(): CanvasDoc {
  return {
    nodes: [{ id: "g", type: "group", label: "Group", x: -20, y: -20, width: 300, height: 120 }, card("a", 0, 0), card("b", 400, 0, { future: { kept: true } })],
    edges: [{ id: "e", fromNode: "a", toNode: "b", fromSide: "right", toSide: "left" }],
    future: "kept",
  };
}

describe("geometry", () => {
  it("leaves a box by the side facing the other, weighed by its proportions", () => {
    const box = { x: 0, y: 0, width: 200, height: 50 };
    expect(facingSide(box, { x: 400, y: 30 })).toBe("right");
    expect(facingSide(box, { x: 100, y: -300 })).toBe("top");
    expect(facingSide(box, { x: -50, y: 200 })).toBe("bottom");
    expect(nearestSide(box, { x: 190, y: 25 })).toBe("right");
  });

  it("draws an edge from side to side, with its label on the curve", () => {
    const geometry = edgeGeometry({ x: 0, y: 0, width: 100, height: 50 }, { x: 300, y: 0, width: 100, height: 50 }, "right", "left");
    expect(geometry.from).toEqual({ x: 100, y: 25 });
    expect(geometry.to).toEqual({ x: 300, y: 25 });
    expect(geometry.label).toEqual({ x: 200, y: 25 });
    expect(geometry.path.startsWith("M 100 25 C")).toBe(true);
  });

  it("carries what a group holds by geometry, nested groups included", () => {
    const value = doc();
    expect(groupContents(value, "g")).toEqual(["a"]);
    expect([...movingSet(value, ["g"])].sort()).toEqual(["a", "g"]);
  });

  it("links only to http and https, and knows an attachment from a workspace path", () => {
    expect(safeUrl("javascript:alert(1)")).toBeNull();
    expect(safeUrl("https://example.com/a")).toBe("https://example.com/a");
    expect(attachment("files/abc123/preview.svg")).toEqual({ id: "abc123", name: "preview.svg" });
    expect(attachment("site/index.html")).toBeNull();
    expect(colorValue("3")).toBe("var(--cv-3)");
    expect(colorValue("#2e8b67")).toBe("#2e8b67");
    expect(colorValue("url(x)")).toBe("");
  });

  it("gives a document from anything the host sent", () => {
    expect(normalizeDoc(null)).toEqual({ nodes: [], edges: [] });
    expect(normalizeDoc({ nodes: [card("a", 0, 0)], extra: 1 })).toEqual({ nodes: [card("a", 0, 0)], edges: [], extra: 1 });
  });
});

describe("operations", () => {
  it("replace in place, keep unknown keys, and take a node's edges with it", () => {
    const value = doc();
    const moved = applyOps(value, [{ op: "put", kind: "node", item: { ...value.nodes[2], x: 500 } }]);
    expect(moved.nodes.map((node) => node.id)).toEqual(["g", "a", "b"]);
    expect(moved.nodes[2].future).toEqual({ kept: true });
    expect(moved.future).toBe("kept");
    expect(applyOps(value, [{ op: "delete", id: "a" }]).edges).toEqual([]);
    expect(applyOps(value, [{ op: "put", kind: "node", item: card("c", 0, 0), at: "back" }]).nodes[0].id).toBe("c");
  });

  it("drop an edge whose node is gone rather than draw it hanging", () => {
    expect(applyOps(doc(), [{ op: "put", kind: "edge", item: { id: "x", fromNode: "a", toNode: "nowhere" } }]).edges.map((edge) => edge.id)).toEqual(["e"]);
  });

  it("invert to the document they started from", () => {
    const value = doc();
    const ops: Op[] = [
      { op: "put", kind: "node", item: { ...value.nodes[1], text: "changed" } },
      { op: "delete", id: "b" },
      { op: "put", kind: "node", item: card("c", 9, 9) },
    ];
    const after = applyOps(value, ops);
    const back = applyOps(after, invertOps(value, ops));
    const ids = (d: CanvasDoc) => [d.nodes.map((node) => node.id).sort(), d.edges.map((edge) => edge.id)];
    expect(ids(back)).toEqual(ids(value));
    expect(back.nodes.find((node) => node.id === "a")?.text).toBe("a");
  });

  it("fold a drag of many steps into one put", () => {
    const steps: Op[] = [1, 2, 3].map((x) => ({ op: "put", kind: "node", item: card("a", x, 0) }));
    expect(compact(steps)).toEqual([{ op: "put", kind: "node", item: card("a", 3, 0), at: undefined }]);
    const recreate: Op[] = [{ op: "put", kind: "node", item: card("a", 1, 0) }, { op: "delete", id: "a" }, { op: "put", kind: "node", item: card("a", 2, 0) }];
    expect(compact(recreate)).toHaveLength(3);
  });

  it("describe a whole-document change as single items", () => {
    const value = doc();
    const after = applyOps(value, [{ op: "delete", id: "e" }, { op: "put", kind: "node", item: card("c", 0, 0) }]);
    expect(applyOps(value, diffDocs(value, after))).toEqual(after);
  });

  it("undo one action at a time, and never bring back what someone else deleted", () => {
    const stack = new UndoStack();
    let value = doc();
    const move: Op[] = [{ op: "put", kind: "node", item: { ...value.nodes[1], x: 50 } }];
    stack.record(value, move);
    value = applyOps(value, move);
    const back = stack.undo(value)!;
    expect(applyOps(value, back).nodes[1].x).toBe(0);
    const again = stack.redo(applyOps(value, back))!;
    expect(again).toHaveLength(1);
    // The agent deleted the card meanwhile: undoing the move must not resurrect it.
    const deleted = applyOps(value, [{ op: "delete", id: "a" }]);
    stack.record(value, move);
    expect(stack.undo(deleted)).toEqual([]);
  });
});

describe("history words", () => {
  it("counts items in words, never ids", () => {
    setLang("en");
    expect(changeSummary({ added: { card: 3, edge: 1 }, removed: {}, changed: 2, renamed: true })).toBe("+3 cards, +1 connection, 2 changed, renamed");
    expect(changeSummary({ removed: { group: 2 } })).toBe("2 groups removed");
    expect(changeSummary({}, "import")).toBe("Imported from a file");
    expect(changeSummary({ changed: 4, restored_from: 3 }, "restore")).toBe("Restored an earlier version, 4 changed");
    setLang("ru");
    expect(changeSummary({ added: { card: 2 }, removed: { edge: 5 }, changed: 1 })).toBe("+2 карточки, удалено: 5 связей, 1 изменён");
  });

  it("groups revisions under Today, Yesterday and the date", () => {
    setLang("en");
    const now = new Date(2026, 9, 6, 15, 0);
    const at = (day: number, hour: number) => new Date(2026, 9, day, hour, 5).toISOString();
    const groups = groupByDay([{ saved_at: at(4, 9) }, { saved_at: at(6, 14) }, { saved_at: at(5, 23) }], now);
    expect(groups.map((group) => group.label)).toEqual(["Today", "Yesterday", dayLabel(at(4, 9), now)]);
  });
});

describe("templates", () => {
  it("connect only nodes they contain, with unique ids", () => {
    for (const id of TEMPLATES) {
      const value = templateDoc(id);
      const ids = new Set(value.nodes.map((node) => node.id));
      expect(ids.size).toBe(value.nodes.length);
      for (const edge of value.edges) {
        expect(ids.has(edge.fromNode)).toBe(true);
        expect(ids.has(edge.toNode)).toBe(true);
      }
    }
    expect(templateDoc("blank")).toEqual({ nodes: [], edges: [] });
  });
});

describe("frames", () => {
  it("knows a live page from a file card", () => {
    expect(frameOf({ id: "f", type: "file", file: "site/index.html", x: 0, y: 0, width: 1280, height: 800, daedalus: { frame: { device: "desktop", rev: 2 } } })).toEqual({ device: "desktop", rev: 2 });
    expect(frameOf({ id: "f", type: "file", file: "notes.md", x: 0, y: 0, width: 100, height: 100 })).toBeNull();
  });

  it("reads a frame's message field by field and refuses what is not one", () => {
    expect(readFrameMessage({ daedalus: "canvas-frame", type: "blocked", origin: "https://cdn.example.test" })).toEqual({ type: "blocked", origin: "https://cdn.example.test" });
    expect(readFrameMessage({ daedalus: "canvas-frame", type: "blocked", origin: "https://cdn.example.test/x.js" })).toBeNull();
    expect(readFrameMessage({ daedalus: "canvas-frame", type: "blocked", origin: "javascript:alert(1)" })).toBeNull();
    expect(readFrameMessage({ daedalus: "other", type: "loaded" })).toBeNull();
    const picked = readFrameMessage({ daedalus: "canvas-frame", type: "picked", selector: "#hero", tag: "h1", text: "x".repeat(900), html: "<h1>", rect: { x: 1, y: "2", width: Infinity, height: 4 } });
    expect(picked).toMatchObject({ type: "picked", selector: "#hero", rect: { x: 1, y: 0, width: 0, height: 4 } });
    expect((picked as { text: string }).text).toHaveLength(400);
  });

  it("composes the bar's message as the host does", () => {
    const canvas = { id: "c1", title: "Shop", version: 3, doc: { nodes: [{ id: "web", type: "file" as const, file: "site/index.html", x: 0, y: 0, width: 1280, height: 800 }], edges: [] } };
    const chips: Chip[] = [
      { id: "1", kind: "pick", frameId: "web", page: "index.html", selector: "#hero", tag: "h1", text: "Hi", html: "<h1 id=\"hero\">Hi```</h1>", note: " Larger " },
      { id: "2", kind: "card", nodeId: "n1", words: "Hero needs work" },
    ];
    expect(barText(chips, " Please ")).toBe("On card n1 «Hero needs work»:\n\nPlease");
    expect(composeMessage(canvas, barText(chips, "Please"), barPicks(chips))).toBe([
      "[Canvas «Shop» · canvas c1 · version 3 · /app/canvas/c1]",
      "On frame `web` (site/index.html, 1280x800), element `#hero`:",
      "```html\n<h1 id=\"hero\">Hi</h1>\n```",
      "Larger",
      "On card n1 «Hero needs work»:\n\nPlease",
    ].join("\n\n"));
  });
});
