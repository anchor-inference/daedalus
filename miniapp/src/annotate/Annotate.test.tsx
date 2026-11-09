// @vitest-environment jsdom
// The Snapshot button and its editor: no button without a conversation to attach to, the editor over
// the window on a press, Escape that drops the words being typed before it closes anything, Cancel, and
// the keys that pick a tool by the key's place, not its letter. Then the pencil on a picture waiting in
// a composer: offered on pictures a canvas reads, Done in place of the file, and a second markup that
// opens on the original with the earlier steps back.

import { act as reactAct } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { setLang } from "../i18n";
import { SnapshotButton } from "./Annotate";
import { ComposeTarget } from "./compose";
import { MarkupButton, markable, pngName } from "./markup";
import type { Snapshot } from "./source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeAll(() => {
  setLang("en");
  // jsdom has neither a 2D context nor a ResizeObserver; the overlay draws nothing here and measures once.
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
  globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;
  // Nor can it decode a picture or encode a canvas: a bitmap is its size, and every canvas a tiny PNG.
  globalThis.createImageBitmap = (async () => ({ width: 1280, height: 800, close() {} })) as unknown as typeof createImageBitmap;
  HTMLCanvasElement.prototype.toBlob = function (done: BlobCallback) { done(new Blob(["png"], { type: "image/png" })); };
});

let root: Root | null = null;

async function draw(node: React.ReactNode) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  await reactAct(async () => root!.render(node));
}

afterEach(async () => {
  await reactAct(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

const shot: Snapshot = { image: null, width: 640, height: 400, where: "https://example.com/cart", text: "Page: https://example.com/cart" };

async function press(el: Element | null) {
  await reactAct(async () => (el as HTMLElement).click());
}

async function key(init: KeyboardEventInit) {
  await reactAct(async () => { document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, ...init })); });
}

describe("SnapshotButton", () => {
  it("is not offered without a conversation to attach to", async () => {
    await draw(<SnapshotButton capture={() => shot} toast={() => undefined} />);
    expect(document.querySelector(".annotate-open")).toBeNull();
  });

  it("opens over the window with the address, and Cancel closes it", async () => {
    await draw(<ComposeTarget.Provider value="s1"><SnapshotButton capture={() => shot} toast={() => undefined} /></ComposeTarget.Provider>);
    await press(document.querySelector(".annotate-open"));
    const overlay = document.querySelector(".annotate");
    expect(overlay?.parentElement).toBe(document.body);
    expect(overlay?.querySelector(".annotate-where")?.textContent).toBe("https://example.com/cart");
    expect(overlay?.querySelectorAll(".annotate-tool")).toHaveLength(7);
    expect(overlay?.querySelectorAll(".annotate-swatch")).toHaveLength(7);
    await press(document.querySelector(".annotate-cancel"));
    expect(document.querySelector(".annotate")).toBeNull();
  });

  it("says so when there is no picture to take", async () => {
    const toast = vi.fn();
    await draw(<ComposeTarget.Provider value="s1"><SnapshotButton capture={() => null} toast={toast} /></ComposeTarget.Provider>);
    await press(document.querySelector(".annotate-open"));
    expect(document.querySelector(".annotate")).toBeNull();
    expect(toast).toHaveBeenCalledWith("No picture of the page yet");
  });

  it("drops the words on Escape before it closes on the next one", async () => {
    await draw(<ComposeTarget.Provider value="s1"><SnapshotButton capture={() => shot} toast={() => undefined} /></ComposeTarget.Provider>);
    await press(document.querySelector(".annotate-open"));
    await press(document.querySelector(".annotate-tool[data-tool='text']"));
    await reactAct(async () => { document.querySelector(".annotate-canvas")!.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, button: 0, clientX: 20, clientY: 20 })); });
    expect(document.querySelector(".annotate-text")).not.toBeNull();
    await key({ key: "Escape" });
    expect(document.querySelector(".annotate-text")).toBeNull();
    expect(document.querySelector(".annotate")?.getAttribute("data-marks")).toBe("0");
    await key({ key: "Escape" });
    expect(document.querySelector(".annotate")).toBeNull();
  });

  it("picks a tool by the key's place on the keyboard, so a Russian layout works too", async () => {
    await draw(<ComposeTarget.Provider value="s1"><SnapshotButton capture={() => shot} toast={() => undefined} /></ComposeTarget.Provider>);
    await press(document.querySelector(".annotate-open"));
    // The key that types "o" in English types "щ" in Russian; its place is the same.
    await key({ key: "щ", code: "KeyO" });
    expect(document.querySelector(".annotate")?.getAttribute("data-tool")).toBe("ellipse");
    await key({ key: "b", code: "KeyB" });
    expect(document.querySelector(".annotate")?.getAttribute("data-tool")).toBe("blur");
    await key({ key: "]", code: "BracketRight" });
    expect(document.querySelector(".annotate-weight")?.getAttribute("data-weight")).toBe("2");
    await key({ key: "2", code: "Digit2" });
    expect(document.querySelectorAll(".annotate-swatch")[1].getAttribute("aria-checked")).toBe("true");
    await key({ key: "q", code: "KeyQ" });
    expect(document.querySelector(".annotate")?.getAttribute("data-marks")).toBe("1");
  });

  it("keeps a key it uses from the chat under it", async () => {
    const heard = vi.fn();
    document.addEventListener("keydown", heard);
    await draw(<ComposeTarget.Provider value="s1"><SnapshotButton capture={() => shot} toast={() => undefined} /></ComposeTarget.Provider>);
    await press(document.querySelector(".annotate-open"));
    await key({ key: "y", code: "KeyY" });
    await key({ key: "a", code: "KeyA" });
    document.removeEventListener("keydown", heard);
    expect(heard).not.toHaveBeenCalled();
  });
});

