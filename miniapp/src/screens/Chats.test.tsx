// @vitest-environment jsdom
// The phone's Chats page: which section a chat lands in, and that the commands the old row carried
// ("…" and "+") are all in the new row's sheet.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ProjectFolder, SessionList, SessionSummary } from "../api";
import { arrange, dayGroup } from "../grouping";
import { setLang } from "../i18n";
import { ChatsScreen, chatSections } from "./Chats";

const listing = vi.hoisted(() => ({ data: null as SessionList | null, error: null as string | null, refresh: () => undefined }));
vi.mock("../store", () => ({ useQuery: () => ({ data: listing.data ?? undefined, loading: !listing.data && !listing.error, error: listing.error, refresh: listing.refresh }), useOffline: () => false, invalidate: () => undefined }));
vi.mock("../projects", () => ({ useEnvironments: () => ({ data: { local: "container", docker: true, host_bridge: true, available: [] } }), useProjects: () => ({ data: [{ id: "bakery", name: "Bakery site" }] }), ProjectSettingsSheet: () => <div data-settings />, MoveSessionSheet: () => <div data-move /> }));
vi.mock("../events", () => ({ useStreamUp: () => true }));

const NOW = new Date("2026-10-08T12:00:00");
const iso = (hoursAgo: number) => new Date(NOW.getTime() - hoursAgo * 3_600_000).toISOString();
function folder(id: string, name: string, total: number, last: string, ephemeral = false, pinned_at = ""): ProjectFolder {
  return { id, name, created_at: "2026-01-01T00:00:00Z", settings: { snapshots: false, ephemeral }, folders: [], total, members: total, active: 0, loops: 0, last_message_at: last, pinned_at };
}
function chat(id: string, project: string, hoursAgo: number, extra: Partial<SessionSummary> = {}): SessionSummary {
  return { id, title: `Chat ${id}`, project_id: project, project, model: "DeepSeek V4 Flash", status: "idle", created_at: iso(hoursAgo + 1), last_message_at: iso(hoursAgo), run_id: null, ...extra };
}

describe("dayGroup", () => {
  it("files a chat under today, yesterday, the week, the month or older", () => {
    expect(dayGroup(iso(1), NOW)).toBe("today");
    expect(dayGroup(iso(20), NOW)).toBe("yesterday");
    expect(dayGroup(iso(24 * 5), NOW)).toBe("week");
    expect(dayGroup(iso(24 * 20), NOW)).toBe("month");
    expect(dayGroup(iso(24 * 90), NOW)).toBe("older");
    expect(dayGroup("not a date", NOW)).toBe("older");
  });
});

describe("chatSections", () => {
  it("gives a project a section and files the chats by day", () => {
    const projects = [folder("bakery", "Bakery site", 2, iso(0.1)), folder("solo", "translator", 1, iso(5), true), folder("old", "Router", 1, iso(30), true)];
    const sessions = [chat("a", "bakery", 0.1), chat("b", "bakery", 2), chat("c", "solo", 5), chat("d", "old", 30)];
    const sections = chatSections(arrange(sessions, projects).folders, NOW);
    expect(sections.map((s) => s.folder?.name ?? s.day)).toEqual(["Bakery site", "today", "yesterday"]);
    expect(sections[0].rows.map((r) => r.s.id)).toEqual(["a", "b"]);
  });

  it("splits by the project rule: a project made by hand with one chat is a project, a chat with a fork is a chat", () => {
    const projects = [folder("esp", "Firmware", 1, iso(1)), folder("solo", "translator", 1, iso(2), true)];
    const fork = chat("f", "solo", 1.5, { metadata: { forked_from: { session_id: "c", seq: 4 } } });
    const sections = chatSections(arrange([chat("e", "esp", 1), chat("c", "solo", 2), fork], projects).folders, NOW);
    expect(sections.map((s) => s.folder?.name ?? s.day)).toEqual(["Firmware", "today"]);
    expect(sections[1].rows.map((r) => r.s.id)).toEqual(["c"]);
    expect(sections[1].rows[0].forks.map((r) => r.s.id)).toEqual(["f"]);
  });
});

describe("chatSections with pins", () => {
  it("puts what is pinned first, in one section, and leaves it out of the rest", () => {
    const projects = [folder("bakery", "Bakery site", 2, iso(0.1), false, "2026-10-01T00:00:00Z"), folder("solo", "translator", 1, iso(5), true, "2026-10-02T00:00:00Z"), folder("old", "Router", 1, iso(30), true)];
    const sessions = [chat("a", "bakery", 0.1), chat("b", "bakery", 2), chat("c", "solo", 5), chat("d", "old", 30)];
    const sections = chatSections(arrange(sessions, projects).folders, NOW);
    expect(sections.map((s) => s.key)).toEqual(["pinned", "day:yesterday"]);
    expect(sections[0].pinned!.map((f) => f.key)).toEqual(["solo", "bakery"]);
    expect(chatSections(arrange(sessions, projects).folders, NOW, { pins: false }).map((s) => s.key)).toEqual(["bakery", "day:today", "day:yesterday"]);
  });
});

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setLang("en");
  localStorage.clear();
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

