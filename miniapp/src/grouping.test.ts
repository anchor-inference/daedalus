import { beforeEach, describe, expect, it } from "vitest";
import type { ProjectFolder, SessionSummary } from "./api";
import { arrange, folderOpen, isProject, rememberFolder, sidebarSections, splitPinned } from "./grouping";

let clock = 1_700_000_000_000;

function agent(id: string, over: Partial<SessionSummary> = {}): SessionSummary {
  clock += 1000;
  const at = new Date(clock).toISOString();
  return { id, title: id, status: "idle", created_at: at, last_message_at: at, run_id: null, project_id: "p", project: "P", ...over };
}

function folder(id: string, name: string, over: Partial<ProjectFolder> = {}): ProjectFolder {
  return {
    id,
    name,
    created_at: "2026-09-01T00:00:00Z",
    settings: { snapshots: false, system: "" },
    folders: [{ id: `f-${id}`, path: `/projects/${name}`, label: "", env: "container", is_git: false, readonly: false, position: 0, managed: false, reachable: true, writable: true }],
    total: 0,
    active: 0,
    loops: 0,
    last_message_at: "",
    ...over,
  };
}

describe("arrange", () => {
  it("changes a folder's signature when an agent's result becomes unread, so the memo redraws the row", () => {
    const projects = [folder("p1", "Bakery", { total: 1 })];
    const seen = agent("a", { project_id: "p1" });
    const before = arrange([seen], projects).folders[0].sig;
    const after = arrange([{ ...seen, unread_result: true }], projects).folders[0].sig;
    expect(after).not.toBe(before);
  });

  it("puts every agent in its project", () => {
    const projects = [folder("p1", "Bakery", { total: 2 }), folder("p2", "Expenses", { total: 1 })];
    const sessions = [
      agent("a", { project_id: "p1" }),
      agent("b", { project_id: "p1" }),
      agent("c", { project_id: "p2" }),
    ];
    const { folders } = arrange(sessions, projects);
    expect(folders.map((f) => f.key)).toEqual(["p2", "p1"]);
    expect(folders[1].rows.map((r) => r.s.id)).toEqual(["b", "a"]);
    // The count on a folder is the API's, over the whole table — not the rows that reached this page.
    expect(folders[1].total).toBe(2);
  });

  it("sorts every project by activity, including Voice and empty projects", () => {
    const projects = [folder("p2", "Zebra", { last_message_at: "2026-09-19T00:00:00Z" }), folder("p1", "Apples"), folder("v", "Voice", { system: "voice", last_message_at: "2026-09-18T00:00:00Z" })];
    const { folders } = arrange([], projects);
    expect(folders.map((f) => f.name)).toEqual(["Zebra", "Voice", "Apples"]);
    expect(folders[1].system).toBe(true);
  });

  it("nests a subagent under its leader and a fork under what it was taken from", () => {
    const sessions = [
      agent("leader", { project_id: "p" }),
      agent("sub", { project_id: "p", metadata: { subagent_of: "leader", subagent_name: "triage" } }),
      agent("fork", { project_id: "p", metadata: { forked_from: { session_id: "leader", seq: 12 } } }),
      agent("forkfork", { project_id: "p", metadata: { forked_from: { session_id: "fork", seq: 20 } } }),
      // A fork of a session that is not in the list stands on its own.
      agent("orphan-fork", { project_id: "p", metadata: { forked_from: { session_id: "gone", seq: 3 } } }),
    ];
    const { folders } = arrange(sessions, [folder("p", "Project")]);
    const rows = folders[0].rows;
    expect(rows.map((r) => r.s.id)).toEqual(["orphan-fork", "leader"]);
    expect(rows[1].kids.map((k) => k.id)).toEqual(["sub"]);
    expect(rows[1].forks.map((f) => f.s.id)).toEqual(["fork"]);
    expect(rows[1].forks[0].forks.map((f) => f.s.id)).toEqual(["forkfork"]);
  });

  it("counts a leader whose subagent is running as working", () => {
    const sessions = [agent("leader", { project_id: "p" }), agent("sub", { project_id: "p", status: "running", metadata: { subagent_of: "leader" } })];
    const { active, total } = arrange(sessions, [folder("p", "Project")]);
    expect(total).toBe(1);
    expect(active).toBe(1);
  });

  it("applies a filter inside every project folder", () => {
    const projects = [folder("p1", "Bakery")];
    const loop = { mode: "interval" as const, interval_seconds: 60, status: "active" as const, run_count: 1, max_runs: null, next_run_at: null, last_run_at: null, last_reason: null, stop_reason: null, pause_note: null, instruction: "x" };
    const sessions = [
      agent("busy", { status: "running", project_id: "p1" }),
      agent("quiet", { project_id: "p1" }),
      agent("looping", { project_id: "p1", metadata: { loop } }),
      agent("alone", { project_id: "p1" }),
    ];
    const working = arrange(sessions, projects, { filter: "working" });
    expect(working.folders[0].rows.map((r) => r.s.id)).toEqual(["busy"]);
    expect(working.shown).toBe(1);
    const loops = arrange(sessions, projects, { filter: "loops" });
    expect(loops.folders[0].rows.map((r) => r.s.id)).toEqual(["looping"]);
    // The header's own counts describe the screen and not the filter, which is what "All · n" means.
    expect(loops.total).toBe(4);
    expect(loops.loops).toBe(1);
  });

  it("searches across the folders and keeps a matching fork's parent on screen", () => {
    const projects = [folder("p1", "Bakery")];
    const sessions = [
      agent("menu", { title: "Menu page", project_id: "p1" }),
      agent("photos", { title: "Photos", project_id: "p1" }),
      agent("fork", { title: "Menu page (fork)", project_id: "p1", metadata: { forked_from: { session_id: "menu", seq: 4 } } }),
      agent("sub", { title: "[sub] menu prices", project_id: "p1", metadata: { subagent_of: "photos", subagent_name: "menu prices" } }),
    ];
    const found = arrange(sessions, projects, { query: "menu" });
    expect(found.folders[0].rows[1].forks.map((f) => f.s.id)).toEqual(["fork"]);
    // "Photos" itself does not match; the subagent named "menu prices" under it does.
    expect(found.folders[0].rows.map((r) => r.s.id)).toEqual(["photos", "menu"]);
    expect(arrange(sessions, projects, { query: "nothing here" }).shown).toBe(0);
  });

  it("narrows the whole screen to one project", () => {
    const projects = [folder("p1", "Bakery"), folder("p2", "Expenses")];
    const sessions = [agent("a", { project_id: "p1" }), agent("b", { project_id: "p2" })];
    const { folders, total } = arrange(sessions, projects, { project: "p1" });
    expect(folders.map((f) => f.key)).toEqual(["p1"]);
    expect(total).toBe(1);
  });
});

