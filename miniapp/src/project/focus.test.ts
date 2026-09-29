// @vitest-environment jsdom
// A project's focus mode decided without a browser: the route, the panel's tabs, the team as the
// sidebar draws it, the orchestrator's steps and events. What the agents list leaves out is mode.test.ts's.

import { describe, expect, it } from "vitest";
import { PANEL_TABS, PROJECT_TABS, defaultPanelTab, readPanelQuery, readPanelTab, tabsFor } from "../panel";
import { parse, projectHome, projectPagePath, projectSessionPath } from "../router";
import { eventTone, parseEvents, systemNote } from "../turns";
import type { Ask } from "../api";
import type { Staff } from "../team/team";
import { canFocus, firstWait, focusView, isChat, oldestOpen, PHONE_TABS, phoneTab, splitTeam, staffTone, stepDetail, stepKey, STEP_KEYS, teamCounts, waitKey } from "./focus";

function member(over: Partial<Staff> = {}): Pick<Staff, "status" | "queued" | "one_off" | "archived_at"> {
  return { status: "off", queued: [], one_off: false, archived_at: null, ...over };
}

describe("a project's route", () => {
  it("names the orchestrator, a session of the project, or one of its pages", () => {
    const home = parse("/app/orchestration/project/p1", "");
    expect([home.screen, home.project, home.page, home.inner]).toEqual(["orchestration", "p1", null, null]);
    expect(focusView(home.page, home.inner)).toEqual({ kind: "orchestrator" });
    const staff = parse("/app/orchestration/project/p1/s/sess-ira", "?panel=board");
    expect(focusView(staff.page, staff.inner)).toEqual({ kind: "session", id: "sess-ira" });
    expect(staff.query.get("panel")).toBe("board");
    expect(focusView("journal", null)).toEqual({ kind: "page", page: "journal" });
    // A page this build does not have is the project's home, not a blank centre.
    expect(focusView("nonsense", null)).toEqual({ kind: "orchestrator" });
    expect(focusView("s", null)).toEqual({ kind: "orchestrator" });
    expect(isChat({ kind: "orchestrator" })).toBe(true);
    expect(isChat({ kind: "page", page: "board" })).toBe(false);
  });

  it("builds the addresses it parses", () => {
    expect(projectHome("p 1", { panel: "board" })).toBe("/app/orchestration/project/p%201?panel=board");
    expect(projectSessionPath("p1", "s1")).toBe("/app/orchestration/project/p1/s/s1");
    expect(projectPagePath("p1", "terminals", { t: "t9", other: null })).toBe("/app/orchestration/project/p1/terminals?t=t9");
    const back = parse(projectSessionPath("p1", "s1"), "");
    expect([back.screen, back.project, back.page, back.inner]).toEqual(["orchestration", "p1", "s", "s1"]);
  });

  it("is offered for real projects only", () => {
    expect(canFocus({ system: "", settings: { snapshots: true } })).toBe(true);
    expect(canFocus({ system: "voice", settings: { snapshots: false } })).toBe(false);
    expect(canFocus({ settings: { snapshots: true, ephemeral: true } })).toBe(false);
    expect(canFocus(null)).toBe(false);
  });
});

describe("the right panel in focus mode", () => {
  it("offers the questions and the project's tabs beside the orchestrator, and both sets beside a session of the project", () => {
    expect(tabsFor("session")).toEqual(["details", "files", "preview", "jobs"]);
    // The orchestrator's own Details follow its questions: what it costs and how full it is.
    expect(tabsFor("orchestrator")).toEqual(["questions", "details", "board", "brief", "wakeups", "folders"]);
    expect(tabsFor("member")).toEqual([...PANEL_TABS, ...PROJECT_TABS]);
    // The main chat leads with every project's questions and keeps a session's own tabs behind them.
    expect(tabsFor("main")).toEqual(["questions", ...PANEL_TABS]);
  });

  it("reads only the tabs the context has from a link", () => {
    const q = new URLSearchParams("panel=files");
    expect(readPanelQuery(q, tabsFor("orchestrator"))).toBeNull();
    expect(readPanelQuery(new URLSearchParams("panel=details"), tabsFor("orchestrator"))?.tab).toBe("details");
    expect(readPanelQuery(new URLSearchParams("panel=brief"), tabsFor("orchestrator"))?.tab).toBe("brief");
    expect(readPanelQuery(new URLSearchParams("panel=brief"))).toBeNull();
  });

  it("opens on the context's first tab, and remembers the project's tab apart from a session's", () => {
    expect(defaultPanelTab(null, 1440, tabsFor("orchestrator"))).toBe("questions");
    expect(defaultPanelTab("files", 1440, tabsFor("orchestrator"))).toBe("questions");
    const kept = new Map<string, string>([["daedalus.session.panel", "files"], ["daedalus.session.panel.project", "brief"], ["daedalus.session.panel.main", "jobs"]]);
    const storage = { getItem: (k: string) => kept.get(k) ?? null };
    expect(readPanelTab(1440, storage, "session")).toBe("files");
    expect(readPanelTab(1440, storage, "orchestrator")).toBe("brief");
    expect(readPanelTab(1440, storage, "main")).toBe("jobs");
    expect(readPanelTab(900, storage, "orchestrator")).toBeNull();
    // A member's panel never opens on the questions it does not have.
    expect(defaultPanelTab("questions", 1440, tabsFor("member"))).toBe("details");
  });
});

