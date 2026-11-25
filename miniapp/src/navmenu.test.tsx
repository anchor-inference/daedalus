// @vitest-environment jsdom
// The More launcher's body: every destination under its group with a line saying what it is, the pin
// that puts it in the sidebar, the search that narrows the list, and the way on to the palette.

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { type PageInfo, resolveNav } from "./navconfig";
import { Launcher } from "./navmenu";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PAGE: PageInfo = { slug: "dosing", title: "Dosing calculator", icon: null, icon_path: null, path: "/app/p/dosing" };

let host: HTMLDivElement;
let root: Root;
const place = vi.fn(async () => true);
const everywhere = vi.fn();
const go = vi.fn();

beforeEach(() => {
  setLang("en");
  localStorage.clear();
  place.mockClear();
  everywhere.mockClear();
  go.mockClear();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

/** A write the host has not answered yet, and the way to answer it. */
function pending() {
  let finish: (took: boolean) => void = () => {};
  const promise = new Promise<boolean>((resolve) => { finish = resolve; });
  return { promise, finish };
}

async function open(phone = false) {
  const nav = resolveNav({ config: null, pages: [PAGE], selfdev: "server" });
  await act(async () => root.render(
    <Launcher nav={nav} placed={phone ? nav.drawer.agents : nav.pinned} current="board" selfdev="server" onGo={go} onPlace={place} onEverywhere={everywhere} onClose={() => {}} phone={phone} />,
  ));
}
const type = (text: string) => act(async () => {
  const field = host.querySelector<HTMLInputElement>(".launch-field")!;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, text);
  field.dispatchEvent(new Event("input", { bubbles: true }));
});
const items = () => [...host.querySelectorAll<HTMLElement>(".launch-grid .launch-row")].map((el) => el.dataset.navItem);

describe("the groups", () => {
  it("lists every destination under its group, in the menu's order, the page last", async () => {
    await open();
    expect([...host.querySelectorAll(".launch-sec[data-group] .launch-label")].map((el) => el.textContent)).toEqual(["Work", "Autonomy", "Knowledge", "Observe", "Pages"]);
    expect(items()).toEqual(["companion", "inbox", "board", "calendar", "canvas", "terminals", "harnesses", "changes", "schedules", "services", "memory", "usage", "health", "page:dosing"]);
  });

  it("says what each one is under its name", async () => {
    await open();
    const board = host.querySelector("[data-nav-item='board']")!;
    expect(board.querySelector(".launch-name")?.textContent).toBe("Board");
    expect(board.querySelector(".launch-desc")?.textContent).toBe("Tasks and their statuses");
    expect(host.querySelectorAll(".launch-desc")).toHaveLength(14);
  });

  it("offers four chips from what the sidebar does not show, and marks the current screen", async () => {
    await open();
    expect([...host.querySelectorAll<HTMLElement>(".launch-chip")].map((el) => el.dataset.chip)).toEqual(["companion", "canvas", "harnesses", "schedules"]);
    expect(host.querySelector("[data-nav-item='board']")?.getAttribute("aria-current")).toBe("page");
  });
});

describe("the pin", () => {
  it("shows what is pinned in the sidebar as pressed, and asks to pin the rest", async () => {
    await open();
    const pin = (id: string) => host.querySelector(`[data-nav-item='${id}']`)!.parentElement!.querySelector<HTMLButtonElement>(".launch-pin")!;
    expect(pin("inbox").getAttribute("aria-pressed")).toBe("true");
    expect(pin("usage").getAttribute("aria-pressed")).toBe("false");
    expect(pin("usage").getAttribute("aria-label")).toBe("Pin to the sidebar: Usage");
    await act(async () => pin("usage").click());
    expect(place).toHaveBeenCalledWith("usage", true);
    await act(async () => pin("inbox").click());
    expect(place).toHaveBeenCalledWith("inbox", false);
  });

  it("shows the press at once, before the host has answered", async () => {
    const gate = pending();
    place.mockImplementationOnce(() => gate.promise);
    await open();
    const pin = host.querySelector("[data-nav-item='usage']")!.parentElement!.querySelector<HTMLButtonElement>(".launch-pin")!;
    await act(async () => pin.click());
    expect(pin.getAttribute("aria-pressed")).toBe("true");
    await act(async () => gate.finish(false));
    expect(pin.getAttribute("aria-pressed")).toBe("false");
  });

  it("is the drawer's list on a phone, and says so", async () => {
    await open(true);
    const pin = host.querySelector("[data-nav-item='memory']")!.parentElement!.querySelector<HTMLButtonElement>(".launch-pin")!;
    expect(pin.getAttribute("aria-label")).toBe("Add to the menu: Memory");
    expect(host.querySelector(".launch-foot [data-nav='about']")).not.toBeNull();
  });
});

describe("the search", () => {
  it("narrows the destinations to those whose name or line holds the words", async () => {
    await open();
    await type("quotas");
    expect(items()).toEqual(["usage"]);
    expect(host.querySelector(".launch-frequent")).toBeNull();
    await type("page");
    expect(items()).toContain("page:dosing");
  });

  it("hands words that match nothing to the palette", async () => {
    await open();
    await type("zzqqxx");
    expect(items()).toEqual([]);
    const row = host.querySelector<HTMLButtonElement>(".launch-everywhere")!;
    expect(row.textContent).toContain("zzqqxx");
    await act(async () => row.click());
    expect(everywhere).toHaveBeenCalledWith("zzqqxx");
  });
});
