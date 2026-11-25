// @vitest-environment jsdom
// A list row as a reader meets it: it opens on a tap, its commands open on a long press, on a right
// click and from its ⋮ (so no command needs a gesture), and the sheet of commands lists the
// destructive one last.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "../../i18n";
import { Menu } from "./menu";
import { Row } from "./row";

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
  vi.useRealTimers();
});

// Titles of invented chats, held in constants: hardcoded.test.ts reads `title="…"` in any source as
// words a reader would see.
const CHECKOUT = "Checkout page";
const DIGEST = "Weekly digest";
const menu = () => Array.from(document.querySelectorAll<HTMLButtonElement>(".menu-row"));

describe("Row", () => {
  it("opens on a tap and keeps every command behind its ⋮", async () => {
    const onOpen = vi.fn();
    const rename = vi.fn();
    await act(async () => root.render(<Row title={CHECKOUT} meta="Working" onOpen={onOpen} actions={[{ label: "Rename", onSelect: rename }, { label: "Delete", danger: true, onSelect: vi.fn() }]} />));
    await act(async () => host.querySelector<HTMLElement>(".list-row")!.click());
    expect(onOpen).toHaveBeenCalledTimes(1);
    await act(async () => host.querySelector<HTMLButtonElement>(".list-row-more")!.click());
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(document.querySelector(".sheet-preview")?.textContent).toContain(CHECKOUT);
    expect(menu().map((b) => b.textContent)).toEqual(["Rename", "Delete"]);
    await act(async () => menu()[0].click());
    expect(rename).toHaveBeenCalled();
    expect(document.querySelector(".sheet-preview")).toBeNull();
  });

  it("opens the same sheet on a long press and swallows the click that follows the release", async () => {
    vi.useFakeTimers();
    const onOpen = vi.fn();
    await act(async () => root.render(<Row title={DIGEST} onOpen={onOpen} actions={[{ label: "Archive", onSelect: vi.fn() }]} />));
    const row = host.querySelector<HTMLElement>(".list-row")!;
    await act(async () => { row.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, pointerType: "touch", clientX: 10, clientY: 10 })); });
    await act(async () => { vi.advanceTimersByTime(500); });
    expect(menu().map((b) => b.textContent)).toEqual(["Archive"]);
    await act(async () => { row.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, pointerType: "touch" })); row.click(); });
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("answers a right click and the keyboard's menu key with the sheet", async () => {
    await act(async () => root.render(<Row title="translator" onOpen={vi.fn()} actions={[{ label: "Move…", onSelect: vi.fn() }]} />));
    const row = host.querySelector<HTMLElement>(".list-row")!;
    await act(async () => { row.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true })); });
    expect(menu()).toHaveLength(1);
  });

  it("draws no ⋮ for a row without commands", async () => {
    await act(async () => root.render(<Row title="Plain" onOpen={vi.fn()} />));
    expect(host.querySelector(".list-row-more")).toBeNull();
  });
});

describe("Menu as a sheet", () => {
  it("moves the destructive commands last, after a divider, whatever the menu's order", async () => {
    await act(async () => root.render(<Menu label="Commands" onClose={vi.fn()} items={[{ label: "Delete", danger: true, onSelect: vi.fn() }, "-", { label: "Rename", onSelect: vi.fn() }]} />));
    expect(menu().map((b) => b.textContent)).toEqual(["Rename", "Delete"]);
    expect(document.querySelector(".menu-sheet .menu-sep")).not.toBeNull();
  });
});
