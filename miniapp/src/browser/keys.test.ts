import { describe, expect, it } from "vitest";
import { clipboardChord, isKeyPress, keyInput, modsOf, mouseButton, tapKey, viewerChord } from "./keys";

const key = (k: string, code = "", mods: Partial<Record<"altKey" | "ctrlKey" | "metaKey" | "shiftKey", boolean>> = {}, keyCode = 0) => ({ key: k, code, keyCode, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, ...mods });

describe("the keyboard on the page", () => {
  it("writes the modifier bits as CDP reads them", () => {
    expect(modsOf(key("a", "KeyA", { altKey: true }))).toBe(1);
    expect(modsOf(key("a", "KeyA", { ctrlKey: true }))).toBe(2);
    expect(modsOf(key("a", "KeyA", { metaKey: true }))).toBe(4);
    expect(modsOf(key("a", "KeyA", { shiftKey: true, ctrlKey: true }))).toBe(10);
  });

  // The table: what goes as a press, what is left to arrive as text.
  const table: [string, ReturnType<typeof key>, boolean][] = [
    ["Enter", key("Enter", "Enter"), true],
    ["Tab", key("Tab", "Tab"), true],
    ["Backspace", key("Backspace", "Backspace"), true],
    ["an arrow", key("ArrowDown", "ArrowDown"), true],
    ["Escape", key("Escape", "Escape"), true],
    ["F5", key("F5", "F5"), true],
    ["a letter", key("a", "KeyA"), false],
    ["a capital", key("A", "KeyA", { shiftKey: true }), false],
    ["a Cyrillic letter", key("ж", "Semicolon"), false],
    ["Ctrl+A", key("a", "KeyA", { ctrlKey: true }), true],
    ["Cmd+C", key("c", "KeyC", { metaKey: true }), true],
    ["Option+e (a character on a Mac)", key("´", "KeyE", { altKey: true }), false],
    ["a lone Shift", key("Shift", "ShiftLeft", { shiftKey: true }), false],
    ["a dead key", key("Dead", "Quote"), false],
    ["an IME's Process", key("Process", "KeyA"), false],
  ];
  for (const [name, e, pressed] of table) {
    it(`sends ${name} as ${pressed ? "a key" : "text"}`, () => expect(isKeyPress(e)).toBe(pressed));
  }

  it("writes the golden Ctrl+A press", () => {
    expect(keyInput(key("a", "KeyA", { ctrlKey: true }, 65), "down")).toEqual({ t: "key", type: "down", key: "a", code: "KeyA", key_code: 65, mods: 2 });
  });

  it("gives Enter its carriage return going down, and nothing going up", () => {
    expect(keyInput(key("Enter", "Enter"), "down")).toEqual({ t: "key", type: "down", key: "Enter", code: "Enter", key_code: 13, text: "\r", mods: 0 });
    expect(keyInput(key("Enter", "Enter"), "up").text).toBeUndefined();
    expect(keyInput(key("Enter", "Enter", { ctrlKey: true }), "down").text).toBeUndefined();
  });

  it("knows the virtual codes a page checks", () => {
    expect(keyInput(key("ArrowLeft", "ArrowLeft"), "down").key_code).toBe(37);
    expect(keyInput(key("F12", "F12"), "down").key_code).toBe(123);
    expect(keyInput(key("z", "KeyZ", { ctrlKey: true }), "down").key_code).toBe(90);
  });

  it("taps a key from the phone's row as down then up", () => {
    expect(tapKey("Backspace").map((k) => `${k.type}:${k.key}:${k.key_code}`)).toEqual(["down:Backspace:8", "up:Backspace:8"]);
  });

  it("names the mouse's buttons", () => {
    expect([0, 1, 2].map(mouseButton)).toEqual(["left", "middle", "right"]);
  });
});

describe("the viewer's own chords", () => {
  it("catches the address bar, reload, back and forward on a Mac", () => {
    expect(viewerChord(key("l", "KeyL", { metaKey: true }), true)).toBe("address");
    expect(viewerChord(key("r", "KeyR", { metaKey: true }), true)).toBe("reload");
    expect(viewerChord(key("[", "BracketLeft", { metaKey: true }), true)).toBe("back");
    expect(viewerChord(key("]", "BracketRight", { metaKey: true }), true)).toBe("forward");
    expect(viewerChord(key("w", "KeyW", { metaKey: true }), true)).toBe("swallow");
  });

  it("uses Ctrl elsewhere, and Alt with an arrow for history", () => {
    expect(viewerChord(key("l", "KeyL", { ctrlKey: true }), false)).toBe("address");
    expect(viewerChord(key("ArrowLeft", "ArrowLeft", { altKey: true }), false)).toBe("back");
    expect(viewerChord(key("F5", "F5"), false)).toBe("reload");
  });

  it("leaves the page its own shortcuts", () => {
    expect(viewerChord(key("a", "KeyA", { ctrlKey: true }), false)).toBeNull();
    expect(viewerChord(key("l", "KeyL", { ctrlKey: true }), true)).toBeNull();
    expect(viewerChord(key("Enter", "Enter"), false)).toBeNull();
  });
});

describe("the clipboard's chords", () => {
  it("leaves a paste to the operator's clipboard rather than the page's", () => {
    expect(clipboardChord(key("v", "KeyV", { ctrlKey: true }), false)).toBe("paste");
    expect(clipboardChord(key("V", "KeyV", { ctrlKey: true, shiftKey: true }), false)).toBe("paste");
    expect(clipboardChord(key("Insert", "Insert", { shiftKey: true }), false)).toBe("paste");
    expect(clipboardChord(key("v", "KeyV", { metaKey: true }), true)).toBe("paste");
    // A Russian layout: the letter is "м", the key is still V.
    expect(clipboardChord(key("м", "KeyV", { ctrlKey: true }), false)).toBe("paste");
  });

  it("asks the page for its selection on a copy or a cut", () => {
    expect(clipboardChord(key("c", "KeyC", { ctrlKey: true }), false)).toBe("copy");
    expect(clipboardChord(key("x", "KeyX", { ctrlKey: true }), false)).toBe("cut");
    expect(clipboardChord(key("Insert", "Insert", { ctrlKey: true }), false)).toBe("copy");
    expect(clipboardChord(key("c", "KeyC", { metaKey: true }), true)).toBe("copy");
    expect(clipboardChord(key("с", "KeyC", { ctrlKey: true }), false)).toBe("copy");
  });

  it("leaves everything else, and the other platform's modifier, alone", () => {
    expect(clipboardChord(key("v", "KeyV"), false)).toBeNull();
    expect(clipboardChord(key("v", "KeyV", { ctrlKey: true }), true)).toBeNull();
    expect(clipboardChord(key("c", "KeyC", { metaKey: true }), false)).toBeNull();
    expect(clipboardChord(key("v", "KeyV", { ctrlKey: true, altKey: true }), false)).toBeNull();
    // Ctrl+Shift+C is the developer tools' chord on many pages, not a copy.
    expect(clipboardChord(key("C", "KeyC", { ctrlKey: true, shiftKey: true }), false)).toBeNull();
    expect(clipboardChord(key("a", "KeyA", { ctrlKey: true }), false)).toBeNull();
  });

  it("leaves the chords themselves keys, so the viewer asks about the clipboard first", () => {
    expect(isKeyPress(key("c", "KeyC", { ctrlKey: true }))).toBe(true);
    expect(isKeyPress(key("v", "KeyV", { ctrlKey: true }))).toBe(true);
  });
});
