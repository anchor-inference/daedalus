import { describe, expect, it } from "vitest";
import { byProject, filtered, storedGrouping } from "./boardview";

const card = (id: string, extra: Record<string, unknown> = {}) => ({ id, status: "todo", priority: 3, updated_at: "2026-10-20T10:00:00Z", project_id: null, project_name: null, needs_you: null, ...extra });

describe("the board of every project", () => {
  it("keeps only the cards that wait on the operator under Needs you", () => {
    const cards = [card("a", { needs_you: "question" }), card("b"), card("c", { needs_you: "review", status: "review" }), card("d", { needs_you: "approval" })];
    expect(filtered(cards, true).map((c) => c.id)).toEqual(["a", "c", "d"]);
    expect(filtered(cards, false)).toHaveLength(4);
  });

  it("does not guess from the status when the host sends no reason", () => {
    expect(filtered([card("a", { status: "review", needs_you: undefined })], true)).toEqual([]);
  });

  it("groups by project, by name, with the tasks of no project last, each in the order the work flows", () => {
    const groups = byProject([
      card("loose"),
      card("g-todo", { project_id: "g", project_name: "Garden" }),
      card("b-doing", { project_id: "b", project_name: "Bakery", status: "doing" }),
      card("g-review", { project_id: "g", project_name: "Garden", status: "review" }),
      card("g-urgent", { project_id: "g", project_name: "Garden", priority: 1 }),
    ]);
    expect(groups.map((g) => [g.name, g.items.map((c) => c.id)])).toEqual([
      ["Bakery", ["b-doing"]],
      ["Garden", ["g-review", "g-urgent", "g-todo"]],
      ["", ["loose"]],
    ]);
    expect(groups[2].projectId).toBeNull();
  });

  it("remembers the grouping, and falls back to the form's own on anything else", () => {
    expect(storedGrouping(() => "project", "status")).toBe("project");
    expect(storedGrouping(() => "status", "project")).toBe("status");
    expect(storedGrouping(() => "nonsense", "status")).toBe("status");
    expect(storedGrouping(() => null, "project")).toBe("project");
    expect(storedGrouping(() => { throw new Error("no storage"); }, "project")).toBe("project");
  });
});
