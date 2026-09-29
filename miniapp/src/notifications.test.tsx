// @vitest-environment jsdom
// The centre's grouping and its answers: by day and by project, and a second answer told what the
// first one was rather than shown an error.

import { act as reactAct } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Notification } from "./api";
import { ActionButtons, act, byDay, byProject, entryPath, noticeLine } from "./notifications";
import { setLang } from "./i18n";
import { prime } from "./store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function entry(id: number, over: Partial<Notification> = {}): Notification {
  return {
    id, at: "2026-09-24T10:00:00Z", updated_at: "2026-09-24T10:00:00Z", category: "system", kind: "service", level: "normal", tone: "info",
    title: `Entry ${id}`, body: "", link: "", session_id: null, run_id: null, project_id: null, staff_id: null, terminal_id: null, source: "system",
    dedupe_key: null, request_ref: null, count: 1, actions: [], seen: false, resolved: null, needs_you: false, delivered: {}, ...over,
  };
}

beforeEach(() => setLang("en"));
afterEach(() => vi.restoreAllMocks());

describe("grouping", () => {
  it("puts the reader's today first and everything before it under Earlier", () => {
    const now = new Date(2026, 8, 24, 15, 0);
    const today = new Date(2026, 8, 24, 0, 5).toISOString();
    const yesterday = new Date(2026, 8, 23, 23, 55).toISOString();
    const groups = byDay([entry(3, { updated_at: today }), entry(2, { updated_at: yesterday }), entry(1, { updated_at: "2026-09-01T10:00:00Z" })], now);
    expect(groups.map((g) => [g.label, g.entries.map((e) => e.id)])).toEqual([["Today", [3]], ["Earlier", [2, 1]]]);
    expect(byDay([], now)).toEqual([]);
  });

  it("groups by project name, the newest project first and the unplaced last", () => {
    const names = new Map([["p1", "Bakery"], ["p2", "Expenses"]]);
    const groups = byProject([entry(5, { project_id: "p2" }), entry(4), entry(3, { project_id: "p1" }), entry(2, { project_id: "p2" }), entry(1, { project_id: "gone" })], names);
    expect(groups.map((g) => [g.label, g.entries.map((e) => e.id)])).toEqual([["Expenses", [5, 2]], ["Bakery", [3]], ["Outside projects", [4, 1]]]);
  });
});

describe("where an entry leads", () => {
  it("follows its link, then its session, then the centre", () => {
    expect(entryPath({ link: "/app/board/t1", session_id: "s1" })).toBe("/app/board/t1");
    expect(entryPath({ link: "", session_id: "s1" })).toBe("/app/agents/s1");
    expect(entryPath({ link: "https://example.com", session_id: null })).toBe("/app/inbox");
    // A project's or the main chat's link, as the host writes it, opens in orchestration mode.
    expect(entryPath({ link: "/app/project/p1/board?task=t1", session_id: null })).toBe("/app/orchestration/project/p1/board?task=t1");
    expect(entryPath({ link: "/app/main", session_id: null })).toBe("/app/orchestration");
  });
});

function answer(status: number, body: unknown) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

describe("answering", () => {
  it("posts the action and returns the resolution", async () => {
    const fetch = answer(200, { resolution: "allow", notification: null });
    expect(await act(7, "allow")).toEqual({ resolution: "allow", notification: null, conflict: false });
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/notifications/7/act");
    expect(JSON.parse(String(init?.body))).toEqual({ action: "allow" });
  });

  it("reads a 409 as the answer somebody gave first", async () => {
    answer(409, { resolution: "deny", notification: entry(7, { resolved: "deny" }) });
    const r = await act(7, "allow");
    expect(r.conflict).toBe(true);
    expect(r.resolution).toBe("deny");
  });

  it("throws on anything else", async () => {
    answer(400, { detail: "this notification offers no 'x'" });
    await expect(act(7, "x")).rejects.toThrow("offers no");
  });
});

describe("the buttons", () => {
  let root: Root;
  let host: HTMLDivElement;
  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    reactAct(() => root.unmount());
    host.remove();
  });

  const request = entry(9, { category: "permission", needs_you: true, request_ref: "policy:s:k", actions: [{ id: "allow", label: "Allow", style: "primary", quick: true }, { id: "deny", label: "Deny", style: "default", quick: true }] });

  it("give way to the real outcome when the request was answered elsewhere", async () => {
    answer(409, { resolution: "deny", notification: null });
    const done = vi.fn();
    await reactAct(async () => root.render(<ActionButtons entry={request} onDone={done} />));
    const allow = host.querySelector<HTMLButtonElement>('[data-action="allow"]')!;
    await reactAct(async () => allow.click());
    expect(host.querySelector(".notice-outcome")?.textContent).toBe("Denied");
    expect(host.querySelector("button")).toBeNull();
    expect(done).toHaveBeenCalledWith(expect.objectContaining({ conflict: true, resolution: "deny" }));
  });

  it("show what an answered entry was answered with, and nothing for an entry with nothing to answer", async () => {
    await reactAct(async () => root.render(<ActionButtons entry={{ ...request, needs_you: false, resolved: "allow" }} />));
    expect(host.textContent).toBe("Allowed");
    await reactAct(async () => root.render(<ActionButtons entry={entry(1)} />));
    expect(host.innerHTML).toBe("");
  });
});

describe("the line under a notification", () => {
  it("names the project once when the session's title already carries it", () => {
    prime("/api/sessions", { sessions: [{ id: "o1", title: "Orchestrator · Anchor Inference" }, { id: "s1", title: "Anchor Inference" }] });
    const names = new Map([["p1", "Anchor Inference"]]);
    expect(noticeLine(entry(1, { category: "system", project_id: "p1", session_id: "o1" }), names)).toBe("System · Anchor Inference · Orchestrator");
    expect(noticeLine(entry(2, { category: "system", project_id: "p1", session_id: "s1" }), names)).toBe("System · Anchor Inference");
    expect(noticeLine(entry(3, { category: "spend" }), names)).toBe("Spending · the whole app");
  });
});
