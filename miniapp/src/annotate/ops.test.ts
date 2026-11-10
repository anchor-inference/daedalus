// The editor's steps: what Undo and Redo walk through, a crop that keeps no speck and no pixel outside
// the picture, the size after crops and turns, a reopened picture's steps each undoable, and the cap
// that keeps a huge photo inside what a canvas can hold.

import { describe, expect, it } from "vitest";
import type { Mark } from "./draw";
import { EMPTY_HISTORY, MAX_PIXELS, cropRect, fitPixels, historyOf, historyReducer, sizeAfter, startsWith, type Op } from "./ops";

const arrow: Mark = { tool: "arrow", color: "#ff3b30", width: 3, from: { x: 10, y: 10 }, to: { x: 100, y: 60 } };
const box: Mark = { tool: "box", color: "#0a84ff", width: 3, from: { x: 10, y: 10 }, to: { x: 100, y: 60 } };
const one: Op = { kind: "mark", mark: arrow };
const two: Op = { kind: "mark", mark: box };
const crop: Op = { kind: "crop", rect: { x: 10, y: 20, w: 300, h: 200 } };
const turn: Op = { kind: "rotate" };

describe("historyReducer", () => {
  it("adds, undoes, redoes, clears and undoes the clear", () => {
    let state = historyReducer(EMPTY_HISTORY, { kind: "add", op: one });
    state = historyReducer(state, { kind: "add", op: crop });
    state = historyReducer(state, { kind: "undo" });
    expect(state.ops).toEqual([one]);
    state = historyReducer(state, { kind: "redo" });
    expect(state.ops).toEqual([one, crop]);
    state = historyReducer(state, { kind: "clear" });
    expect(state.ops).toEqual([]);
    state = historyReducer(state, { kind: "undo" });
    expect(state.ops).toEqual([one, crop]);
  });
  it("forgets what could be redone once something new is done", () => {
    let state = historyReducer(EMPTY_HISTORY, { kind: "add", op: one });
    state = historyReducer(state, { kind: "undo" });
    state = historyReducer(state, { kind: "add", op: two });
    expect(state.future).toEqual([]);
    expect(historyReducer(state, { kind: "redo" })).toBe(state);
  });
  it("records no step for a dot, a speck of crop, an empty clear or an undo with nothing behind it", () => {
    expect(historyReducer(EMPTY_HISTORY, { kind: "add", op: { kind: "mark", mark: { ...arrow, to: arrow.from } } })).toBe(EMPTY_HISTORY);
    expect(historyReducer(EMPTY_HISTORY, { kind: "add", op: { kind: "crop", rect: { x: 0, y: 0, w: 5, h: 300 } } })).toBe(EMPTY_HISTORY);
    expect(historyReducer(EMPTY_HISTORY, { kind: "clear" })).toBe(EMPTY_HISTORY);
    expect(historyReducer(EMPTY_HISTORY, { kind: "undo" })).toBe(EMPTY_HISTORY);
  });
  it("reopens on earlier steps with each one undoable", () => {
    let state = historyOf([one, turn, two]);
    expect(state.ops).toEqual([one, turn, two]);
    for (const left of [[one, turn], [one], []]) {
      state = historyReducer(state, { kind: "undo" });
      expect(state.ops).toEqual(left);
    }
    expect(historyReducer(state, { kind: "undo" })).toBe(state);
  });
});

describe("cropRect and sizeAfter", () => {
  it("holds a crop to whole pixels inside the picture", () => {
    expect(cropRect({ x: -4.2, y: 10.6, w: 2000, h: 50.2 }, 1280, 800)).toEqual({ x: 0, y: 11, w: 1280, h: 50 });
  });
  it("follows crops and quarter turns", () => {
    expect(sizeAfter(1280, 800, [])).toEqual({ width: 1280, height: 800 });
    expect(sizeAfter(1280, 800, [turn])).toEqual({ width: 800, height: 1280 });
    expect(sizeAfter(1280, 800, [one, crop, turn])).toEqual({ width: 200, height: 300 });
    expect(sizeAfter(1280, 800, [turn, turn, turn, turn])).toEqual({ width: 1280, height: 800 });
  });
});

describe("startsWith", () => {
  it("knows when only the new steps need drawing", () => {
    expect(startsWith([one, two], [one])).toBe(true);
    expect(startsWith([one, two], [])).toBe(true);
    expect(startsWith([one], [one, two])).toBe(false);
    expect(startsWith([one, two], [{ ...one }])).toBe(false);
  });
});

describe("fitPixels", () => {
  it("leaves a picture a canvas can hold as it is", () => {
    expect(fitPixels(3840, 2160)).toEqual({ width: 3840, height: 2160 });
  });
  it("shrinks a huge photo to the cap at its own proportions", () => {
    const size = fitPixels(8000, 6000);
    expect(size.width * size.height).toBeLessThanOrEqual(MAX_PIXELS);
    expect(size.width / size.height).toBeCloseTo(8000 / 6000, 2);
    expect(size.width).toBeGreaterThan(4600);
  });
});
