// The menu's keyboard without its markup: where the arrows go, and which key combinations are the shell's.

import { describe, expect, it } from "vitest";
import { type Box, SEARCH, latinKey, moveInGrid, shortcutFor } from "./navigation";

// The launcher as it is drawn: four tiles, then a catalogue three across with a short last row,
// then the foot's three entries, each row 40 px under the one before.
const row = (y: number, xs: number[], w: number): Box[] => xs.map((x) => ({ x, y, w, h: 32 }));
const LAUNCHER: Box[] = [
  ...row(0, [0, 150, 300, 450], 140), // 0-3 tiles
  ...row(80, [0, 200, 400], 190), // 4-6
  ...row(120, [0, 200, 400], 190), // 7-9
  ...row(160, [0], 190), // 10
  ...row(220, [0, 120, 240], 110), // 11-13 the foot
];

describe("the arrows", () => {
  it("go from the search field into the first item, and from the top row back to it", () => {
    expect(moveInGrid(LAUNCHER, SEARCH, "ArrowDown")).toBe(0);
    expect(moveInGrid(LAUNCHER, SEARCH, "ArrowUp")).toBeNull();
    expect(moveInGrid(LAUNCHER, SEARCH, "ArrowRight")).toBeNull();
    expect(moveInGrid(LAUNCHER, 2, "ArrowUp")).toBe(SEARCH);
  });

  it("move along a row, and carry on into the next one past its end", () => {
    expect(moveInGrid(LAUNCHER, 0, "ArrowRight")).toBe(1);
    expect(moveInGrid(LAUNCHER, 5, "ArrowLeft")).toBe(4);
    expect(moveInGrid(LAUNCHER, 3, "ArrowRight")).toBe(4);
    expect(moveInGrid(LAUNCHER, 4, "ArrowLeft")).toBe(3);
    expect(moveInGrid(LAUNCHER, 13, "ArrowRight")).toBeNull();
    expect(moveInGrid(LAUNCHER, 0, "ArrowLeft")).toBeNull();
  });

  it("go down to the nearest item of the next row, whatever its column count", () => {
    // The fourth tile's middle is at 520: the catalogue's third column (495) is nearest.
    expect(moveInGrid(LAUNCHER, 3, "ArrowDown")).toBe(6);
    expect(moveInGrid(LAUNCHER, 1, "ArrowDown")).toBe(5);
    expect(moveInGrid(LAUNCHER, 9, "ArrowDown")).toBe(10);
    // The short last row is the next row down for every column above it.
    expect(moveInGrid(LAUNCHER, 6, "ArrowUp")).toBe(3);
    expect(moveInGrid(LAUNCHER, 10, "ArrowDown")).toBe(11);
    expect(moveInGrid(LAUNCHER, 12, "ArrowDown")).toBeNull();
  });

  it("jump to the ends, and ignore every other key", () => {
    expect(moveInGrid(LAUNCHER, 5, "Home")).toBe(0);
    expect(moveInGrid(LAUNCHER, 5, "End")).toBe(13);
    expect(moveInGrid(LAUNCHER, 5, "Enter")).toBeNull();
    expect(moveInGrid(LAUNCHER, 5, "a")).toBeNull();
    expect(moveInGrid([], SEARCH, "ArrowDown")).toBeNull();
  });
});

describe("the shortcuts", () => {
  const key = (k: string, mods: Partial<{ meta: boolean; ctrl: boolean; shift: boolean; alt: boolean }> = {}) => ({ key: k, metaKey: !!mods.meta, ctrlKey: !!mods.ctrl, shiftKey: !!mods.shift, altKey: !!mods.alt });

  it("open the menu on Ctrl or ⌘ with Shift and M", () => {
    expect(shortcutFor(key("M", { meta: true, shift: true }))).toBe("menu");
    expect(shortcutFor(key("m", { ctrl: true, shift: true }))).toBe("menu");
    expect(shortcutFor(key("m", { meta: true }))).toBeNull();
    expect(shortcutFor(key("m", { shift: true }))).toBeNull();
  });

  it("read the key, not the letter, so a Russian layout keeps them", () => {
    expect(shortcutFor({ ...key("Ь", { ctrl: true, shift: true }), code: "KeyM" })).toBe("menu");
    expect(latinKey({ key: "л", code: "KeyK" })).toBe("k");
    expect(latinKey({ key: "ы", code: "KeyS" })).toBe("s");
    expect(latinKey({ key: "\\", code: "Backslash" })).toBe("\\");
  });

  it("start a new chat on Ctrl or ⌘ with Shift and O, on any layout", () => {
    expect(shortcutFor(key("O", { ctrl: true, shift: true }))).toBe("newchat");
    expect(shortcutFor({ ...key("Щ", { meta: true, shift: true }), code: "KeyO" })).toBe("newchat");
    expect(shortcutFor(key("o", { ctrl: true }))).toBeNull();
  });

  it("fold the sidebar on Ctrl or ⌘ with backslash", () => {
    expect(shortcutFor(key("\\", { meta: true }))).toBe("sidebar");
    expect(shortcutFor(key("\\", { ctrl: true }))).toBe("sidebar");
    expect(shortcutFor(key("\\"))).toBeNull();
    expect(shortcutFor(key("\\", { ctrl: true, shift: true }))).toBeNull();
  });

  it("leave Alt combinations to the browser", () => {
    expect(shortcutFor(key("\\", { ctrl: true, alt: true }))).toBeNull();
    expect(shortcutFor(key("m", { ctrl: true, shift: true, alt: true }))).toBeNull();
  });
});
