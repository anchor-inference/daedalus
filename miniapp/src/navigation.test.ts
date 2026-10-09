// The menu's model without its markup: which groups an installation has, where the arrows go, and
// which key combinations are the shell's.

import { describe, expect, it } from "vitest";
import { GROUPS, latinKey, menuSections, moveIndex, shortcutFor } from "./navigation";

describe("the sections", () => {
  it("list every destination once, in the rail's old order, with the terminals after the board and the command-line agents after them", () => {
    const all = menuSections("server").flatMap((g) => g.items);
    expect(all).toEqual(["agents", "voice", "inbox", "board", "calendar", "diagrams", "terminals", "harnesses", "changes", "schedules", "services", "memory", "usage", "health"]);
    expect(new Set(all).size).toBe(all.length);
    expect(menuSections("server").map((g) => g.key)).toEqual(GROUPS.map((g) => g.key));
  });

  it("leave out what the installation does not have, and a group that is left empty", () => {
    const off = menuSections("off");
    expect(off.flatMap((g) => g.items)).not.toContain("changes");
    expect(off.find((g) => g.key === "autonomy")?.items).toEqual(["schedules", "services"]);
    expect(off.every((g) => g.items.length > 0)).toBe(true);
  });
});

describe("the arrows", () => {
  it("move down and up and wrap at both ends", () => {
    expect(moveIndex(0, "ArrowDown", 4)).toBe(1);
    expect(moveIndex(3, "ArrowDown", 4)).toBe(0);
    expect(moveIndex(0, "ArrowUp", 4)).toBe(3);
    expect(moveIndex(2, "ArrowUp", 4)).toBe(1);
  });

  it("start from the first item when nothing is focused", () => {
    expect(moveIndex(-1, "ArrowDown", 4)).toBe(0);
    expect(moveIndex(-1, "ArrowUp", 4)).toBe(3);
  });

  it("jump to the ends, and ignore every other key", () => {
    expect(moveIndex(2, "Home", 4)).toBe(0);
    expect(moveIndex(1, "End", 4)).toBe(3);
    expect(moveIndex(1, "Enter", 4)).toBeNull();
    expect(moveIndex(1, "a", 4)).toBeNull();
    expect(moveIndex(0, "ArrowDown", 0)).toBeNull();
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