describe("ChatsScreen", () => {
  it("keeps every command of the old row in the new row's sheet", async () => {
    const projects = [folder("bakery", "Bakery site", 2, iso(0.1))];
    const kid = chat("k", "bakery", 0.2, { title: "[sub] review", metadata: { subagent_of: "a", subagent_name: "review" } });
    listing.data = { sessions: [chat("a", "bakery", 0.1, { status: "waiting" }), chat("b", "bakery", 2), kid], projects, next_cursor: null } as SessionList;
    await act(async () => root.render(<ChatsScreen onOpen={vi.fn()} toast={vi.fn()} />));
    expect(host.textContent).toContain("Bakery site");
    expect(host.querySelectorAll(".ph-row")).toHaveLength(2);
    expect(host.querySelector(".ph-row-m")?.textContent).toContain("Needs you");
    await act(async () => host.querySelector<HTMLButtonElement>(".ph-row-more")!.click());
    const words = Array.from(document.querySelectorAll(".ph-mrow")).map((b) => b.textContent ?? "");
    expect(words).toEqual(expect.arrayContaining([
      "New chat in Bakery site", expect.stringContaining("review"), "Rename", "Move…", "Settings for Bakery site", "Archive", "Delete",
    ]));
    expect(words[words.length - 1]).toBe("Delete");
  });

  it("draws the pinned block first, a pinned project folding with its live chat still in sight, and offers Pin and Unpin", async () => {
    const projects = [folder("bakery", "Bakery site", 2, iso(0.1), false, "2026-10-01T00:00:00Z"), folder("solo", "translator", 1, iso(5), true, "2026-10-02T00:00:00Z"), folder("old", "Router", 1, iso(30), true)];
    listing.data = { sessions: [chat("a", "bakery", 0.1, { status: "waiting" }), chat("b", "bakery", 2), chat("c", "solo", 5), chat("d", "old", 30)], projects, next_cursor: null } as SessionList;
    await act(async () => root.render(<ChatsScreen onOpen={vi.fn()} toast={vi.fn()} />));
    const block = host.querySelector("[data-pinned]")!;
    expect(block.querySelector(".ph-sec")?.textContent).toContain("Pinned");
    expect(Array.from(block.querySelectorAll("[data-session]")).map((r) => (r as HTMLElement).dataset.session)).toEqual(["c", "a", "b"]);
    expect(host.querySelectorAll("[data-session=\"c\"]")).toHaveLength(1);
    // Folded, the project keeps the chat that waits for the operator.
    await act(async () => block.querySelector<HTMLElement>("[data-project-head=\"bakery\"] .ph-row")!.click());
    expect(Array.from(block.querySelectorAll("[data-session]")).map((r) => (r as HTMLElement).dataset.session)).toEqual(["c", "a"]);
    await act(async () => block.querySelector<HTMLButtonElement>("[data-session=\"c\"] .ph-row-more")!.click());
    expect(Array.from(document.querySelectorAll(".ph-mrow")).map((b) => b.textContent)).toContain("Unpin");
  });

  it("offers Pin on a chat below the block", async () => {
    listing.data = { sessions: [chat("d", "old", 30)], projects: [folder("old", "Router", 1, iso(30), true)], next_cursor: null } as SessionList;
    await act(async () => root.render(<ChatsScreen onOpen={vi.fn()} toast={vi.fn()} />));
    expect(host.querySelector("[data-pinned]")).toBeNull();
    await act(async () => host.querySelector<HTMLButtonElement>(".ph-row-more")!.click());
    expect(Array.from(document.querySelectorAll(".ph-mrow")).map((b) => b.textContent)).toContain("Pin");
  });

  it("says nothing matches under a chip with nothing in it and offers the whole list back", async () => {
    listing.data = { sessions: [chat("a", "solo", 1)], projects: [folder("solo", "translator", 1, iso(1))], next_cursor: null } as SessionList;
    await act(async () => root.render(<ChatsScreen onOpen={vi.fn()} toast={vi.fn()} />));
    await act(async () => Array.from(host.querySelectorAll<HTMLButtonElement>(".ph-chip")).find((b) => b.textContent?.startsWith("Loops"))!.click());
    expect(host.querySelector(".ph-empty")?.textContent).toContain("Nothing matches.");
    await act(async () => host.querySelector<HTMLButtonElement>(".ph-empty .ph-btn")!.click());
    expect(host.querySelectorAll(".ph-row")).toHaveLength(1);
  });

  it("shows the error with its cause and a way to try again, and a skeleton while it loads", async () => {
    listing.data = null;
    listing.error = null;
    await act(async () => root.render(<ChatsScreen onOpen={vi.fn()} toast={vi.fn()} />));
    expect(host.querySelectorAll(".ph-skrow").length).toBeGreaterThan(0);
    listing.error = "502 from /api/sessions";
    const refresh = vi.fn();
    listing.refresh = refresh;
    await act(async () => root.render(<ChatsScreen onOpen={vi.fn()} toast={vi.fn()} />));
    expect(host.querySelector(".ph-empty")?.textContent).toContain("502 from /api/sessions");
    await act(async () => host.querySelector<HTMLButtonElement>(".ph-empty .ph-btn")!.click());
    expect(refresh).toHaveBeenCalled();
    listing.error = null;
  });
});