describe("the team in the sidebar", () => {
  it("colours each member by what the operator would do about it", () => {
    expect(staffTone(member({ status: "working" }))).toBe("working");
    expect(staffTone(member({ status: "starting" }))).toBe("working");
    expect(staffTone(member({ status: "permission" }))).toBe("waiting");
    expect(staffTone(member({ status: "turn_done_unseen" }))).toBe("review");
    expect(staffTone(member({ status: "idle" }))).toBe("free");
    expect(staffTone(member({ status: "off" }))).toBe("off");
    expect(staffTone(member({ status: "exited" }))).toBe("off");
    // Silence is grey, not red: silent is not failed.
    expect(staffTone(member({ status: "no_signal" }))).toBe("silent");
    expect(staffTone(member({ status: "error" }))).toBe("error");
  });

  it("says why a launch waits, first in the queue first", () => {
    const queued = [
      { staff_id: "a", task_id: "t2", priority: 3, position: 4, reason: "project", detail: "6 of 6 staff are working", since: 1, by: "operator" },
      { staff_id: "a", task_id: "t1", priority: 1, position: 2, reason: "machine", detail: "20 of 20 terminal sessions are running", since: 2, by: "orchestrator" },
    ];
    const waiting = member({ status: "off", queued });
    expect(staffTone(waiting)).toBe("waiting");
    expect(firstWait(waiting)?.task_id).toBe("t1");
    expect(waitKey("machine")).toBe("focus.wait.machine");
    // A reason the host adds later still reads as something, never as a bare key.
    expect(waitKey("tea-break")).toBe("focus.wait.queued");
    expect(waitKey(null)).toBe("focus.wait.queued");
    expect(firstWait(member())).toBeNull();
  });

  it("lists the one-off helpers apart and leaves the dismissed out", () => {
    const staff = [
      { id: "a", one_off: false, archived_at: null },
      { id: "b", one_off: true, archived_at: null },
      { id: "c", one_off: false, archived_at: "2026-09-24T00:00:00Z" },
    ];
    const { team, oneOff } = splitTeam(staff);
    expect(team.map((m) => m.id)).toEqual(["a"]);
    expect(oneOff.map((m) => m.id)).toEqual(["b"]);
  });
});

describe("the orchestrator's steps", () => {
  it("names each step by what it did, not by the tool", () => {
    expect(stepKey("Folders", { op: "add", path: "/work/bakery-bot" })).toBe("Folders.add");
    expect(stepKey("Folders", {})).toBe("Folders.list");
    expect(stepKey("Tasks", { op: "create", title: "Notify: endpoint" })).toBe("Tasks.create");
    expect(stepKey("Brief", { section: "goals" })).toBe("Brief.read");
    expect(stepKey("Brief", { section: "goals", body: "…" })).toBe("Brief.write");
    expect(stepKey("Team", { concurrency: 4 })).toBe("Team.concurrency");
    expect(stepKey("Assign", { staff: "Max" })).toBe("Assign");
    expect(stepKey("Exec", { command: "ls" })).toBe("other");
    for (const key of ["Folders.add", "Tasks.create", "Assign", "Watch", "AskOperator", "other"]) expect(STEP_KEYS).toContain(key);
  });

  it("shows what a step acted on", () => {
    expect(stepDetail("Folders", { op: "add", path: "/work/bakery-bot" })).toBe("/work/bakery-bot");
    expect(stepDetail("Tasks", { op: "move", task_id: "t3a9c1", status: "review" })).toBe("t3a9c1 → review");
    expect(stepDetail("Assign", { staff: "Max", title: "Notify: endpoint" })).toBe("Max · Notify: endpoint");
    expect(stepDetail("AskOperator", { title: "Database", text: "Postgres or SQLite?\nContext…" })).toBe("Database");
    expect(stepDetail("AskOperator", { questions: [{ title: "Database", text: "?" }, { title: "Launch day", text: "?" }] })).toBe("Database · Launch day");
    expect(stepDetail("WithdrawQuestions", { ids: ["qk7m2x", "qa1b2c"], reason: "the brief settles it" })).toBe("qk7m2x, qa1b2c · the brief settles it");
  });
});

