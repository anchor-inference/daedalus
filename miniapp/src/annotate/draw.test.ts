// The markup's arithmetic: where a pointer lands on the picture, what a tap leaves behind, how an arrow
// snaps, what the blur keeps of the pixels under it, and what the marks ask of a canvas.

import { describe, expect, it } from "vitest";
import { arrowHead, blurBlock, contrastOf, drawMark, fitBox, labelBox, meaningful, mosaic, rectOf, smoothPath, snapArrow, snapshotName, squareOf, strokeWidth, textSize, toPicture, type Mark, type Pen } from "./draw";

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
  it("drops a tap with the arrow or the box, an empty text, a one-point stroke and a sliver of blur", () => {
    expect(meaningful({ tool: "blur", from: { x: 0, y: 0 }, to: { x: 40, y: 2 }, block: 12 })).toBe(false);
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

/** A context that writes down what it was asked to do. */
function recorder(): Pen & { calls: string[] } {
  const calls: string[] = [];
  const note = (name: string) => (...args: unknown[]) => { calls.push(`${name}(${args.map((a) => (typeof a === "number" ? Math.round(a) : a)).join(",")})`); };
  return {
    calls, strokeStyle: "", fillStyle: "", lineWidth: 1, lineCap: "butt", lineJoin: "miter", font: "", textBaseline: "alphabetic",
    beginPath: note("beginPath"), moveTo: note("moveTo"), lineTo: note("lineTo"), closePath: note("closePath"), stroke: note("stroke"), fill: note("fill"),
    strokeRect: note("strokeRect"), fillRect: note("fillRect"), fillText: note("fillText"), save: note("save"), restore: note("restore"),
    ellipse: note("ellipse"), quadraticCurveTo: note("quadraticCurveTo"),
    // Every letter is ten pixels wide, so a label's plate can be checked by hand.
    measureText: (text: string) => ({ width: text.length * 10 }) as TextMetrics,
  } as unknown as Pen & { calls: string[] };
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
    // The head starts at the tip; the shaft stops inside it, so its round cap does not blunt the point.
    expect(pen.calls).toContain("moveTo(100,60)");
    expect(pen.calls).not.toContain("lineTo(100,60)");
  });
  it("sets text on a plate of its colour, in the colour that reads on it, line by line", () => {
    const pen = recorder();
    drawMark(pen, { tool: "text", color: "#ffcc00", size: 20, at: { x: 5, y: 5 }, text: "one\ntwo" });
    expect(pen.calls.filter((c) => c.startsWith("fillText"))).toEqual(["fillText(one,11,11)", "fillText(two,11,35)"]);
    expect(pen.calls.indexOf("fill()")).toBeLessThan(pen.calls.indexOf("fillText(one,11,11)"));
    expect(pen.fillStyle).toBe("#000000");
  });
  it("draws an ellipse inside the dragged rectangle", () => {
    const pen = recorder();
    drawMark(pen, { tool: "ellipse", color: "#fff", width: 2, from: { x: 100, y: 60 }, to: { x: 10, y: 10 } });
    expect(pen.calls).toContain("ellipse(55,35,45,25,0,0,6)");
  });
  it("leaves a blur to the canvas, since it reads the pixels under it", () => {
    const pen = recorder();
    drawMark(pen, { tool: "blur", from: { x: 0, y: 0 }, to: { x: 50, y: 50 }, block: 12 });
    expect(pen.calls).toEqual([]);
  });
  it("follows a pen stroke through its points", () => {
    const pen = recorder();
    drawMark(pen, { tool: "pen", color: "#fff", width: 2, points: [{ x: 1, y: 2 }, { x: 3, y: 4 }, { x: 5, y: 6 }] });
    expect(pen.calls).toEqual(expect.arrayContaining(["moveTo(1,2)", "quadraticCurveTo(3,4,4,5)", "lineTo(5,6)", "stroke()"]));
  });
});

describe("contrastOf and labelBox", () => {
  it("puts dark on a light colour and light on a dark one", () => {
    expect(contrastOf("#ffcc00")).toBe("#000000");
    expect(contrastOf("#111111")).toBe("#ffffff");
  });
  it("fits the plate to the widest line with a margin all round", () => {
    expect(labelBox(20, [30, 50])).toEqual({ pad: 6, line: 24, w: 62, h: 56, radius: 5 });
    expect(labelBox(20, [30]).h).toBe(32);
  });
});

describe("snapArrow", () => {
  const from = { x: 0, y: 0 };
  it("levels an arrow drawn a few degrees off, keeping its length", () => {
    const tip = snapArrow(from, { x: 100, y: 5 }, false);
    expect(tip.y).toBeCloseTo(0);
    expect(tip.x).toBeCloseTo(Math.hypot(100, 5));
  });
  it("snaps to the diagonal too, and leaves a plainly slanted arrow alone", () => {
    const tip = snapArrow(from, { x: 100, y: 95 }, false);
    expect(tip.x).toBeCloseTo(tip.y);
    expect(snapArrow(from, { x: 100, y: 60 }, false)).toEqual({ x: 100, y: 60 });
  });
  it("keeps to fifteen-degree steps with Shift, and to none when asked", () => {
    const tip = snapArrow(from, { x: 100, y: 60 }, false, true);
    expect(Math.atan2(tip.y, tip.x)).toBeCloseTo(Math.PI / 6);
    expect(snapArrow(from, { x: 100, y: 3 }, true)).toEqual({ x: 100, y: 3 });
  });
});

describe("squareOf", () => {
  it("makes the drag a square whichever way it went", () => {
    expect(squareOf({ x: 10, y: 10 }, { x: 40, y: 20 })).toEqual({ x: 40, y: 40 });
    expect(squareOf({ x: 50, y: 50 }, { x: 40, y: 0 })).toEqual({ x: 0, y: 0 });
  });
});

describe("smoothPath", () => {
  it("draws a two-point stroke straight", () => {
    const pen = recorder();
    smoothPath(pen, [{ x: 0, y: 0 }, { x: 4, y: 4 }]);
    expect(pen.calls).toEqual(["beginPath()", "moveTo(0,0)", "lineTo(4,4)"]);
  });
});

describe("mosaic", () => {
  it("replaces every square with the mean of all its pixels", () => {
    // A 4 by 2 strip: a black and a white pixel in each column pair, so each 2 by 2 square is one grey.
    const data = new Uint8ClampedArray(4 * 2 * 4);
    for (let i = 0; i < 8; i++) {
      const v = i % 2 ? 255 : 0;
      data.set([v, v, v, 255], i * 4);
    }
    mosaic(data, 4, 2, 2);
    for (let i = 0; i < 8; i++) expect(Array.from(data.slice(i * 4, i * 4 + 4))).toEqual([128, 128, 128, 255]);
  });
  it("averages the ragged squares at the edges by their own size", () => {
    const data = new Uint8ClampedArray([10, 0, 0, 255, 20, 0, 0, 255, 90, 0, 0, 255]);
    mosaic(data, 3, 1, 2);
    expect([data[0], data[4], data[8]]).toEqual([15, 15, 90]);
  });
  it("is coarse enough to swallow a line of text whole", () => {
    expect(blurBlock(1920, 1080)).toBe(40);
    expect(blurBlock(300, 200)).toBe(12);
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
