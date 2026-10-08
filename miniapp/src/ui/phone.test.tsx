// @vitest-environment jsdom
// The phone's primitives as a reader meets them: a row opens on a tap, its commands open on a long
// press, on a right click and from its ⋮ (so no command needs a gesture), the drawer closes on Escape
// and on Back and gives focus back, and a sheet lists the destructive command last.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "../i18n";
import { ActionSheet, Drawer, ListRow, closeDrawer, openDrawer, useDrawerOpen } from "./phone";

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

// Titles of invented chats, held in constants: hardcoded.test.ts reads `title="…"` in any source as
// words a reader would see.
const CHECKOUT = "Checkout page";
const DIGEST = "Weekly digest";
const menu = () => Array.from(document.querySelectorAll<HTMLButtonElement>(".ph-mrow"));

describe("ListRow", () => {
  it("opens on a tap and keeps every command behind its ⋮", async () => {
    const onOpen = vi.fn();
    const rename = vi.fn();
    await act(async () => root.render(<ListRow title={CHECKOUT} meta="Working" onOpen={onOpen} actions={[{ label: "Rename", onSelect: rename }, { label: "Delete", danger: true, onSelect: vi.fn() }]} />));
    await act(async () => host.querySelector<HTMLElement>(".ph-row")!.click());
    expect(onOpen).toHaveBeenCalledTimes(1);
    await act(async () => host.querySelector<HTMLButtonElement>(".ph-row-more")!.click());
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".ph-preview")?.textContent).toContain(CHECKOUT);
    expect(menu().map((b) => b.textContent)).toEqual(["Rename", "Delete"]);
    await act(async () => menu()[0].click());
    expect(rename).toHaveBeenCalled();
    expect(document.querySelector(".ph-preview")).toBeNull();
  });

  it("opens the same sheet on a long press and swallows the click that follows the release", async () => {
    vi.useFakeTimers();
    const onOpen = vi.fn();
    await act(async () => root.render(<ListRow title={DIGEST} onOpen={onOpen} actions={[{ label: "Archive", onSelect: vi.fn() }]} />));
    const row = host.querySelector<HTMLElement>(".ph-row")!;
    await act(async () => { row.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerType: "touch", clientX: 10, clientY: 10 })); });
    await act(async () => { vi.advanceTimersByTime(500); });
    expect(menu().map((b) => b.textContent)).toEqual(["Archive"]);
    await act(async () => { row.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, pointerType: "touch" })); row.click(); });
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("answers a right click and the keyboard's menu key with the sheet", async () => {
    await act(async () => root.render(<ListRow title="translator" onOpen={vi.fn()} actions={[{ label: "Move…", onSelect: vi.fn() }]} />));
    const row = host.querySelector<HTMLElement>(".ph-row")!;
    await act(async () => { row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })); });
    expect(menu()).toHaveLength(1);
  });

  it("draws no ⋮ for a row without commands", async () => {
    await act(async () => root.render(<ListRow title="Plain" onOpen={vi.fn()} />));
    expect(host.querySelector(".ph-row-more")).toBeNull();
  });
});

describe("ActionSheet", () => {
  it("moves the destructive commands last, after a divider, whatever the menu's order", async () => {
    await act(async () => root.render(<ActionSheet onClose={vi.fn()} items={[{ label: "Delete", danger: true, onSelect: vi.fn() }, "-", { label: "Rename", onSelect: vi.fn() }]} />));
    expect(menu().map((b) => b.textContent)).toEqual(["Rename", "Delete"]);
    expect(document.querySelector(".ph-msep")).not.toBeNull();
  });
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
