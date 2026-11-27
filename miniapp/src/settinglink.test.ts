// @vitest-environment jsdom
// The way from an error to its setting: the reference the host sends, the address it becomes, the row
// found once the page has drawn it, scrolled to and made to blink, and the notice that offers it —
// once, however many hands the error passes through.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, type Notification } from "./api";
import {
  dismissSettingNotice, FLASH_CLASS, NOTICE_EVENT, noticeFromEvent, raiseFromError, raiseSettingNotice, keepInView, revealSetting, rowSelector,
  settingNotices, settingOfError, settingPath, settingRef, type SettingNotice,
} from "./settinglink";
import { NoticeQueue } from "./ui/primitives";
import { localEntry, settingGroup, toastDuration, URGENT_MS } from "./toasts";

const SUMMARY = { page: "context", key: "compaction.preset" };

describe("the reference", () => {
  it("is read only when it is a page and a key", () => {
    expect(settingRef(SUMMARY)).toEqual(SUMMARY);
    expect(settingRef({ page: "limits" })).toBeNull();
    expect(settingRef("limits")).toBeNull();
    expect(settingRef(null)).toBeNull();
  });

  it("comes off an error the host refused with, and off nothing else", () => {
    expect(settingOfError(new ApiError(409, "the summary model is over its limit", { detail: "x", setting: SUMMARY }))).toEqual(SUMMARY);
    expect(settingOfError(new ApiError(409, "busy"))).toBeNull();
    expect(settingOfError(new Error("plain"))).toBeNull();
  });

  it("becomes the section's address naming the row", () => {
    expect(settingPath(SUMMARY)).toBe("/app/settings/context#compaction.preset");
    expect(settingPath({ page: "models", key: "providers.opencode.api_key" })).toBe("/app/settings/models#providers.opencode.api_key");
    expect(settingPath({ page: "tasks", key: "vision.preset" })).toBe("/app/settings/tasks#vision.preset");
  });

  it("quotes a key in its selector, so a provider id cannot break out of it", () => {
    expect(rowSelector('providers.a"b.api_key')).toBe('[data-setting="providers.a\\"b.api_key"]');
  });
});

describe("revealSetting", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
  });
  afterEach(() => vi.useRealTimers());

  function row(key: string): HTMLElement {
    const el = document.createElement("div");
    el.dataset.setting = key;
    el.scrollIntoView = vi.fn();
    document.body.appendChild(el);
    return el;
  }

  it("scrolls the row into the middle and makes it blink, then lets it rest", async () => {
    const el = row("compaction.preset");
    const found = revealSetting("compaction.preset", { flashMs: 1000 });
    await expect(found).resolves.toBe(true);
    expect(el.scrollIntoView).toHaveBeenCalledWith(expect.objectContaining({ block: "center" }));
    expect(el.classList.contains(FLASH_CLASS)).toBe(true);
    vi.advanceTimersByTime(1000);
    expect(el.classList.contains(FLASH_CLASS)).toBe(false);
  });

  it("waits for a row the page draws after its settings arrive", async () => {
    const found = revealSetting("vision.preset", { every: 100, tries: 10 });
    vi.advanceTimersByTime(300);
    const el = row("vision.preset");
    vi.advanceTimersByTime(100);
    await expect(found).resolves.toBe(true);
    expect(el.classList.contains(FLASH_CLASS)).toBe(true);
  });

  it("gives up and says so when the row never comes", async () => {
    const found = revealSetting("nowhere", { every: 50, tries: 4 });
    vi.advanceTimersByTime(400);
    await expect(found).resolves.toBe(false);
  });
});

