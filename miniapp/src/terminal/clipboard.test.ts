// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { copyNow, copyText } from "./clipboard";

/** A copy command as a browser runs it in a key press: a `copy` event whose clipboard the page may fill. */
function gestureCopy(store: { text?: string }) {
  return vi.fn(() => {
    const data = { setData: (_type: string, value: string) => { store.text = value; } };
    const event = new Event("copy", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "clipboardData", { value: data });
    (document.activeElement ?? document.body).dispatchEvent(event);
    return true;
  });
}

function setClipboard(value: unknown) {
  Object.defineProperty(navigator, "clipboard", { value, configurable: true });
}

afterEach(() => {
  setClipboard(undefined);
  document.body.innerHTML = "";
});

describe("copyNow", () => {
  it("fills the copy event's clipboard and leaves the focus in the terminal", () => {
    document.body.innerHTML = `<div class="xterm"><textarea class="xterm-helper-textarea"></textarea></div>`;
    const terminal = document.querySelector("textarea")!;
    terminal.focus();
    // xterm.js's own copy listener must not put its empty selection over the text.
    const xterm = vi.fn();
    terminal.addEventListener("copy", xterm);
    const store: { text?: string } = {};
    document.execCommand = gestureCopy(store);
    expect(copyNow("copy me")).toBe(true);
    expect(store.text).toBe("copy me");
    expect(xterm).not.toHaveBeenCalled();
    expect(document.activeElement).toBe(terminal);
    expect(document.querySelectorAll("textarea").length).toBe(1);
  });

  it("falls back to a selected field and gives the focus back", () => {
    document.body.innerHTML = `<textarea id="terminal"></textarea>`;
    const terminal = document.getElementById("terminal") as HTMLTextAreaElement;
    terminal.focus();
    // A webview that fires no copy event without a selection, but copies a selected field.
    let copied = "";
    document.execCommand = vi.fn(() => {
      const field = Array.from(document.querySelectorAll("textarea")).find((f) => f !== terminal && f.selectionEnd > f.selectionStart);
      if (!field) return false;
      copied = field.value;
      return true;
    });
    expect(copyNow("from the field")).toBe(true);
    expect(copied).toBe("from the field");
    expect(document.activeElement).toBe(terminal);
    expect(document.querySelectorAll("textarea").length).toBe(1);
  });
});

describe("copyText", () => {
  it("is refused outside a gesture on a page with no clipboard API (plain http)", async () => {
    document.execCommand = vi.fn(() => false);
    setClipboard(undefined);
    expect(await copyText("held")).toBe(false);
  });

  it("uses the asynchronous clipboard where the copy command is refused but the API exists", async () => {
    document.execCommand = vi.fn(() => false);
    const writeText = vi.fn(() => Promise.resolve());
    setClipboard({ writeText });
    expect(await copyText("osc 52")).toBe(true);
    expect(writeText).toHaveBeenCalledWith("osc 52");
  });

  it("writes in the gesture before anything asynchronous", async () => {
    const store: { text?: string } = {};
    document.execCommand = gestureCopy(store);
    const writeText = vi.fn(() => Promise.resolve());
    setClipboard({ writeText });
    const pending = copyText("now");
    // Synchronously, before the promise settles: a key press's moment is over by the first await.
    expect(store.text).toBe("now");
    expect(await pending).toBe(true);
    expect(writeText).not.toHaveBeenCalled();
  });
});
