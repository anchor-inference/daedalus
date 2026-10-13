// @vitest-environment jsdom
// The Snapshot button and its overlay: no button without a conversation to attach to, the overlay over
// the window on a press, Escape that drops the words being typed before it closes anything, and Cancel.

import { act as reactAct } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { setLang } from "../i18n";
import { SnapshotButton } from "./Annotate";
import { ComposeTarget } from "./compose";
import type { Snapshot } from "./source";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

beforeAll(() => {
  setLang("en");
  // jsdom has neither a 2D context nor a ResizeObserver; the overlay draws nothing here and measures once.
  HTMLCanvasElement.prototype.getContext = (() => null) as typeof HTMLCanvasElement.prototype.getContext;
  globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver;
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
    expect(overlay?.querySelectorAll(".annotate-tool")).toHaveLength(4);
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
});
