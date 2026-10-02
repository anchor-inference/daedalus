// @vitest-environment jsdom
// The pop-up stack as the reader meets it, on a fake clock: toasts leave by themselves, a pointer on
// one holds it and only it, closing the last one under the pointer does not freeze the next, the
// stack shows three and a count, "Close all" empties it, and the device switch keeps it away.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppEvent, Notification } from "./api";
import { ToastHost } from "./ui/dialogs";
import { popupsShown, setPopupsShown } from "./popups";
import { GLANCE_MS, NotificationToasts, TOAST_MS } from "./toasts";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

type Handler = (event: AppEvent, meta: { replayed: boolean }) => void;
const handlers: Map<string, Set<Handler>> = vi.hoisted(() => new Map());

vi.mock("./events", async (original) => {
  const { useEffect, useRef } = await import("react");
  return {
    ...(await original<typeof import("./events")>()),
    useEvent(types: readonly string[], handler: Handler) {
      const latest = useRef(handler);
      latest.current = handler;
      const key = types.join(",");
      useEffect(() => {
        const wrapped: Handler = (event, meta) => latest.current(event, meta);
        for (const type of key.split(",")) {
          if (!handlers.has(type)) handlers.set(type, new Set());
          handlers.get(type)!.add(wrapped);
        }
        return () => {
          for (const type of key.split(",")) handlers.get(type)?.delete(wrapped);
        };
      }, [key]);
    },
  };
});

vi.mock("./presence", () => ({ shownScopes: () => ({ sessions: new Set(), terminals: new Set(), projects: new Set() }) }));

function entry(id: number, over: Partial<Notification> = {}): Notification {
  return {
    id, at: "2026-09-24T10:00:00Z", updated_at: "2026-09-24T10:00:00Z", category: "run_finished", kind: "run", level: "normal", tone: "ok",
    title: `Entry ${id}`, body: "", link: "", session_id: "s-elsewhere", run_id: null, project_id: null, staff_id: null, terminal_id: null, source: "run",
    dedupe_key: null, request_ref: null, count: 1, actions: [], seen: false, resolved: null, needs_you: false, delivered: {}, ...over,
  };
}

let seq = 0;
function arrive(...ids: number[]) {
  act(() => {
    for (const id of ids) {
      seq += 1;
      const event = { seq, at: "", type: "notify", project_id: null, session_id: null, staff_id: null, terminal_id: null, payload: { notification: entry(id), toast: true } } as unknown as AppEvent;
      for (const h of handlers.get("notify") ?? []) h(event, { replayed: false });
    }
  });
}

function shown(): string[] {
  return [...document.querySelectorAll<HTMLElement>(".notice-toast")].map((el) => el.dataset.notice ?? "");
}

function advance(ms: number) {
  act(() => {
    vi.advanceTimersByTime(ms);
  });
}

function pointer(el: Element, type: "pointerover" | "pointermove" | "pointerout", { pointerType = "mouse", moved = 4 } = {}) {
  act(() => {
    const event = new PointerEvent(type, { bubbles: true, pointerType, relatedTarget: type === "pointerout" ? document.body : null });
    Object.defineProperty(event, "movementX", { value: moved });
    Object.defineProperty(event, "movementY", { value: 0 });
    el.dispatchEvent(event);
  });
}

function click(el: Element | null) {
  act(() => {
    (el as HTMLElement).click();
  });
}