describe("the events the orchestrator was woken with", () => {
  const batch = [
    "[events · Bakery 2.0 · 3 since 14:30]",
    '- 14:31 Max (Codex) finished a turn on "Notification: endpoint" (t3a9c1): "3 files, tests green" — ReadStaff("Max") for the whole reply',
    "- 14:33 Naya needs permission [qk7m2]: Exec: npm install grammy (in bakery-bot) (autonomy normal) — yours to answer or escalate",
    "- 14:34 the operator moved \"Menu photos\" (t88e02) review → doing",
    "- … and 2 more (Team, Tasks)",
  ].join("\n");

  it("is recognised by its origin and drawn as a card", () => {
    expect(systemNote({ role: "user", text: batch, origin: "events", internal: false })?.kind).toBe("events");
  });

  it("reads back each line with its time and tone, without the advice meant for the model", () => {
    const parsed = parseEvents(batch)!;
    expect([parsed.project, parsed.count, parsed.since, parsed.more]).toEqual(["Bakery 2.0", 3, "14:30", 2]);
    expect(parsed.lines.map((l) => [l.time, l.tone])).toEqual([["14:31", "ok"], ["14:33", "warn"], ["14:34", "info"]]);
    expect(parsed.lines[0].text).toBe('Max (Codex) finished a turn on "Notification: endpoint" (t3a9c1): "3 files, tests green"');
    expect(parsed.lines[1].text).not.toContain("yours to answer");
    expect(parsed.lines[1].ask).toBe("qk7m2");
  });

  it("keeps a wrapped line with its event and refuses anything that is not a batch", () => {
    const wrapped = parseEvents("[events · P · 1 since 09:00]\n- 09:01 Ada reported stuck: \"the build\nfails on arm\"")!;
    expect(wrapped.lines).toHaveLength(1);
    expect(wrapped.lines[0].text).toContain("fails on arm");
    expect(wrapped.lines[0].tone).toBe("warn");
    expect(parseEvents("Hello")).toBeNull();
    expect(eventTone("Ada stopped with an error on \"Menu\": boom")).toBe("bad");
  });

  it("reads a failed approval as a failure, not as an answer, and keeps the model's advice out", () => {
    const failed = parseEvents([
      "[events · Work · 2 since 15:48]",
      "- 15:48 you approved request [qshpm9], but the folder /home/someone/labs could not be added: Work already has the folder /home/someone/labs",
      '- 15:48 the operator answered your request [qshpm9] "Add the host folder /home/someone/labs to Work?": approved, but the folder /home/someone/labs could not be added: Work already has the folder /home/someone/labs — nothing was added; do not ask for it again unless that reason is gone',
    ].join("\n"))!;
    expect(failed.lines.map((l) => l.tone)).toEqual(["bad", "bad"]);
    expect(failed.lines[1].text.endsWith("already has the folder /home/someone/labs")).toBe(true);
  });
});

describe("a project on a phone", () => {
  it("lights the tab of the route, keeps the bar on the other pages, and gives a session the whole height", () => {
    expect(phoneTab(focusView(null, null))).toEqual({ tab: "orchestrator", bar: true });
    expect(phoneTab(focusView("team", null))).toEqual({ tab: "team", bar: true });
    expect(phoneTab(focusView("board", null))).toEqual({ tab: "board", bar: true });
    expect(phoneTab(focusView("terminals", null))).toEqual({ tab: "terminals", bar: true });
    expect(phoneTab(focusView("journal", null))).toEqual({ tab: null, bar: true });
    expect(phoneTab(focusView("brief", null))).toEqual({ tab: null, bar: true });
    expect(phoneTab(focusView("s", "sess-lev"))).toEqual({ tab: null, bar: false });
    expect(PHONE_TABS).toEqual(["orchestrator", "team", "terminals", "board"]);
  });

  it("puts the operator's longest-waiting open request in the banner, and counts the rest", () => {
    const ask = (id: string, created_at: string, over: Partial<{ routed_to: string; resolved_at: string | null }> = {}) => ({ id, created_at, routed_to: "operator", resolved_at: null, ...over });
    const asks = [
      ask("newer", "2026-09-24T09:55:00Z"),
      ask("oldest", "2026-09-24T09:54:00Z"),
      ask("answered", "2026-09-24T09:00:00Z", { resolved_at: "2026-09-24T09:01:00Z" }),
      ask("orchestrator's", "2026-09-24T08:00:00Z", { routed_to: "orchestrator" }),
    ];
    expect(oldestOpen(asks)).toEqual({ ask: asks[1], waiting: 2 });
    expect(oldestOpen([])).toEqual({ ask: null, waiting: 0 });
  });

  it("counts members at work and tasks waiting for a look", () => {
    const staff = [
      { status: "working" as const, archived_at: null },
      { status: "starting" as const, archived_at: null },
      { status: "working" as const, archived_at: "2026-09-20T00:00:00Z" },
      { status: "idle" as const, archived_at: null },
      { status: "off" as const, archived_at: null, queued: [{ staff_id: "a", task_id: "t", priority: 1, position: 1, reason: "machine", detail: "", since: 0, by: "operator" }] },
    ];
    expect(teamCounts(staff, [{ status: "review" }, { status: "doing" }, { status: "review" }])).toEqual({ working: 2, review: 2 });
  });
});
