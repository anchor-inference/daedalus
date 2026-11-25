import { afterEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { assigneeName, cardState, dueDay, dueLabel, dueValue, eventCause, eventText, movesFor, taskText, type BoardTask } from "./tasks";

const task = (extra: Partial<BoardTask> = {}): BoardTask => ({
  id: "7f2a1c", title: "Order form", status: "todo", priority: 3, acceptance: "", checklist: [], depends_on: [], session_id: null, origin_session_id: "s1",
  notes: "", created_at: "2026-11-20T10:00:00Z", updated_at: "2026-11-24T10:00:00Z", ...extra,
});

afterEach(() => setLang("en"));

describe("a board card", () => {
  it("offers only the moves the host takes from the operator's plain edit", () => {
    expect(movesFor(task())).toEqual(["blocked", "dropped"]);
    expect(movesFor(task({ status: "doing" }))).toEqual([]);
    expect(movesFor(task({ status: "review" }))).toEqual([]);
    expect(movesFor(task({ status: "done" }))).toEqual([]);
    expect(movesFor(task({ status: "dropped" }))).toEqual(["todo"]);
    // A card the operator keeps is moved into work by their word alone.
    const own = { assignee: { kind: "operator" as const } };
    expect(movesFor(task(own))).toEqual(["doing", "blocked", "dropped"]);
    expect(movesFor(task({ ...own, status: "doing" }))).toEqual(["todo", "blocked"]);
  });

  it("says what it waits on, and that its chat is at work only while it is in Doing", () => {
    const running = (id: string) => id === "s1";
    expect(cardState(task({ needs_you: "approval" }), running)).toEqual({ tone: "attention", text: "Your approval" });
    expect(cardState(task({ needs_you: "assigned" }), running)).toEqual({ tone: "attention", text: "Yours" });
    expect(cardState(task({ status: "doing" }), running)).toEqual({ tone: "live", text: "working" });
    expect(cardState(task(), running)).toBeNull();
    expect(cardState(task({ status: "done", needs_you: "review" }), running)).toBeNull();
  });

  it("names who does it, with no name for the operator", () => {
    expect(assigneeName(task())).toBe("Agent");
    expect(assigneeName(task({ assignee: { kind: "operator" } }))).toBe("you");
    expect(assigneeName(task({ assignee: { kind: "staff", id: "st", name: "Ira", color: "teal" } }))).toBe("Ira");
  });

  it("reads a due date picked as a day as that day in every zone", () => {
    expect(dueDay("2026-11-26T23:59:59Z")).toBe("2026-11-26");
    expect(dueDay("2026-11-26T10:00:00Z")).toBeNull();
    expect(dueValue("2026-11-26T23:59:59Z")).toBe("2026-11-26");
    const now = new Date(2026, 10, 25, 12, 0);
    expect(dueLabel("2026-11-25T23:59:59Z", now)).toBe("today");
    expect(dueLabel("2026-11-26T23:59:59Z", now, true)).toMatch(/^Tomorrow, 26/);
    expect(dueLabel("2026-11-28T23:59:59Z", now)).toMatch(/28/);
    setLang("ru");
    expect(dueLabel("2026-11-24T23:59:59Z", now)).toBe("вчера");
  });

  it("merges the brief into one description and one acceptance, saying a restated check once", () => {
    const text = taskText({ acceptance: "plan.md names every limit.", brief: { objective: "Plan the limits", deliverable: "plan.md", boundaries: "", done_when: "plan.md names every limit" } });
    expect(text.description).toEqual([{ text: "Plan the limits" }, { lead: "Deliverable", text: "plan.md" }]);
    expect(text.acceptance).toEqual(["plan.md names every limit."]);
    expect(taskText({ acceptance: "Every pot is wet.", brief: { done_when: "The owner says so" } }).acceptance).toEqual(["The owner says so", "Every pot is wet."]);
    expect(taskText({ acceptance: "", brief: {} })).toEqual({ description: [], acceptance: [] });
  });

  it("words its history in the reader's language from the event's fields", () => {
    const at = "2026-11-24T10:00:00Z";
    const actor = { kind: "agent", id: "session:s1", name: "Menu" };
    expect(eventText({ seq: 1, type: "task.moved", at, actor, from: "todo", to: "doing", text: "todo → doing" })).toBe("To do → Doing");
    expect(eventText({ seq: 2, type: "task.assigned", at, actor, from: null, to: null, text: "", assignee: { kind: "operator" } })).toBe("You kept it for yourself");
    expect(eventText({ seq: 3, type: "task.changed", at, actor, from: null, to: null, text: "", due_at: null })).toBe("Due date cleared");
    expect(eventText({ seq: 5, type: "task.changed", at, actor, from: null, to: null, text: "", field: "priority", change: { from: 3, to: 1 } })).toBe("Priority Normal → Urgent");
    expect(eventText({ seq: 6, type: "task.changed", at, actor, from: null, to: null, text: "", field: "title", change: { from: "a", to: "Book the venue" } })).toBe("Renamed to “Book the venue”");
    expect(eventText({ seq: 7, type: "task.removed", at, actor, from: null, to: null, text: "" })).toBe("Removed from the board");
    // A creation rebuilt from an old card names no column rather than guessing To do.
    expect(eventText({ seq: 8, type: "task.created", at, actor, from: null, to: null, text: "Created", backfilled: true })).toBe("Created");
    const moved = { seq: 9, type: "task.moved", at, actor, from: "blocked", to: "todo", text: "", cause: { task_id: "3f2a1c", title: "Book the venue" } };
    expect(eventCause({ ...moved, operation: "board.task.close" })).toBe("because “Book the venue” was closed");
    expect(eventCause({ ...moved, operation: "review.verdict" })).toBe("because “Book the venue” was finished");
    expect(eventCause({ ...moved, cause: undefined })).toBe("");
    setLang("ru");
    expect(eventText({ seq: 4, type: "task.created", at, actor, from: null, to: "todo", text: "" })).toBe("Создана в колонке «К работе»");
  });
});
