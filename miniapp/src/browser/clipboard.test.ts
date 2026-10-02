// @vitest-environment jsdom
// The clipboard between the operator and the page: a copy asks the page and writes the operator's
// clipboard, starting the write in the gesture where the browser allows it and offering a button when
// it does not; a password is never copied; a paste goes onto the page as text, and a paste too long to
// arrive whole is refused rather than sent with holes.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { copyFromPage, MAX_PASTE_CHARS, pasteFromClipboard, pasteText } from "./clipboard";
import { CopyError, type Copied, type LiveView } from "./live";
import type { InputMessage } from "./protocol";

const toasts = vi.hoisted(() => [] as { text: string; action?: { label: string; run: () => void } }[]);
vi.mock("../ui/dialogs", () => ({
  toast: (text: string, opts: { action?: { label: string; run: () => void } } = {}) => {
    toasts.push({ text, action: opts.action });
    return toasts.length;
  },
}));

function fakeLive(answer: Promise<Copied>): LiveView & { sent: InputMessage[] } {
  const sent: InputMessage[] = [];
  return { sent, copy: () => answer, input: (m: InputMessage) => (sent.push(m), true) } as unknown as LiveView & { sent: InputMessage[] };
}

type Clip = { write?: (items: unknown[]) => Promise<void>; writeText?: (s: string) => Promise<void>; readText?: () => Promise<string> };

function setClipboard(clip: Clip | undefined): void {
  Object.defineProperty(navigator, "clipboard", { value: clip, configurable: true });
}

class FakeItem {
  constructor(readonly items: Record<string, Promise<Blob>>) {}
}

beforeEach(() => {
  toasts.length = 0;
});

afterEach(() => {
  setClipboard(undefined);
  vi.unstubAllGlobals();
});

describe("copying from the page", () => {
  it("starts the write in the gesture and fills it with the page's answer", async () => {
    let written: Promise<Blob> | null = null;
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("ClipboardItem", FakeItem);
    setClipboard({
      write: (items) => {
        // Called before the answer came: the item holds a promise, as Safari needs.
        written = (items[0] as FakeItem).items["text/plain"];
        return written.then(() => undefined);
      },
      writeText,
    });
    await copyFromPage(fakeLive(Promise.resolve({ text: "someone@example.com", truncated: false, withheld: false })));
    expect(written).not.toBeNull();
    expect(await (await written!).text()).toBe("someone@example.com");
    expect(writeText).not.toHaveBeenCalled();
    expect(toasts.map((x) => x.text)).toEqual(["Copied 19 characters"]);
  });

  it("writes plainly where the item is refused, and offers a button where that is refused too", async () => {
    vi.stubGlobal("ClipboardItem", FakeItem);
    const writeText = vi.fn<(s: string) => Promise<void>>(async () => {
      throw new Error("not allowed");
    });
    setClipboard({ write: async () => { throw new Error("no promise items"); }, writeText });
    await copyFromPage(fakeLive(Promise.resolve({ text: "abc", truncated: false, withheld: false })));
    expect(writeText).toHaveBeenCalledWith("abc");
    expect(toasts).toHaveLength(1);
    expect(toasts[0].text).toBe("3 characters are ready to copy");
    // The button's own tap is a gesture: the write is allowed there.
    writeText.mockImplementation(async () => undefined);
    toasts[0].action!.run();
    await vi.waitFor(() => expect(toasts.map((x) => x.text)).toContain("Copied 3 characters"));
  });

  it("never copies a password field, and says when nothing is selected or the page cannot answer", async () => {
    const writeText = vi.fn(async () => undefined);
    setClipboard({ writeText });
    await copyFromPage(fakeLive(Promise.resolve({ text: "", truncated: false, withheld: true })));
    await copyFromPage(fakeLive(Promise.resolve({ text: "", truncated: false, withheld: false })));
    await copyFromPage(fakeLive(Promise.reject(new CopyError("a dialog is open"))));
    await copyFromPage(fakeLive(Promise.reject(new CopyError("timeout"))));
    expect(writeText).not.toHaveBeenCalled();
    expect(toasts.map((x) => x.text)).toEqual([
      "A password field is never copied",
      "Nothing is selected on the page",
      "Could not read the page's selection",
      "The page did not answer in time",
    ]);
  });

  it("says when the selection was longer than a copy carries", async () => {
    setClipboard({ writeText: async () => undefined });
    await copyFromPage(fakeLive(Promise.resolve({ text: "x".repeat(5), truncated: true, withheld: false })));
    expect(toasts[0].text).toBe("Copied the first 5 characters: the selection is longer");
  });
});

describe("pasting onto the page", () => {
  it("puts the operator's clipboard on the page as text", async () => {
    setClipboard({ readText: async () => "пароль-1" });
    const live = fakeLive(new Promise<Copied>(() => undefined));
    await pasteFromClipboard(live, true);
    expect(live.sent).toEqual([{ t: "text", text: "пароль-1" }]);
    expect(toasts.map((x) => x.text)).toEqual(["Pasted 8 characters"]);
  });

  it("points a phone at the line under the page when the clipboard may not be read", async () => {
    setClipboard({ readText: async () => { throw new Error("denied"); } });
    const live = fakeLive(new Promise<Copied>(() => undefined));
    await pasteFromClipboard(live, true);
    await pasteFromClipboard(live, false);
    expect(live.sent).toEqual([]);
    expect(toasts[0].text).toMatch(/long-press the line below/);
    expect(toasts[1].text).toMatch(/Ctrl\+V/);
  });

  it("refuses a paste too long to arrive whole", () => {
    const live = fakeLive(new Promise<Copied>(() => undefined));
    expect(pasteText(live, "x".repeat(MAX_PASTE_CHARS + 1))).toBe(false);
    expect(live.sent).toEqual([]);
    expect(pasteText(live, "x".repeat(MAX_PASTE_CHARS))).toBe(true);
  });
});
