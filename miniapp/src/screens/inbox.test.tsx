// @vitest-environment jsdom
// The open entry of the Inbox: the table of what each button does only where the host says, a free
// answer only for a question, and an answer taken here reported back as the host gave it.

import { act as reactAct } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Notification } from "../api";
import { setLang, t } from "../i18n";
import { InboxDetail, type InboxItem } from "./InboxDetail";
import { INBOX_FILTERS, firstLine, tintOf, viewOf } from "./inboxdata";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function entry(id: number, over: Partial<Notification> = {}): Notification {
  return {
    id, at: "2026-09-24T10:00:00Z", updated_at: "2026-09-24T10:00:00Z", category: "system", kind: "service", level: "normal", tone: "info",
    title: `Entry ${id}`, body: "", link: "", session_id: null, run_id: null, project_id: null, staff_id: null, terminal_id: null, source: "system",
    dedupe_key: null, request_ref: null, count: 1, actions: [], seen: false, resolved: null, needs_you: false, delivered: {}, ...over,
  };
}

function itemOf(e: Notification): InboxItem {
  return { key: `n${e.id}`, entry: e, icon: "inbox", tint: tintOf(e), kind: "Question", title: e.title, preview: "", where: "", at: e.updated_at, unread: !e.seen };
}

const question = entry(5, {
  category: "question", needs_you: true, request_ref: "ask:s1:t1", body: "The digest has 14 items.\n\nKeep the usual 8?",
  actions: [
    { id: "answer:0", label: "Keep 8", style: "primary", quick: true, description: "Answers with this option." },
    { id: "answer:1", label: "All 14", style: "default", quick: true },
    { id: "open", label: "Open", style: "ghost", quick: false },
  ],
});

describe("the views", () => {
  it("are four, and unread is not one of them", () => {
    expect(INBOX_FILTERS.map((f) => f.id)).toEqual(["all", "needs", "problems", "done"]);
    expect(viewOf("needs")).toBe("needs_you");
    expect(viewOf("done")).toBe("done");
  });

  it("colour what waits or went wrong orange, finished work cyan and the rest grey", () => {
    expect(tintOf(question)).toBe("attention");
    expect(tintOf(entry(1, { tone: "warning" }))).toBe("attention");
    expect(tintOf(entry(2, { tone: "ok" }))).toBe("live");
    expect(tintOf(entry(3, { tone: "ok", level: "quiet" }))).toBe("quiet");
    expect(tintOf({ ...question, needs_you: false, resolved: "answered" })).toBe("quiet");
    expect(firstLine("\n  \nthe command\nmore")).toBe("the command");
  });
});

describe("the open entry", () => {
  let root: Root;
  let host: HTMLDivElement;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    reactAct(() => root.unmount());
    host.remove();
    vi.restoreAllMocks();
  });

  const draw = (e: Notification) =>
    reactAct(async () => root.render(
      <InboxDetail item={itemOf(e)} names={new Map()} toast={vi.fn()} onOpenChat={vi.fn()} onToggleRead={vi.fn()} onDelete={vi.fn()} onDecided={vi.fn()} />,
    ));

  it("draws a button for every answer but Open, and the table only for the buttons the host described", async () => {
    await draw(question);
    expect([...host.querySelectorAll(".inbox-actions button")].map((b) => b.textContent)).toEqual(["Keep 8", "All 14"]);
    expect([...host.querySelectorAll(".inbox-outcomes-row")].map((r) => r.textContent)).toEqual(["Keep 8Answers with this option."]);
    expect(host.querySelectorAll(".inbox-prose > div")).toHaveLength(2);
  });

  it("draws no table at all when the host described nothing", async () => {
    await draw({ ...question, actions: question.actions.map(({ description: _gone, ...a }) => a) });
    expect(host.querySelector(".inbox-outcomes")).toBeNull();
  });

  it("sends a free answer to a question as the answer's text", async () => {
    const fetch = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ resolution: "answered", notification: null }), { status: 200, headers: { "Content-Type": "application/json" } }));
    await draw(question);
    const input = host.querySelector<HTMLInputElement>(".inbox-reply-input")!;
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    await reactAct(async () => { set.call(input, "keep 8 and add the shopfront"); input.dispatchEvent(new Event("input", { bubbles: true })); });
    await reactAct(async () => host.querySelector<HTMLButtonElement>(".inbox-send")!.click());
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/notifications/5/act");
    expect(JSON.parse(String(init?.body))).toEqual({ action: "answer", value: "keep 8 and add the shopfront" });
    expect(host.querySelector(".inbox-outcome")?.textContent).toBe("Answered");
  });

  it("offers no free answer to a permission, which the host refuses one for", async () => {
    await draw(entry(6, { category: "permission", needs_you: true, request_ref: "policy:s1:k", body: "Exec: ls", actions: [{ id: "allow", label: "Allow", style: "primary", quick: true }] }));
    expect(host.querySelector(".inbox-reply")).toBeNull();
    expect(host.querySelector(".inbox-code")?.textContent).toBe("Exec: ls");
  });

  it("keeps a waiting request from being deleted out from under its run", async () => {
    await draw(question);
    expect(host.querySelector<HTMLButtonElement>(`button[aria-label="${t("inbox.delete")}"]`)!.disabled).toBe(true);
    await draw({ ...question, needs_you: false, resolved: "answered" });
    expect(host.querySelector<HTMLButtonElement>(`button[aria-label="${t("inbox.delete")}"]`)!.disabled).toBe(false);
  });
});
