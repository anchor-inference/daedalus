// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { insideTerminal, KeyLike, reservedKey } from "./keys";

const key = (code: string, mods: Partial<KeyLike> = {}): KeyLike => ({ code, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, ...mods });
const linux = { mac: false, altScreen: false };
const mac = { mac: true, altScreen: false };

describe("reservedKey", () => {
  it("takes Ctrl+` by the physical key, whatever the layout types", () => {
    // On a Russian layout the same key reports key "ё"; only `code` is read.
    expect(reservedKey(key("Backquote", { ctrlKey: true }), linux)).toBe("toggle-dock");
    expect(reservedKey(key("Backquote", { ctrlKey: true }), mac)).toBe("toggle-dock");
  });

  it("leaves the shell's own control keys to the shell", () => {
    for (const code of ["KeyK", "Backslash", "KeyR", "KeyC", "KeyV", "KeyD", "KeyL", "Period", "KeyF"]) {
      expect(reservedKey(key(code, { ctrlKey: true }), linux)).toBeNull();
    }
    expect(reservedKey(key("KeyC", { ctrlKey: true }), mac)).toBeNull();
  });

  it("copies and pastes with Ctrl+Shift+C/V, and with Cmd+C/V on a Mac", () => {
    expect(reservedKey(key("KeyC", { ctrlKey: true, shiftKey: true }), linux)).toBe("copy");
    expect(reservedKey(key("KeyV", { ctrlKey: true, shiftKey: true }), linux)).toBe("paste");
    expect(reservedKey(key("KeyC", { metaKey: true }), mac)).toBe("copy");
    expect(reservedKey(key("KeyV", { metaKey: true }), mac)).toBe("paste");
    // Cmd means nothing to a terminal elsewhere, and is not taken there.
    expect(reservedKey(key("KeyC", { metaKey: true }), linux)).toBeNull();
  });

  it("copies with plain Ctrl+C only while something is selected, and never on a Mac", () => {
    const selected = { ...linux, selection: true };
    expect(reservedKey(key("KeyC", { ctrlKey: true }), selected)).toBe("copy-clear");
    // Nothing selected: the interrupt, as always.
    expect(reservedKey(key("KeyC", { ctrlKey: true }), linux)).toBeNull();
    expect(reservedKey(key("KeyC", { ctrlKey: true }), { ...linux, mouseProgram: true })).toBeNull();
    // On a Mac Ctrl+C is the interrupt even over a selection; Cmd+C is the copy.
    expect(reservedKey(key("KeyC", { ctrlKey: true }), { ...mac, selection: true })).toBeNull();
    expect(reservedKey(key("KeyC", { metaKey: true }), { ...mac, selection: true })).toBe("copy");
    // Ctrl+Shift+C copies a selection and leaves it in place.
    expect(reservedKey(key("KeyC", { ctrlKey: true, shiftKey: true }), selected)).toBe("copy");
  });

  it("hands the copy keys to a program that reports the mouse when xterm.js selected nothing", () => {
    // Claude Code's full-screen view draws its own selection and copies it on Cmd+C or Ctrl+Shift+C.
    const program = { mouseProgram: true };
    expect(reservedKey(key("KeyC", { metaKey: true }), { ...mac, ...program })).toBeNull();
    expect(reservedKey(key("KeyC", { ctrlKey: true, shiftKey: true }), { ...linux, ...program })).toBeNull();
    // A Shift- or Option-drag selection is xterm.js's own, and is copied here.
    expect(reservedKey(key("KeyC", { metaKey: true }), { ...mac, ...program, selection: true })).toBe("copy");
    // The program already copied, but the browser refused it: the key delivers what is held.
    expect(reservedKey(key("KeyC", { metaKey: true }), { ...mac, ...program, held: true })).toBe("copy");
    expect(reservedKey(key("KeyC", { ctrlKey: true, shiftKey: true }), { ...linux, ...program, held: true })).toBe("copy");
    // Over a plain shell the keys stay taken: Ctrl+Shift+C would reach it as an interrupt.
    expect(reservedKey(key("KeyC", { ctrlKey: true, shiftKey: true }), linux)).toBe("copy");
    expect(reservedKey(key("KeyC", { metaKey: true }), mac)).toBe("copy");
    // Paste is the browser's in every case.
    expect(reservedKey(key("KeyV", { metaKey: true }), { ...mac, ...program })).toBe("paste");
  });

  it("opens search with Ctrl+Shift+F and sizes the font with Ctrl+=, Ctrl+- and Ctrl+0", () => {
    expect(reservedKey(key("KeyF", { ctrlKey: true, shiftKey: true }), linux)).toBe("search");
    expect(reservedKey(key("Equal", { ctrlKey: true }), linux)).toBe("font-bigger");
    expect(reservedKey(key("Equal", { ctrlKey: true, shiftKey: true }), linux)).toBe("font-bigger");
    expect(reservedKey(key("Minus", { ctrlKey: true }), linux)).toBe("font-smaller");
    expect(reservedKey(key("Digit0", { ctrlKey: true }), linux)).toBe("font-reset");
    expect(reservedKey(key("NumpadAdd", { ctrlKey: true }), linux)).toBe("font-bigger");
  });

  it("jumps between marks with Ctrl+↑/↓ only outside the alternate screen", () => {
    expect(reservedKey(key("ArrowUp", { ctrlKey: true }), linux)).toBe("previous-mark");
    expect(reservedKey(key("ArrowDown", { ctrlKey: true }), linux)).toBe("next-mark");
    expect(reservedKey(key("ArrowUp", { ctrlKey: true }), { mac: false, altScreen: true })).toBeNull();
  });

  it("never takes a key with Alt held (AltGr layouts type characters that way)", () => {
    expect(reservedKey(key("KeyC", { ctrlKey: true, shiftKey: true, altKey: true }), linux)).toBeNull();
    expect(reservedKey(key("Backquote", { ctrlKey: true, altKey: true }), linux)).toBeNull();
  });

  it("leaves plain keys alone", () => {
    expect(reservedKey(key("KeyA"), linux)).toBeNull();
    expect(reservedKey(key("Backquote"), linux)).toBeNull();
    expect(reservedKey(key("ArrowUp"), linux)).toBeNull();
  });
});

describe("insideTerminal", () => {
  it("knows a target inside a terminal from one outside", () => {
    document.body.innerHTML = `<div class="xterm"><textarea id="in"></textarea></div><div data-terminal><span id="strip"></span></div><input id="out">`;
    expect(insideTerminal(document.getElementById("in"))).toBe(true);
    expect(insideTerminal(document.getElementById("strip"))).toBe(true);
    expect(insideTerminal(document.getElementById("out"))).toBe(false);
    expect(insideTerminal(null)).toBe(false);
    expect(insideTerminal(window)).toBe(false);
  });
});