describe("isProject", () => {
  it("lists a chat's scratch project as the chat, and everything else as a project", () => {
    expect(isProject({ settings: { snapshots: true, ephemeral: true } })).toBe(false);
    // Made by hand, or kept: a project even with one chat or none.
    expect(isProject({ settings: { snapshots: false } })).toBe(true);
    expect(isProject({ settings: { snapshots: false, ephemeral: false } })).toBe(true);
    // The installation's own project is one whatever it holds.
    expect(isProject({ settings: { snapshots: false, system: "voice", ephemeral: true } })).toBe(true);
    expect(isProject({ settings: { snapshots: false, ephemeral: true }, system: "voice" })).toBe(true);
  });
});

describe("sidebarSections", () => {
  const now = new Date("2026-10-09T12:00:00");
  const at = (hours: number) => new Date(now.getTime() - hours * 3_600_000).toISOString();
  const chatProject = (id: string, last: string) => folder(id, id, { total: 1, last_message_at: last, settings: { snapshots: true, ephemeral: true } });

  it("puts projects in one section and the chats by day, the month folded into earlier", () => {
    const projects = [folder("esp", "Firmware", { total: 0 }), chatProject("c1", at(1)), chatProject("c2", at(30)), chatProject("c3", at(24 * 4)), chatProject("c4", at(24 * 20)), chatProject("c5", at(24 * 90))];
    const sessions = ["c1", "c2", "c3", "c4", "c5"].map((id, i) => agent(id, { project_id: id, last_message_at: projects[i + 1].last_message_at }));
    const { projects: listed, days, chats } = sidebarSections(arrange(sessions, projects).folders, { now });
    // An empty project made by hand is listed: it is a project before its first chat.
    expect(listed.map((f) => f.key)).toEqual(["esp"]);
    expect(days.map((d) => [d.day, d.rows.map((r) => r.s.id)])).toEqual([["today", ["c1"]], ["yesterday", ["c2"]], ["week", ["c3"]], ["older", ["c4", "c5"]]]);
    expect(chats).toBe(5);
  });

  it("keeps only the projects with something in the answer under a filter", () => {
    const projects = [folder("esp", "Firmware", { total: 0 }), folder("home", "Home", { total: 1 })];
    const { projects: listed } = sidebarSections(arrange([agent("h", { project_id: "home" })], projects).folders, { filtered: true, now });
    expect(listed.map((f) => f.key)).toEqual(["home"]);
  });

  it("makes a scratch project a project once the server clears its mark", () => {
    const sessions = [agent("a", { project_id: "s" }), agent("b", { project_id: "s" })];
    const scratch = chatProject("s", at(1));
    expect(sidebarSections(arrange(sessions, [scratch]).folders, { now }).projects).toHaveLength(0);
    const kept = { ...scratch, settings: { ...scratch.settings, ephemeral: false } };
    expect(sidebarSections(arrange(sessions, [kept]).folders, { now }).projects.map((f) => f.key)).toEqual(["s"]);
  });
});

