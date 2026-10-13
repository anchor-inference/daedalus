// The snapshot's arithmetic: where a pointer lands on the picture, what a tap leaves behind, what Undo
// and Clear step through, and what the marks ask of a canvas.

import { describe, expect, it } from "vitest";
import { EMPTY_BOARD, arrowHead, boardReducer, drawMark, fitBox, meaningful, outlineOf, rectOf, snapshotName, strokeWidth, textSize, toPicture, type Mark, type Pen } from "./draw";

describe("fitBox", () => {
  it("keeps the picture's proportions inside the box", () => {
    expect(fitBox(1280, 800, 640, 640)).toEqual({ width: 640, height: 400 });
    expect(fitBox(1280, 800, 2000, 400)).toEqual({ width: 640, height: 400 });
  });
  it("is empty for a box or a picture of no size", () => {
    expect(fitBox(0, 800, 640, 640)).toEqual({ width: 0, height: 0 });
    expect(fitBox(1280, 800, 0, 640)).toEqual({ width: 0, height: 0 });
  });
});

describe("toPicture", () => {
  const rect = { left: 100, top: 50, width: 640, height: 400 };
  it("maps a pointer on the scaled picture to the picture's own pixels", () => {
    expect(toPicture(420, 250, rect, 1280, 800)).toEqual({ x: 640, y: 400 });
    expect(toPicture(100, 50, rect, 1280, 800)).toEqual({ x: 0, y: 0 });
  });
  it("holds a drag that leaves the picture at its edge", () => {
    expect(toPicture(2000, -30, rect, 1280, 800)).toEqual({ x: 1280, y: 0 });
  });
});

describe("sizes", () => {
  it("scales the line and the text with the picture", () => {
    expect(strokeWidth(1280, 800)).toBe(3);
    expect(strokeWidth(320, 200)).toBe(2);
    expect(textSize(1280, 800)).toBe(27);
    expect(textSize(200, 100)).toBe(14);
  });
});

describe("rectOf and arrowHead", () => {
  it("draws a box whichever way the drag went", () => {
    expect(rectOf({ x: 50, y: 80 }, { x: 10, y: 20 })).toEqual({ x: 10, y: 20, w: 40, h: 60 });
  });
  it("sweeps the head back from the tip", () => {
    const [a, b] = arrowHead({ x: 0, y: 0 }, { x: 100, y: 0 }, 10);
    expect(a.x).toBeCloseTo(100 - 10 * Math.cos(Math.PI / 6));
    expect(b.x).toBeCloseTo(a.x);
    expect(a.y).toBeCloseTo(5);
    expect(b.y).toBeCloseTo(-5);
  });
});

const arrow: Mark = { tool: "arrow", color: "#ff3b30", width: 3, from: { x: 10, y: 10 }, to: { x: 100, y: 60 } };
const box: Mark = { tool: "box", color: "#0a84ff", width: 3, from: { x: 10, y: 10 }, to: { x: 100, y: 60 } };

describe("meaningful", () => {
  it("drops a tap with the arrow or the box, an empty text and a one-point stroke", () => {
    expect(meaningful({ ...arrow, to: { x: 11, y: 11 } })).toBe(false);
    expect(meaningful({ ...box, to: { x: 200, y: 12 } })).toBe(false);
    expect(meaningful({ tool: "text", color: "#fff", size: 20, at: { x: 0, y: 0 }, text: "  " })).toBe(false);
    expect(meaningful({ tool: "pen", color: "#fff", width: 2, points: [{ x: 1, y: 1 }] })).toBe(false);
  });
  it("keeps a drawn one", () => {
    expect(meaningful(arrow)).toBe(true);
    expect(meaningful(box)).toBe(true);
    expect(meaningful({ tool: "pen", color: "#fff", width: 2, points: [{ x: 1, y: 1 }, { x: 3, y: 4 }] })).toBe(true);
  });
});