describe("the pop-up stack", () => {
  let root: Root;
  let host: HTMLDivElement;

  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    setPopupsShown(true);
    Object.defineProperty(document, "visibilityState", { value: "visible", configurable: true });
    // A desktop: the stack of three in the corner.
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: query.includes("min-width"), media: query, addEventListener() {}, removeEventListener() {} }));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    act(() => root.render(<><NotificationToasts /><ToastHost /></>));
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("lets a toast go by itself after its time", () => {
    arrive(1);
    expect(shown()).toEqual(["1"]);
    advance(TOAST_MS - 100);
    expect(shown()).toEqual(["1"]);
    advance(200);
    expect(shown()).toEqual([]);
  });

  it("holds the toast under a moving mouse, and only that one, until the mouse leaves", () => {
    arrive(1, 2);
    const first = document.querySelector("[data-notice='1']")!;
    pointer(first, "pointerover");
    pointer(first, "pointermove");
    advance(TOAST_MS * 3);
    expect(shown()).toEqual(["1"]);
    pointer(first, "pointerout");
    advance(TOAST_MS - 100);
    expect(shown()).toEqual(["1"]);
    advance(200);
    expect(shown()).toEqual([]);
  });

  it("does not hold a toast that appeared under a mouse lying still in the corner", () => {
    arrive(1);
    const toast = document.querySelector("[data-notice='1']")!;
    pointer(toast, "pointerover");
    pointer(toast, "pointermove", { moved: 0 });
    advance(TOAST_MS + 100);
    expect(shown()).toEqual([]);
  });

  it("does not hold a toast a finger tapped: a touch leaves no hover to end it", () => {
    arrive(1);
    pointer(document.querySelector("[data-notice='1']")!, "pointermove", { pointerType: "touch" });
    advance(TOAST_MS + 100);
    expect(shown()).toEqual([]);
  });

  it("does not freeze the next toasts when the last one is closed under the pointer", () => {
    arrive(1);
    const toast = document.querySelector("[data-notice='1']")!;
    pointer(toast, "pointermove");
    click(toast.querySelector(".notice-toast-head button"));
    expect(shown()).toEqual([]);
    arrive(2);
    advance(TOAST_MS + 100);
    expect(shown()).toEqual([]);
  });

  it("shows three, newest on top, and counts the rest in a line that opens the inbox", () => {
    arrive(1, 2, 3, 4, 5);
    expect(shown()).toEqual(["3", "2", "1"]);
    const more = document.querySelector<HTMLButtonElement>(".notice-more")!;
    expect(more.textContent).toBe("+2 more");
    click(more);
    expect(shown()).toEqual([]);
    expect(window.location.pathname).toBe("/app/inbox");
  });

  it("offers Close all only with two or more, and it closes every one", () => {
    arrive(1);
    expect(document.querySelector("[data-close-all]")).toBeNull();
    arrive(2, 3, 4);
    click(document.querySelector("[data-close-all]"));
    expect(shown()).toEqual([]);
    expect(document.querySelector(".notice-toasts")).toBeNull();
    // The waiting one went with them rather than taking the free place.
    advance(GLANCE_MS);
    expect(shown()).toEqual([]);
  });

  it("raises nothing on a device where pop-ups are off", () => {
    act(() => setPopupsShown(false));
    arrive(1, 2);
    expect(shown()).toEqual([]);
    act(() => setPopupsShown(true));
    arrive(3);
    expect(shown()).toEqual(["3"]);
  });

  it("switches pop-ups off from the stack itself, and Undo brings them back", () => {
    arrive(1, 2);
    click(document.querySelector("[data-popups-off]"));
    expect(shown()).toEqual([]);
    expect(popupsShown()).toBe(false);
    expect(localStorage.getItem("daedalus.notice.popups")).toBe("off");
    const status = document.querySelector(".toast")!;
    expect(status.textContent).toContain("Settings");
    click(status.querySelector("button"));
    expect(popupsShown()).toBe(true);
    arrive(3);
    expect(shown()).toEqual(["3"]);
  });

  it("closes what is up when pop-ups are switched off elsewhere", () => {
    arrive(1, 2);
    act(() => setPopupsShown(false));
    expect(shown()).toEqual([]);
  });
});

describe("the device switch", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    setPopupsShown(true);
  });

  it("still works for the open page when storage refuses", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    setPopupsShown(false);
    expect(popupsShown()).toBe(false);
    setPopupsShown(true);
    expect(popupsShown()).toBe(true);
  });
});