describe("pins", () => {
  const now = new Date("2026-10-09T12:00:00");
  const at = (hours: number) => new Date(now.getTime() - hours * 3_600_000).toISOString();
  const chatProject = (id: string, last: string, pinned_at = "") => folder(id, id, { total: 1, last_message_at: last, pinned_at, settings: { snapshots: true, ephemeral: true } });
  const projects = () => [
    folder("esp", "Firmware", { total: 1, pinned_at: "2026-10-01T10:00:00Z" }),
    folder("home", "Home", { total: 1 }),
    folder("empty", "Empty", { total: 0, pinned_at: "2026-10-03T10:00:00Z" }),
    chatProject("c1", at(1), "2026-10-02T10:00:00Z"),
    chatProject("c2", at(30)),
  ];
  const sessions = () => [
    agent("e", { project_id: "esp", status: "running" }),
    agent("h", { project_id: "home" }),
    agent("c1", { project_id: "c1", last_message_at: at(1) }),
    agent("c2", { project_id: "c2", last_message_at: at(30) }),
  ];

  it("lists what is pinned in its own block, newest pin first, and nowhere below it", () => {
    const { pinned, projects: listed, days, chats } = sidebarSections(arrange(sessions(), projects()).folders, { now });
    // An empty project is a project before its first chat, pinned or not.
    expect(pinned.map((f) => f.key)).toEqual(["empty", "c1", "esp"]);
    expect(listed.map((f) => f.key)).toEqual(["home"]);
    expect(days.map((d) => [d.day, d.rows.map((r) => r.s.id)])).toEqual([["yesterday", ["c2"]]]);
    expect(chats).toBe(1);
  });

  it("applies the filter and the search to the block too", () => {
    // Only what works: the running chat's project stays, the idle chat and the empty project go.
    const working = arrange(sessions(), projects(), { query: "e" }).folders;
    expect(sidebarSections(working, { filtered: true, now }).pinned.map((f) => f.key)).toEqual(["esp"]);
    expect(sidebarSections(arrange(sessions(), projects(), { query: "nothing" }).folders, { filtered: true, now }).pinned).toEqual([]);
  });

  it("drops a pinned chat whose row is gone, archived or deleted", () => {
    const left = sessions().filter((s) => s.id !== "c1");
    expect(sidebarSections(arrange(left, projects()).folders, { now }).pinned.map((f) => f.key)).toEqual(["empty", "esp"]);
  });

  it("keeps the pin on a chat that became a project, and lists it as a pinned project", () => {
    const grown = projects().map((p) => (p.id === "c1" ? { ...p, settings: { ...p.settings, ephemeral: false } } : p));
    const two = [...sessions(), agent("c1b", { project_id: "c1" })];
    const pinned = sidebarSections(arrange(two, grown).folders, { now }).pinned;
    expect(pinned.map((f) => [f.key, f.chat])).toEqual([["empty", false], ["c1", false], ["esp", false]]);
    expect(pinned[1].rows.map((r) => r.s.id).sort()).toEqual(["c1", "c1b"]);
  });

  it("pins nothing where pins are off, for the archive", () => {
    const folders = arrange(sessions(), projects()).folders;
    expect(splitPinned(folders, { pins: false })).toEqual({ pinned: [], rest: folders });
  });
});

describe("folderOpen", () => {
  // There is no DOM in this repo's vitest setup, so the store the module reads is supplied here.
  // What is being tested is the default and the per-folder memory, not the browser's implementation.
  const store = new Map<string, string>();
  beforeEach(() => {
    store.clear();
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
  });

  it("is collapsed until it is opened, and remembers per folder", () => {
    expect(folderOpen("p1")).toBe(false);
    rememberFolder("p1", true);
    expect(folderOpen("p1")).toBe(true);
    expect(folderOpen("p2")).toBe(false);
    rememberFolder("p1", false);
    expect(folderOpen("p1")).toBe(false);
  });
});
