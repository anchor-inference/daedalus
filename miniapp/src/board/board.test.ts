import { describe, expect, it } from "vitest";
import { Assignee, NeedsYou, ProjectTask, acceptanceChip, acceptanceTone, arrange, briefChanges, chips, columnCount, columnOf, deliveryState, emptyBrief, hasAcceptance, mergeBlock, mergesOnAccept, missingBrief, requirementSource, sections, statusLine, toggleFilter } from "./board";

let seq = 0;
function task(fields: Partial<ProjectTask> = {}): ProjectTask {
  seq += 1;
  return {
    id: `t${seq}`,
    title: `task ${seq}`,
    status: "todo",
    priority: 3,
    acceptance: "",
    checklist: [],
    depends_on: [],
    session_id: null,
    notes: "",
    created_at: `2026-09-24T10:${String(seq).padStart(2, "0")}:00Z`,
    updated_at: `2026-09-24T10:${String(seq).padStart(2, "0")}:00Z`,
    project_id: "p",
    assignee_staff_id: null,
    brief: emptyBrief(),
    branch: null,
    merge_state: "",
    assignee: null,
    ...fields,
  };
}

function need(fields: Partial<NeedsYou> = {}): NeedsYou {
  return { id: "a1", short_id: "q12345", origin: "staff", kind: "question", text: "?", suggestion: "", created_at: "2026-09-24T10:00:00Z", task_id: null, task_title: null, staff: null, session_id: null, ...fields };
}

const ada = (fields: Partial<Assignee> = {}): Assignee => ({ id: "s1", name: "Ada", color: "blue", harness: "daedalus", archived_at: null, status: "working", on_task: true, waiting_for: "", status_at: "2026-09-24T10:00:00Z", session_id: "x", ...fields });

describe("the columns of a project board", () => {
  it("puts a blocked task in the queue and a dropped one with the finished", () => {
    expect((["todo", "blocked", "doing", "review", "done", "dropped"] as const).map(columnOf)).toEqual(["queue", "queue", "doing", "review", "done", "done"]);
  });

  it("reads each column in its own order", () => {
    const urgentOld = task({ status: "doing", priority: 1, updated_at: "2026-09-24T09:00:00Z" });
    const calm = task({ status: "doing", priority: 3, updated_at: "2026-09-24T11:00:00Z" });
    const urgentNew = task({ status: "doing", priority: 1, updated_at: "2026-09-24T12:00:00Z" });
    const waiting = task({ status: "blocked", priority: 1 });
    const readyLow = task({ status: "todo", priority: 4 });
    const readyHigh = task({ status: "todo", priority: 2 });
    const late = need({ id: "late", created_at: "2026-09-24T12:00:00Z" });
    const early = need({ id: "early", created_at: "2026-09-24T08:00:00Z" });
    const arranged = arrange({ tasks: [urgentOld, calm, urgentNew, waiting, readyLow, readyHigh], needs_you: [late, early] });
    expect(arranged.doing.map((t) => t.id)).toEqual([urgentNew.id, urgentOld.id, calm.id]);
    // What can start now comes before what waits for another task, whatever its priority.
    expect(arranged.queue.map((t) => t.id)).toEqual([readyHigh.id, readyLow.id, waiting.id]);
    // The request that has waited longest is first.
    expect(arranged.needs.map((n) => n.id)).toEqual(["early", "late"]);
  });

  it("counts finished work from the host, which does not send it until asked", () => {
    const arranged = arrange({ tasks: [task({ status: "review" })], needs_you: [need()] });
    const counts = { todo: 0, doing: 0, review: 1, done: 4, blocked: 0, dropped: 1, needs_you: 1 };
    expect(columnCount("done", arranged, counts)).toBe(5);
    expect(columnCount("done", arranged)).toBe(0);
    expect(chips(arranged, counts)).toEqual([
      { column: "needs", count: 1 },
      { column: "review", count: 1 },
      { column: "done", count: 5 },
    ]);
  });
});

describe("the phone's chips over one list", () => {
  const arranged = arrange({ tasks: [task({ status: "doing" }), task({ status: "todo" }), task({ status: "done" })], needs_you: [] });

  it("lists every open column that has something, and folds the finished away", () => {
    expect(sections(null, arranged)).toEqual(["doing", "queue"]);
  });

  it("shows one column under a pressed chip, and a second tap lets go", () => {
    expect(sections("review", arranged)).toEqual(["review"]);
    expect(sections("done", arranged)).toEqual(["done"]);
    expect(toggleFilter(null, "doing")).toBe("doing");
    expect(toggleFilter("doing", "doing")).toBeNull();
    expect(toggleFilter("doing", "queue")).toBe("queue");
  });
});

describe("a card's status line", () => {
  it("speaks for the assignee only while their session works on this task", () => {
    expect(statusLine(task({ status: "doing", assignee: ada() }), {})).toEqual({ kind: "working", status: "working", since: "2026-09-24T10:00:00Z", waiting: "" });
    expect(statusLine(task({ status: "todo", assignee: ada({ on_task: false }) }), {})).toEqual({ kind: "waiting", name: "Ada" });
  });

  it("names the first open task a blocked one waits for", () => {
    const titles = { d1: { title: "Checkout", status: "done" as const }, d2: { title: "Menu", status: "doing" as const }, d3: { title: "Photos", status: "todo" as const } };
    expect(statusLine(task({ status: "blocked", depends_on: ["d1", "d2", "d3"] }), titles)).toEqual({ kind: "after", title: "Menu", more: 1 });
  });

  it("says nothing for a task nobody is on and nothing holds up", () => {
    expect(statusLine(task({ status: "review", assignee: ada({ on_task: false }) }), {})).toEqual({ kind: "none" });
    expect(statusLine(task({ status: "blocked", depends_on: ["gone"] }), {})).toEqual({ kind: "none" });
  });
});

