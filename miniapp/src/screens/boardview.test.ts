import { describe, expect, it } from "vitest";
import { byProject, filtered, lanes, scopeFrom, scopeKey, scopeQuery, searched, storedGrouping } from "./boardview";

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

  it("lanes a column by project for every card, by the chat that made it in one project, and not at all in one chat", () => {
    const cards = [
      card("a", { project_id: "p", project_name: "Bakery", origin_session_id: "s1" }),
      card("b", { origin_session_id: null }),
      card("c", { project_id: "p", project_name: "Bakery", origin_session_id: "s2" }),
    ];
    const title = (id: string) => ({ s1: "Menu", s2: "Photos" } as Record<string, string>)[id];
    expect(lanes(cards, "all", title).map((l) => [l.label, l.items.map((c) => c.id)])).toEqual([["Bakery", ["a", "c"]], ["", ["b"]]]);
    expect(lanes(cards, "project", title).map((l) => [l.label, l.chat])).toEqual([["Menu", "s1"], ["Photos", "s2"], ["", null]]);
    expect(lanes(cards, "chat", title)).toEqual([{ key: "chat", label: null, chat: null, items: cards }]);
    expect(lanes([], "chat", title)).toEqual([]);
  });

  it("reads the scope from the address, falling back to the shell's project lens, and writes it back", () => {
    const q = (s: string) => new URLSearchParams(s);
    expect(scopeFrom(q("scope=chat&session=s1"), null)).toEqual({ kind: "chat", id: "s1" });
    expect(scopeFrom(q("scope=project&project=p"), null)).toEqual({ kind: "project", id: "p" });
    expect(scopeFrom(q("scope=chat"), "p")).toEqual({ kind: "project", id: "p" });
    expect(scopeFrom(q("scope=all"), "p")).toEqual({ kind: "all" });
    expect(scopeFrom(q(""), null)).toEqual({ kind: "all" });
    expect(scopeQuery({ kind: "chat", id: "s1" })).toEqual({ scope: "chat", session: "s1" });
    expect(scopeKey({ kind: "chat", id: "s 1" })).toBe("/api/board?include_done=1&session=s%201");
    expect(scopeKey({ kind: "all" })).toBe("/api/board?include_done=1");
  });

  it("finds cards by every word of the search, in its title or its id", () => {
    const cards = [card("7f2a1c", { title: "Order form posts" }), card("3b9e44", { title: "Opening hours" })];
    expect(searched(cards, "form ORDER").map((c) => c.id)).toEqual(["7f2a1c"]);
    expect(searched(cards, "3b9")).toHaveLength(1);
    expect(searched(cards, "  ")).toHaveLength(2);
  });
});
