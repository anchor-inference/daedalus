// @vitest-environment jsdom
// The export of one conversation over an agent link: Zip saves what the host built under the name it gave, To notes
// says where the transcript went, a failure is said rather than swallowed, and the row is in the Agents tab for a
// conversation that is over as much as for a live one.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentTalk, LinkExportTarget } from "./api";
import { setLang, t } from "./i18n";

const world = vi.hoisted(() => ({
  zips: [] as [LinkExportTarget, string][], notes: [] as [LinkExportTarget, string][], fail: "" as string,
}));
vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  api: { get: async () => ({ messages: [], files: [] }), post: async () => ({}), put: async () => ({}), fetch: async () => new Response("") },
  linkExportZip: async (target: LinkExportTarget, language: string) => {
    world.zips.push([target, language]);
    if (world.fail) throw new Error(world.fail);
    return { blob: new Blob(["zip"]), name: "conversation-agents-example-test-2026-11-11.zip" };
  },
  linkExportNote: async (target: LinkExportTarget, language: string) => {
    world.notes.push([target, language]);
    if (world.fail) throw new Error(world.fail);
    return { note_id: "n1", session_id: "s1", title: "Conversation with agents.example.test — 2026-11-11" };
  },
}));

import { AgentsTab } from "./agenttalk";
import { LinkExport } from "./linkexport";

let host: HTMLDivElement;
let root: Root;
let saved: string[];

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setLang("en");
  world.zips = [];
  world.notes = [];
  world.fail = "";
  saved = [];
  URL.createObjectURL = vi.fn(() => "blob:export");
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(this.download);
  });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
});

async function click(selector: string) {
  await act(async () => {
    host.querySelector<HTMLButtonElement>(selector)!.click();
  });
  await act(async () => {});
}

describe("the export of a conversation", () => {
  it("saves the zip under the host's name and in the app's language", async () => {
    setLang("ru");
    const toasts: string[] = [];
    act(() => root.render(<LinkExport target={{ kind: "guest", id: "ab12" }} toast={(text) => toasts.push(text)} />));
    expect(host.querySelector("[data-link-export-zip]")!.textContent).toBe("Архив");
    expect(host.querySelector("[data-link-export-note]")!.textContent).toBe("В заметки");
    await click("[data-link-export-zip]");
    expect(world.zips).toEqual([[{ kind: "guest", id: "ab12" }, "ru"]]);
    expect(saved).toEqual(["conversation-agents-example-test-2026-11-11.zip"]);
    expect(toasts).toEqual([]);
  });

  it("says the transcript went to notes", async () => {
    const toasts: string[] = [];
    act(() => root.render(<LinkExport target={{ kind: "talk", id: "t1" }} toast={(text) => toasts.push(text)} />));
    await click("[data-link-export-note]");
    expect(world.notes).toEqual([[{ kind: "talk", id: "t1" }, "en"]]);
    expect(toasts).toEqual([]);
    expect(host.querySelector("[data-link-export-done]")!.textContent).toBe(t("link.export.saved"));
  });

  it("tells a failure and saves nothing", async () => {
    world.fail = "no such conversation";
    const toasts: string[] = [];
    act(() => root.render(<LinkExport target={{ kind: "talk", id: "t1" }} toast={(text) => toasts.push(text)} />));
    await click("[data-link-export-zip]");
    await click("[data-link-export-note]");
    expect(saved).toEqual([]);
    expect(toasts).toHaveLength(2);
    expect(toasts.every((text) => /no such conversation/i.test(text))).toBe(true);
    expect(host.querySelector<HTMLButtonElement>("[data-link-export-zip]")!.disabled).toBe(false);
  });
});

const talk = (over: Partial<AgentTalk> = {}): AgentTalk => ({
  id: "t1", session_id: "s1", host: "agents.example.test", topic: "", mode: "direct", state: "open", reason: "", waiting_for: "", quiet: false, review: "first",
  sends: 1, sends_limit: 30, wake_turns: 0, wake_usd: 0, created_at: "", last_at: "", closed_at: null, spent_usd: 0, replies: 0, unread: 0, held: 0, file_mb: 50, day_mb: 500, tasks: [], ...over,
});

describe("where the export stands", () => {
  it("is on every conversation in the Agents tab, a closed one too", () => {
    act(() => root.render(<AgentsTab sessionId="s1" talks={[talk(), talk({ id: "t2", state: "closed", reason: "ended" })]} loaded toast={() => {}} />));
    expect(host.querySelectorAll("[data-link-export='talk']").length).toBe(2);
    expect(host.querySelector("[data-talk='t2'] [data-link-export-zip]")).not.toBeNull();
  });
});