describe("boardReducer", () => {
  it("adds, undoes, clears and undoes the clear", () => {
    let board = boardReducer(EMPTY_BOARD, { kind: "add", mark: arrow });
    board = boardReducer(board, { kind: "add", mark: box });
    expect(board.marks).toEqual([arrow, box]);
    board = boardReducer(board, { kind: "undo" });
    expect(board.marks).toEqual([arrow]);
    board = boardReducer(board, { kind: "clear" });
    expect(board.marks).toEqual([]);
    board = boardReducer(board, { kind: "undo" });
    expect(board.marks).toEqual([arrow]);
    board = boardReducer(board, { kind: "undo" });
    expect(board).toEqual(EMPTY_BOARD);
  });
  it("records no step for a mark not worth keeping, an empty clear or an undo with nothing behind it", () => {
    expect(boardReducer(EMPTY_BOARD, { kind: "add", mark: { ...arrow, to: arrow.from } })).toBe(EMPTY_BOARD);
    expect(boardReducer(EMPTY_BOARD, { kind: "clear" })).toBe(EMPTY_BOARD);
    expect(boardReducer(EMPTY_BOARD, { kind: "undo" })).toBe(EMPTY_BOARD);
  });
});

/** A context that writes down what it was asked to do. */
function recorder(): Pen & { calls: string[] } {
  const calls: string[] = [];
  const note = (name: string) => (...args: unknown[]) => { calls.push(`${name}(${args.map((a) => (typeof a === "number" ? Math.round(a) : a)).join(",")})`); };
  return {
    calls, strokeStyle: "", fillStyle: "", lineWidth: 1, lineCap: "butt", lineJoin: "miter", font: "", textBaseline: "alphabetic",
    beginPath: note("beginPath"), moveTo: note("moveTo"), lineTo: note("lineTo"), closePath: note("closePath"), stroke: note("stroke"), fill: note("fill"),
    strokeRect: note("strokeRect"), fillText: note("fillText"), strokeText: note("strokeText"), save: note("save"), restore: note("restore"),
  } as Pen & { calls: string[] };
}

describe("drawMark", () => {
  it("strokes a box as one rectangle in its colour and width", () => {
    const pen = recorder();
    drawMark(pen, box);
    expect(pen.calls).toContain("strokeRect(10,10,90,50)");
    expect(pen.strokeStyle).toBe("#0a84ff");
  });
  it("draws an arrow as its shaft and a filled head", () => {
    const pen = recorder();
    drawMark(pen, arrow);
    expect(pen.calls.filter((c) => c === "stroke()")).toHaveLength(1);
    expect(pen.calls.filter((c) => c === "fill()")).toHaveLength(1);
    expect(pen.calls).toContain("lineTo(100,60)");
  });
  it("outlines text before filling it, line by line", () => {
    const pen = recorder();
    drawMark(pen, { tool: "text", color: "#ffcc00", size: 20, at: { x: 5, y: 5 }, text: "one\ntwo" });
    expect(pen.calls.filter((c) => c.startsWith("strokeText"))).toEqual(["strokeText(one,5,5)", "strokeText(two,5,29)"]);
    expect(pen.calls.indexOf("strokeText(one,5,5)")).toBeLessThan(pen.calls.indexOf("fillText(one,5,5)"));
  });
  it("follows a pen stroke through its points", () => {
    const pen = recorder();
    drawMark(pen, { tool: "pen", color: "#fff", width: 2, points: [{ x: 1, y: 2 }, { x: 3, y: 4 }, { x: 5, y: 6 }] });
    expect(pen.calls).toEqual(expect.arrayContaining(["moveTo(1,2)", "lineTo(3,4)", "lineTo(5,6)", "stroke()"]));
  });
});

describe("outlineOf", () => {
  it("puts dark under a light colour and light under a dark one", () => {
    expect(outlineOf("#ffcc00")).toBe("#000000");
    expect(outlineOf("#111111")).toBe("#ffffff");
  });
});

describe("snapshotName", () => {
  const at = new Date(2026, 9, 14, 9, 5, 7);
  it("names the site and the moment", () => {
    expect(snapshotName("https://www.example.com/cart?id=1", at)).toBe("snapshot-example.com-20261014-090507.png");
  });
  it("uses a workspace file's name when there is no address", () => {
    expect(snapshotName("reports/sales report.html", at)).toBe("snapshot-sales-report.html-20261014-090507.png");
    expect(snapshotName("", at)).toBe("snapshot-20261014-090507.png");
  });
});
