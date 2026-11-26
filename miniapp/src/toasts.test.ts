// @vitest-environment jsdom
// The toast rules as rules: how many are shown, in which order, for how long, and which events never
// become a toast at all.

import { describe, expect, it } from "vitest";
import type { Notification } from "./api";
import { GLANCE_MS, NoticeQueue, STACK_MAX } from "./ui/primitives";
import { ACTIONABLE_MS, TOAST_MS, URGENT_MS, shouldToast, toastDuration, type ToastContext } from "./toasts";

function entry(id: number, over: Partial<Notification> = {}): Notification {
  return {
    id, at: "2026-09-24T10:00:00Z", updated_at: "2026-09-24T10:00:00Z", category: "run_finished", kind: "run", level: "normal", tone: "ok",
    title: `Entry ${id}`, body: "", link: "", session_id: null, run_id: null, project_id: null, staff_id: null, terminal_id: null, source: "run",
    dedupe_key: null, request_ref: null, count: 1, actions: [], seen: false, resolved: null, needs_you: false, delivered: {}, ...over,
  };
}

const permission = (id: number) => entry(id, { category: "permission", level: "urgent", needs_you: true, request_ref: `policy:s:${id}`, actions: [{ id: "allow", label: "Allow", style: "primary", quick: true }, { id: "deny", label: "Deny", style: "default", quick: true }, { id: "open", label: "Open", style: "ghost", quick: false }] });

function clock(start = 1_000_000) {
  const c = { now: start, advance: (ms: number) => { c.now += ms; } };
  return c;
}

type Queue = NoticeQueue<Notification>;
/** An entry's pop-up as the bridge raises it: keyed by the entry, kept as long as its kind is. */
const put = (q: Queue, e: Notification, seq: number) => q.push(e.id, e, seq, toastDuration(e));

