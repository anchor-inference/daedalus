import { describe, expect, it } from "vitest";
import { OVERLAP, clip, layoutDay, packLanes } from "./layout";

describe("overlap layout", () => {
  it("gives a lone event the whole column", () => {
    expect(layoutDay([{ id: "a", start: 540, end: 600 }]).get("a")).toEqual({ left: 0, width: 1, column: 0, columns: 1 });
  });

  it("puts overlapping events side by side and lets a free one stretch", () => {
    const placed = layoutDay([
      { id: "long", start: 540, end: 720 },
      { id: "first", start: 540, end: 600 },
      { id: "later", start: 630, end: 690 },
    ]);
    expect(placed.get("long")).toMatchObject({ left: 0, width: 0.5 * OVERLAP });
    expect(placed.get("first")).toMatchObject({ left: 0.5, width: 0.5 });
    expect(placed.get("later")).toMatchObject({ left: 0.5, width: 0.5 });
  });

  it("splits three at once into thirds and stretches the one with room beside it", () => {
    const placed = layoutDay([
      { id: "a", start: 540, end: 660 },
      { id: "b", start: 560, end: 600 },
      { id: "c", start: 570, end: 590 },
      { id: "d", start: 620, end: 650 },
    ]);
    expect(placed.get("a")?.columns).toBe(3);
    expect(placed.get("d")).toMatchObject({ column: 1, width: 2 / 3 });
    expect(placed.get("b")?.width).toBeCloseTo((1 / 3) * OVERLAP);
  });

  it("starts a new cluster after a gap", () => {
    const placed = layoutDay([
      { id: "a", start: 540, end: 600 },
      { id: "b", start: 560, end: 620 },
      { id: "c", start: 700, end: 760 },
    ]);
    expect(placed.get("c")).toMatchObject({ width: 1 });
  });

  it("treats a very short event as tall as it is drawn", () => {
    const placed = layoutDay([
      { id: "a", start: 540, end: 545 },
      { id: "b", start: 550, end: 555 },
    ]);
    expect(placed.get("a")?.columns).toBe(2);
  });
});

describe("all-day lanes", () => {
  it("stacks overlapping bars and reuses a lane that is free", () => {
    const lanes = packLanes([
      { id: "trip", first: 1, last: 4 },
      { id: "holiday", first: 2, last: 2 },
      { id: "later", first: 5, last: 6 },
    ]);
    expect(lanes.get("trip")).toBe(0);
    expect(lanes.get("holiday")).toBe(1);
    expect(lanes.get("later")).toBe(0);
  });

  it("clips a run to a row and says which ends are cut", () => {
    expect(clip(-2, 3, 7)).toEqual({ first: 0, last: 2, before: true, after: false });
    expect(clip(5, 9, 7)).toEqual({ first: 5, last: 6, before: false, after: true });
    expect(clip(8, 9, 7)).toBeNull();
  });
});

describe("ordered lanes", () => {
  it("keeps the given order when asked to", () => {
    const lanes = packLanes([{ id: "bar", first: 0, last: 0 }, { id: "long", first: 0, last: 3 }], true);
    expect(lanes.get("bar")).toBe(0);
    expect(lanes.get("long")).toBe(1);
  });
});