describe("the pencil on a composer's picture", () => {
  const png = (name = "pasted-1.png") => new File(["picture"], name, { type: "image/png" });

  it("is offered on the pictures a canvas reads, not on a drawing or a document", () => {
    expect(markable(png())).toBe(true);
    expect(markable(new File(["x"], "photo.JPG", { type: "" }))).toBe(true);
    expect(markable(new File(["<svg/>"], "logo.svg", { type: "image/svg+xml" }))).toBe(false);
    expect(markable(new File(["x"], "notes.txt", { type: "text/plain" }))).toBe(false);
  });

  it("names the result after the original, as a PNG", () => {
    expect(pngName("photo.jpeg")).toBe("photo.png");
    expect(pngName("pasted-1.png")).toBe("pasted-1.png");
    expect(pngName(".jpg")).toBe("picture.png");
  });

  it("leaves the file as it was on Cancel, and on Done with nothing drawn", async () => {
    const replace = vi.fn();
    await draw(<MarkupButton file={png()} onReplace={replace} />);
    await press(document.querySelector(".attachment-markup"));
    expect(document.querySelector(".annotate-attach")?.textContent).toBe("Done");
    await press(document.querySelector(".annotate-cancel"));
    expect(document.querySelector(".annotate")).toBeNull();
    await press(document.querySelector(".attachment-markup"));
    await press(document.querySelector(".annotate-attach"));
    expect(document.querySelector(".annotate")).toBeNull();
    expect(replace).not.toHaveBeenCalled();
  });

  it("puts the marked-up PNG in the file's place, and reopens it on the original with the steps back", async () => {
    const original = new File(["picture"], "photo.jpg", { type: "image/jpeg" });
    const replace = vi.fn();
    await draw(<MarkupButton file={original} onReplace={replace} />);
    await press(document.querySelector(".attachment-markup"));
    await press(document.querySelector(".annotate-rotate"));
    await press(document.querySelector(".annotate-attach"));
    expect(replace).toHaveBeenCalledTimes(1);
    const marked = replace.mock.calls[0][0] as File;
    expect([marked.name, marked.type]).toEqual(["photo.png", "image/png"]);

    await reactAct(async () => root!.render(<MarkupButton file={marked} onReplace={replace} />));
    await press(document.querySelector(".attachment-markup"));
    expect(document.querySelector(".annotate")?.getAttribute("data-marks")).toBe("1");
    // Every step undone and Done: the original goes back, not a copy of it.
    await press(document.querySelector(".annotate-undo"));
    await press(document.querySelector(".annotate-attach"));
    expect(replace).toHaveBeenLastCalledWith(original);
  });
});