describe("the queue", () => {
  it("shows at most three, newest on top, and keeps the rest in line", () => {
    const c = clock();
    const q = new NoticeQueue<Notification>(STACK_MAX, () => c.now);
    for (let i = 1; i <= 5; i++) put(q, entry(i), i);
    expect(q.visible().map((i) => i.data.id)).toEqual([3, 2, 1]);
    expect(q.waiting.map((i) => i.data.id)).toEqual([4, 5]);
  });

  it("lets each one go after its time, and the ones that waited with them", () => {
    const c = clock();
    const q = new NoticeQueue<Notification>(STACK_MAX, () => c.now);
    for (let i = 1; i <= 5; i++) put(q, entry(i), i);
    c.advance(TOAST_MS - 1);
    q.tick();
    expect(q.visible()).toHaveLength(3);
    expect(q.waiting).toHaveLength(2);
    c.advance(1);
    // A burst is one glance: what waited is in the inbox, not in a procession after the first three.
    expect(q.tick()).toBeNull();
    expect(q.count).toBe(0);
  });

  it("shows one that waited for room only while it has a glance left", () => {
    const c = clock();
    const q = new NoticeQueue<Notification>(STACK_MAX, () => c.now);
    for (let i = 1; i <= 3; i++) put(q, entry(i), i);
    c.advance(TOAST_MS - GLANCE_MS - 1000);
    put(q, entry(4), 4);
    c.advance(GLANCE_MS + 1000);
    q.tick();
    expect(q.visible().map((i) => i.data.id)).toEqual([4]);
    expect(q.shown[0].expires).toBe(c.now + TOAST_MS - GLANCE_MS - 1000);
    put(q, entry(5), 5);
    put(q, entry(6), 6);
    put(q, entry(7), 7);
    expect(q.visible().map((i) => i.data.id)).toEqual([6, 5, 4]);
    c.advance(TOAST_MS - GLANCE_MS - 1000);
    q.tick();
    // 4 left with more than a glance of 7's time to go, so 7 takes its place.
    expect(q.visible().map((i) => i.data.id)).toEqual([7, 6, 5]);
    put(q, entry(8), 8);
    c.advance(GLANCE_MS + 1000);
    q.tick();
    // 5, 6 and 7 leave together; 8 has exactly a glance left and gets it.
    expect(q.visible().map((i) => i.data.id)).toEqual([8]);
  });

  it("only counts one whose time runs out before it could be read", () => {
    const c = clock();
    const q = new NoticeQueue<Notification>(1, () => c.now);
    put(q, entry(1), 1);
    c.advance(1);
    put(q, entry(2), 2);
    c.advance(TOAST_MS - 1);
    q.tick();
    expect(q.visible()).toEqual([]);
    expect(q.count).toBe(0);
  });

  it("gives a request to answer longer than a line to read, and an urgent one in between", () => {
    const c = clock();
    const q = new NoticeQueue<Notification>(STACK_MAX, () => c.now);
    put(q, permission(1), 1);
    put(q, entry(2), 2);
    put(q, entry(3, { tone: "error", category: "run_failed" }), 3);
    expect(TOAST_MS).toBeLessThan(URGENT_MS);
    expect(URGENT_MS).toBeLessThan(ACTIONABLE_MS);
    c.advance(TOAST_MS);
    q.tick();
    expect(q.visible().map((i) => i.data.id)).toEqual([3, 1]);
    c.advance(URGENT_MS - TOAST_MS);
    q.tick();
    expect(q.visible().map((i) => i.data.id)).toEqual([1]);
    c.advance(ACTIONABLE_MS - URGENT_MS);
    q.tick();
    expect(q.visible()).toEqual([]);
  });

  it("stops the clock only of the toast that is held", () => {
    const c = clock();
    const q = new NoticeQueue<Notification>(STACK_MAX, () => c.now);
    put(q, entry(1), 1);
    put(q, entry(2), 2);
    c.advance(TOAST_MS - 1000);
    q.hold(1);
    c.advance(60_000);
    q.tick();
    expect(q.visible().map((i) => i.data.id)).toEqual([1]);
    expect(q.next()).toBeNull();
    q.release(1);
    c.advance(999);
    q.tick();
    expect(q.visible()).toHaveLength(1);
    c.advance(1);
    q.tick();
    expect(q.visible()).toEqual([]);
  });

  it("forgets a hold when its toast is gone, so nothing after it stays up forever", () => {
    const c = clock();
    const q = new NoticeQueue<Notification>(STACK_MAX, () => c.now);
    put(q, entry(1), 1);
    q.hold(1);
    // Closed under the pointer: no leave follows.
    q.dismiss(1);
    put(q, entry(2), 2);
    c.advance(TOAST_MS);
    q.tick();
    expect(q.count).toBe(0);
  });

  it("closes all of them at once, the waiting ones too", () => {
    const q = new NoticeQueue<Notification>(STACK_MAX, () => 0);
    for (let i = 1; i <= 5; i++) put(q, entry(i), i);
    expect(q.clear()).toBe(5);
    expect(q.count).toBe(0);
    expect(q.next()).toBeNull();
  });

  it("replaces a repeat of an entry already shown instead of stacking it", () => {
    const c = clock();
    const q = new NoticeQueue<Notification>(STACK_MAX, () => c.now);
    put(q, entry(1), 1);
    c.advance(5000);
    put(q, entry(1, { title: "Again", count: 2 }), 9);
    expect(q.visible()).toHaveLength(1);
    expect(q.visible()[0].data.title).toBe("Again");
    c.advance(5000);
    q.tick();
    expect(q.visible()).toHaveLength(1);
  });

  it("dismisses one and lets the next in", () => {
    const q = new NoticeQueue<Notification>(STACK_MAX, () => 0);
    for (let i = 1; i <= 4; i++) put(q, entry(i), i);
    expect(q.dismiss(2)).toBe(true);
    expect(q.visible().map((i) => i.data.id)).toEqual([4, 3, 1]);
    expect(q.dismiss(42)).toBe(false);
  });

  it("keeps one on a phone and puts the others back in line", () => {
    const q = new NoticeQueue<Notification>(STACK_MAX, () => 0);
    for (let i = 1; i <= 3; i++) put(q, entry(i), i);
    q.setMax(1);
    expect(q.visible().map((i) => i.data.id)).toEqual([3]);
    expect(q.waiting.map((i) => i.data.id)).toEqual([2, 1]);
  });
});

describe("which events become a toast", () => {
  const here: ToastContext = { visible: true, shown: { sessions: new Set(["s-open"]), terminals: new Set(["t-open"]), projects: new Set(["p-open"]) } };
  const live = { replayed: false };
  const payload = (n: Notification, toast = true) => ({ notification: n, toast, deliver: {}, merged: false, summary: { unseen: 1, needs_you: 0 } });

  it("raises a live one the host marked", () => {
    expect(shouldToast(payload(entry(1, { session_id: "s-other" })), live, here)).toBe(true);
  });

  it("never raises a replay, an unmarked one, a quiet one or an answered one", () => {
    expect(shouldToast(payload(entry(1)), { replayed: true }, here)).toBe(false);
    expect(shouldToast(payload(entry(1), false), live, here)).toBe(false);
    expect(shouldToast(payload(entry(1, { level: "quiet" })), live, here)).toBe(false);
    expect(shouldToast(payload(entry(1, { resolved: "allow" })), live, here)).toBe(false);
  });

  it("stays quiet in a hidden page", () => {
    expect(shouldToast(payload(entry(1)), live, { ...here, visible: false })).toBe(false);
  });

  it("says nothing about what the window already shows", () => {
    expect(shouldToast(payload(entry(1, { session_id: "s-open" })), live, here)).toBe(false);
    expect(shouldToast(payload(entry(1, { terminal_id: "t-open" })), live, here)).toBe(false);
    expect(shouldToast(payload(entry(1, { project_id: "p-open" })), live, here)).toBe(false);
    // A session of that project is not the project's page.
    expect(shouldToast(payload(entry(1, { project_id: "p-open", session_id: "s-other" })), live, here)).toBe(true);
  });
});