describe("a revealed row held in view", () => {
  // jsdom has no layout: the column's height and the row's place are numbers the test moves, and the
  // observer is a stand-in whose callback the test calls when it says the column changed size.
  let notify: () => void;
  let column: HTMLElement;
  let el: HTMLElement;
  let layout: { above: number };

  beforeEach(() => {
    vi.useFakeTimers();
    document.body.innerHTML = "";
    layout = { above: 0 };
    vi.stubGlobal("ResizeObserver", class {
      private watching = false;
      constructor(private callback: ResizeObserverCallback) {
        notify = () => this.watching && this.callback([{ target: column } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver);
      }
      observe() {
        this.watching = true;
      }
      disconnect() {
        this.watching = false;
      }
      unobserve() {}
    });
    Object.defineProperty(window, "innerHeight", { value: 800, configurable: true });
    column = document.createElement("div");
    column.getBoundingClientRect = () => ({ height: 600 + layout.above, top: 0, bottom: 600 + layout.above } as DOMRect);
    document.body.appendChild(column);
    el = placed("vision.preset");
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function placed(key: string): HTMLElement {
    const row = document.createElement("div");
    row.dataset.setting = key;
    row.scrollIntoView = vi.fn();
    row.getBoundingClientRect = () => ({ top: 300 + layout.above, bottom: 360 + layout.above, height: 60 } as DOMRect);
    column.appendChild(row);
    return row;
  }

  const land = (px: number) => {
    layout.above = px;
    notify();
  };

  it("puts the row back however late the content above it lands", async () => {
    await revealSetting("vision.preset");
    expect(el.scrollIntoView).toHaveBeenCalledTimes(1);
    // Far past the second or two a fixed grace allowed.
    vi.advanceTimersByTime(12_000);
    land(1200);
    expect(el.scrollIntoView).toHaveBeenCalledTimes(2);
    expect(el.scrollIntoView).toHaveBeenLastCalledWith({ block: "center" });
  });

  it("leaves a row that is still in view where it is", async () => {
    await revealSetting("vision.preset");
    land(200);
    expect(el.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it.each(["wheel", "touchstart", "pointerdown", "keydown"])("lets go the moment the reader's %s comes", async (type) => {
    await revealSetting("vision.preset");
    window.dispatchEvent(new Event(type));
    land(1200);
    expect(el.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("lets go after the bound", async () => {
    await revealSetting("vision.preset", { holdMs: 5000 });
    vi.advanceTimersByTime(5001);
    land(1200);
    expect(el.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("follows the row to a new element when its card draws afresh, and lets go when it is gone", async () => {
    await revealSetting("vision.preset");
    el.remove();
    const again = placed("vision.preset");
    land(1200);
    expect(again.scrollIntoView).toHaveBeenCalledWith({ block: "center" });
    again.remove();
    land(2400);
    expect(again.scrollIntoView).toHaveBeenCalledTimes(1);
    placed("vision.preset");
    land(3600);
    expect(el.scrollIntoView).toHaveBeenCalledTimes(1);
  });

  it("holds the top of a part of the page named by an old address the same way", () => {
    keepInView(el, { find: () => document.querySelector<HTMLElement>("[data-setting]"), block: "start" });
    land(1200);
    expect(el.scrollIntoView).toHaveBeenCalledWith({ block: "start" });
  });

  it("holds the row of the last link only", async () => {
    const other = placed("compaction.preset");
    await revealSetting("vision.preset");
    await revealSetting("compaction.preset");
    land(1200);
    expect(other.scrollIntoView).toHaveBeenCalledTimes(2);
    expect(el.scrollIntoView).toHaveBeenCalledTimes(1);
  });
});

describe("notices", () => {
  afterEach(() => {
    for (const n of [...settingNotices("s1"), ...settingNotices(null)]) dismissSettingNotice(n.id);
  });

  it("keeps one line per setting and tone in a conversation, and announces the toast", () => {
    const heard: SettingNotice[] = [];
    const listen = (e: Event) => heard.push((e as CustomEvent<SettingNotice>).detail);
    window.addEventListener(NOTICE_EVENT, listen);
    raiseSettingNotice({ tone: "warning", title: "first", body: "", setting: SUMMARY, sessionId: "s1" });
    raiseSettingNotice({ tone: "warning", title: "again", body: "", setting: SUMMARY, sessionId: "s1" });
    raiseSettingNotice({ tone: "error", title: "inline only", body: "", setting: SUMMARY, sessionId: "s1", toast: false });
    window.removeEventListener(NOTICE_EVENT, listen);
    expect(settingNotices("s1").map((n) => n.title)).toEqual(["again", "inline only"]);
    expect(heard.map((n) => n.title)).toEqual(["first", "again"]);
  });

  it("says an error once, however many hands it passes through", () => {
    const heard: SettingNotice[] = [];
    const listen = (e: Event) => heard.push((e as CustomEvent<SettingNotice>).detail);
    window.addEventListener(NOTICE_EVENT, listen);
    const error = new ApiError(409, "no model could summarise the history", { setting: SUMMARY });
    expect(raiseFromError(error, "s1")).toBe(true);
    expect(raiseFromError(error)).toBe(true);
    expect(raiseFromError(new ApiError(409, "busy"))).toBe(false);
    window.removeEventListener(NOTICE_EVENT, listen);
    expect(heard).toHaveLength(1);
    expect(heard[0]).toMatchObject({ tone: "error", body: "no model could summarise the history", sessionId: "s1", setting: SUMMARY });
  });

  it("reads a session's event as a warning when the session's model stood in", () => {
    const notice = noticeFromEvent("s1", { kind: "compaction", outcome: "fallback", detail: "cheap/flash: usage limit exceeded", model: "main/big", setting: SUMMARY });
    expect(notice).toMatchObject({ tone: "warning", sessionId: "s1", setting: SUMMARY });
    expect(notice?.body).toContain("main/big");
    expect(noticeFromEvent("s1", { kind: "vision", outcome: "failed", setting: { page: "tasks", key: "vision.preset" } })?.tone).toBe("error");
    expect(noticeFromEvent("s1", { kind: "vision", outcome: "failed" })).toBeNull();
  });
});

describe("the toast of a setting", () => {
  const notice = (id: number, tone: "warning" | "error"): SettingNotice => ({ id, tone, title: "t", body: "b", setting: SUMMARY, sessionId: null, toast: true });

  it("is drawn as a notification of its own, never one the host could mark seen", () => {
    const entry = localEntry(notice(7, "warning"));
    expect(entry.id).toBe(-7);
    expect(entry.setting).toEqual(SUMMARY);
    expect(entry.link).toBe("/app/settings/context#compaction.preset");
    expect(toastDuration(entry)).toBe(URGENT_MS);
  });

  it("replaces the host's toast about the same setting rather than stacking beside it", () => {
    const q = new NoticeQueue<Notification>();
    const put = (e: Notification, seq: number) => q.push(e.id, e, seq, toastDuration(e), settingGroup(e) ? (other) => settingGroup(other) === settingGroup(e) : undefined);
    const host: Notification = { ...localEntry(notice(1, "error")), id: 41, seen: false };
    put(host, 1);
    put(localEntry(notice(2, "error")), 2);
    expect(q.count).toBe(1);
    put(localEntry(notice(3, "warning")), 3);
    expect(q.count).toBe(2);
  });
});