describe("a task's brief", () => {
  it("names the parts still empty", () => {
    expect(missingBrief({ objective: "a menu", deliverable: " ", boundaries: "", done_when: "it renders" })).toEqual(["deliverable", "boundaries"]);
    expect(missingBrief({ objective: "a", deliverable: "b", boundaries: "c", done_when: "d" })).toEqual([]);
  });

  it("sends only the parts that changed", () => {
    const before = { objective: "a", deliverable: "b", boundaries: "", done_when: "" };
    expect(briefChanges(before, { ...before, boundaries: "only src" })).toEqual({ boundaries: "only src" });
    expect(briefChanges(before, before)).toEqual({});
  });
});

describe("review and merge", () => {
  const blocker = (code: string) => ({ code, text: code });
  it("names no reason when Merge may be pressed", () => {
    expect(mergeBlock({ can_merge: true, blockers: [] })).toBeNull();
  });
  it("puts a conflict first, since that one goes back to the orchestrator", () => {
    expect(mergeBlock({ can_merge: false, blockers: [blocker("dirty"), blocker("conflicts")] })?.code).toBe("conflicts");
    expect(mergeBlock({ can_merge: false, blockers: [blocker("moved"), blocker("dirty")] })?.code).toBe("moved");
    expect(mergeBlock({ can_merge: false, blockers: [] })?.code).toBe("unknown");
  });
  it("makes Accept a merge only for unmerged staff work", () => {
    expect(mergesOnAccept({ branch: "agent/ada/t1-menu", merge_state: "proposed" })).toBe(true);
    expect(mergesOnAccept({ branch: "agent/ada/t1-menu", merge_state: "conflict" })).toBe(true);
    expect(mergesOnAccept({ branch: "agent/ada/t1-menu", merge_state: "merged" })).toBe(false);
    expect(mergesOnAccept({ branch: null, merge_state: "" })).toBe(false);
  });
});

describe("the task contract", () => {
  it("chips the acceptance level only on a card in review or done", () => {
    expect(acceptanceChip(task({ status: "done", acceptance_state: "handed_in" }))).toEqual({ state: "handed_in", tone: "" });
    expect(acceptanceChip(task({ status: "review", acceptance_state: "accepted" }))).toEqual({ state: "accepted", tone: "ok" });
    expect(acceptanceChip(task({ status: "done", acceptance_state: "operator_approved" }))?.tone).toBe("ok");
    expect(acceptanceChip(task({ status: "review", acceptance_state: "returned" }))?.tone).toBe("attn");
    expect(acceptanceChip(task({ status: "doing", acceptance_state: "returned" }))).toBeNull();
    expect(acceptanceChip(task({ status: "done", acceptance_state: "" }))).toBeNull();
    // An older host sends no acceptance at all.
    expect(acceptanceChip(task({ status: "done" }))).toBeNull();
    expect(acceptanceTone("handed_in")).toBe("");
  });

  it("has an acceptance section for a level or a check with a word or a mark on it", () => {
    expect(hasAcceptance(task({ checklist: [{ text: "tests pass", done: false }] }))).toBe(false);
    expect(hasAcceptance(task({ checklist: [{ text: "tests pass", done: true, evidence: { how: "pytest", result: "12 passed" } }] }))).toBe(true);
    expect(hasAcceptance(task({ acceptance_state: "returned" }))).toBe(true);
  });

  it("names where a requirement came from", () => {
    expect(requirementSource({ source: "operator", from_operator: true })).toEqual({ key: "operator", ref: "" });
    expect(requirementSource({ source: "answer:q4r8tz", from_operator: true })).toEqual({ key: "answer", ref: "q4r8tz" });
    expect(requirementSource({ source: "rule:120", from_operator: true })).toEqual({ key: "rule", ref: "120" });
    expect(requirementSource({ source: "orchestrator", from_operator: false })).toEqual({ key: "orchestrator", ref: "" });
    expect(requirementSource({ source: "operator:telegram", from_operator: true }).key).toBe("operator");
  });

  it("says how far a requirement got with a member, and a command-line member's input only in words", () => {
    const sent = { opened_at: null, acknowledged_at: null, cli: false };
    expect(deliveryState(sent, "quality")).toBe("sent");
    expect(deliveryState({ ...sent, acknowledged_at: "2026-09-24T10:00:00Z" }, "quality")).toBe("confirmed");
    expect(deliveryState({ ...sent, acknowledged_at: "2026-09-24T10:00:00Z", opened_at: "2026-09-24T10:01:00Z" }, "input")).toBe("opened");
    expect(deliveryState({ ...sent, acknowledged_at: "2026-09-24T10:00:00Z", cli: true }, "input")).toBe("words");
    expect(deliveryState({ ...sent, acknowledged_at: "2026-09-24T10:00:00Z", cli: true }, "scope")).toBe("confirmed");
    expect(deliveryState({ ...sent, cli: true }, "input")).toBe("sent");
  });
});
