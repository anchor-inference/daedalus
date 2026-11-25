// @vitest-environment jsdom
// The phone's drawer as a reader meets it: it closes on Escape and on Back and gives focus back.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "../i18n";
import { Drawer, closeDrawer, openDrawer, useDrawerOpen } from "./phone";

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setLang("en");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
  closeDrawer();
  vi.useRealTimers();
});

function Harness() {
  const open = useDrawerOpen();
  return (
    <>
      <button type="button" className="opener" onClick={openDrawer}>menu</button>
      <Drawer open={open} onClose={closeDrawer} label="Menu"><button type="button" className="inside">Inbox</button></Drawer>
    </>
  );
}

describe("Drawer", () => {
  it("opens from any button, closes on Escape and gives focus back to its opener", async () => {
    await act(async () => root.render(<Harness />));
    const opener = host.querySelector<HTMLButtonElement>(".opener")!;
    opener.focus();
    await act(async () => opener.click());
    const panel = host.querySelector<HTMLElement>(".ph-drawer")!;
    expect(host.querySelector(".ph-drawer-root.open")).not.toBeNull();
    expect(panel.hasAttribute("inert")).toBe(false);
    expect(document.activeElement).toBe(panel);
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(host.querySelector(".ph-drawer-root.open")).toBeNull();
    expect(panel.hasAttribute("inert")).toBe(true);
    expect(document.activeElement).toBe(opener);
  });

  it("takes a history entry while open, so Back closes it rather than leaving the page", async () => {
    // The previous test's close went Back, and jsdom delivers that asynchronously: let it land first,
    // or its popstate arrives in the middle of this test. history.length is not compared for the same
    // reason (a push after a Back replaces the forward entry instead of adding one).
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    await act(async () => root.render(<Harness />));
    await act(async () => host.querySelector<HTMLButtonElement>(".opener")!.click());
    expect((window.history.state as { layer?: boolean }).layer).toBe(true);
    await act(async () => {
      window.history.replaceState({ d: 0 }, "", window.location.href);
      window.dispatchEvent(new PopStateEvent("popstate", { state: { d: 0 } }));
    });
    expect(host.querySelector(".ph-drawer-root.open")).toBeNull();
  });

  it("closes on a tap on the scrim", async () => {
    await act(async () => root.render(<Harness />));
    await act(async () => host.querySelector<HTMLButtonElement>(".opener")!.click());
    await act(async () => host.querySelector<HTMLElement>(".ph-scrim")!.click());
    expect(host.querySelector(".ph-drawer-root.open")).toBeNull();
  });
});
