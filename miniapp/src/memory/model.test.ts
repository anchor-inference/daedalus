// @vitest-environment jsdom
// The Memory screen's arithmetic: how entries are grouped and filtered, what a proposed edit does to
// an entry and what unticking one of its changes leaves, how a date and the status pill read, where a
// bullet came from, which Remember calls of a turn saved something, and how an import is grouped.

import { beforeEach, describe, expect, it } from "vitest";
import type { MemoryEntry, MemoryImportItem, MemoryOp, MemoryRecord, MemoryStatus, MemorySuggestion, MemoryTidy } from "../api";
import { setLang } from "../i18n";
import type { Activity } from "../turns";
import { chosenOps, diffCounts, diffRows, filterEntries, groupEntries, groupSuggestions, learningLines, sourceLabel, splitArchived, statusPill, suggestionsFor, tidyLines, updatedLabel } from "./model";
import { savedMemories } from "./savedchip";
import { byTarget, importable } from "./transfer";

const entry = (id: string, section: MemoryEntry["section"], slug: string, updated: string, extra: Partial<MemoryEntry> = {}): MemoryEntry => ({
  id, section, slug, title: slug, summary: "", project_id: null, project_name: null, aliases: [], sensitive: [], edited_by_operator: false, version: 1,
  created_at: updated, updated_at: updated, count: 1, ...extra,
});
const record = (id: string, text: string, extra: Partial<MemoryRecord> = {}): MemoryRecord => ({
  id, scope: "global", scope_key: "", category: "fact", topics: [], text, salience: 0, version: 1, created_at: null, updated_at: null, last_accessed_at: null, ...extra,
});
const status = (over: Partial<MemoryStatus> = {}): MemoryStatus => ({ generate: true, paused: false, search_chats: true, synthesis_preset: "", budget_usd: 0.5, spent_today_usd: 0, last_synthesis_at: null, learning: 0, ...over });

beforeEach(() => setLang("en"));

describe("grouping", () => {
  it("puts your pages and people first, then projects, topics and recent work, newest first within each", () => {
    const groups = groupEntries([
      entry("t1", "topic", "garden", "2026-10-01"),
      entry("r", "topic", "recent-work", "2026-10-20"),
      entry("p", "you", "profile", "2026-09-01"),
      entry("a1", "area", "bakery", "2026-10-02", { project_id: "p1", project_name: "Bakery" }),
      entry("people", "topic", "people", "2026-10-25"),
      entry("pref", "you", "preferences", "2026-08-01"),
      entry("t2", "topic", "cooking", "2026-10-15"),
      entry("a2", "area", "old-site", "2026-10-10"),
    ]);
    expect(groups.map((g) => [g.id, g.entries.map((e) => e.id)])).toEqual([
      ["you", ["pref", "p", "people"]],
      ["projects", ["a2", "a1"]],
      ["topics", ["t2", "t1"]],
      ["recent", ["r"]],
    ]);
    expect(groupEntries([])).toEqual([]);
  });

  it("filters by title, summary, project and aliases", () => {
    const list = [entry("a", "topic", "garden", "x", { title: "Garden", aliases: ["balcony"] }), entry("b", "area", "site", "x", { title: "Site", project_name: "Bakery" })];
    expect(filterEntries(list, "BALC").map((e) => e.id)).toEqual(["a"]);
    expect(filterEntries(list, "bakery").map((e) => e.id)).toEqual(["b"]);
    expect(filterEntries(list, "  ")).toHaveLength(2);
  });

  it("folds archived bullets away from the live ones", () => {
    const { live, older } = splitArchived([record("a", "x"), record("b", "y", { archived: true })]);
    expect([live.map((r) => r.id), older.map((r) => r.id)]).toEqual([["a"], ["b"]]);
  });
});

describe("a proposed edit", () => {
  const records = [record("m1", "Someone lives in Porto"), record("m2", "Someone bakes bread"), record("m3", "Someone has a cat")];
  const ops: MemoryOp[] = [{ op: "change", id: "m1", text: "Someone lives in Lisbon" }, { op: "remove", id: "m3", text: null }, { op: "add", id: null, text: "Someone moved in 2026" }];

  it("draws every bullet with what happens to it and the added ones last", () => {
    const rows = diffRows(records, ops);
    expect(rows.map((r) => [r.kind, r.text, r.before ?? null, r.op ?? null])).toEqual([
      ["changed", "Someone lives in Lisbon", "Someone lives in Porto", 0],
      ["same", "Someone bakes bread", null, null],
      ["removed", "Someone has a cat", null, 1],
      ["added", "Someone moved in 2026", null, 2],
    ]);
    expect(diffCounts(rows)).toEqual({ added: 1, removed: 1, changed: 1 });
  });

  it("leaves an unticked change out of the drawing and out of what Apply sends", () => {
    const skipped = new Set([1]);
    expect(diffRows(records, ops, skipped).find((r) => r.id === "m3")?.kind).toBe("same");
    expect(chosenOps(ops, skipped)).toEqual([ops[0], ops[2]]);
  });
});

describe("dates and the status pill", () => {
  const now = new Date(2026, 9, 26, 15, 0).getTime();

  it("says today, yesterday, or the date", () => {
    expect(updatedLabel(new Date(2026, 9, 26, 9, 0).toISOString(), now)).toBe("Updated today");
    expect(updatedLabel(new Date(2026, 9, 25, 23, 0).toISOString(), now)).toBe("Updated yesterday");
    expect(updatedLabel(new Date(2026, 6, 14, 12, 0).toISOString(), now)).toMatch(/^Updated (14 Jul|Jul 14)/);
    expect(updatedLabel(null, now)).toBe("");
    setLang("ru");
    expect(updatedLabel(new Date(2026, 9, 26, 9, 0).toISOString(), now)).toBe("Обновлено сегодня");
  });

  it("reads paused before anything, then not learning, the budget, learning now, and the last pass", () => {
    expect(statusPill(undefined)).toBeNull();
    expect(statusPill(status({ paused: true, learning: 2 }))?.tone).toBe("paused");
    expect(statusPill(status({ generate: false }))?.tone).toBe("off");
    expect(statusPill(status({ spent_today_usd: 0.5 }))?.tone).toBe("budget");
    expect(statusPill(status({ budget_usd: 0, spent_today_usd: 3 }))?.tone).toBe("on");
    expect(statusPill(status({ learning: 2 }))).toEqual({ tone: "busy", text: "Learning from chats (2)…" });
    expect(statusPill(status({ last_synthesis_at: new Date(now - 12 * 60000).toISOString() }), now)?.text).toBe("Updated 12 minutes ago");
    expect(statusPill(status())?.text).toBe("On · learns from your chats");
  });
});

describe("learning from chats", () => {
  const report = { at: "", trigger: "manual", model: "m", chats: 3, parts: 4, calls: 4, cost_usd: 0.01, added: 5, merged: 2, updated: 1, retired: 0, new_entries: 1, summaries: 0, held: 2, dropped: { sensitive: 1, no_quote: 2 }, stopped: "" };
  it("names the model, what the last pass did and today's spend", () => {
    const now = Date.parse("2026-10-26T12:00:00Z");
    const lines = learningLines(status({ model: "cheap.model", spent_today_usd: 0.004, report: { ...report, at: new Date(now - 5 * 60000).toISOString() } }), now).map((l) => l.text);
    expect(lines).toEqual([
      "Runs on cheap.model",
      "Last pass 5 minutes ago · chats read: 3",
      "Added 5 · updated 1 · confirmed 2 · waiting for you 2 · refused 3",
      "Spent today: $0.0040 of $0.50",
    ]);
  });
  it("says why it does not run and when the model is a stand-in", () => {
    const lines = learningLines(status({ blocked: "paused", model: "x", model_reason: "chosen_missing", model_missing: "gone", report: null }));
    expect(lines[0]).toEqual({ tone: "warn", text: "Paused: nothing is learned until you resume memory." });
    expect(lines.map((l) => l.text)).toContain("The chosen model gone is no longer configured; x runs instead.");
    expect(lines.map((l) => l.text)).toContain("No pass has run yet.");
    expect(learningLines(status({ model_reason: "none", report: { ...report, stopped: "budget" } })).map((l) => l.tone)).toContain("warn");
    expect(learningLines(undefined)).toEqual([]);
  });
});

describe("sources and saves", () => {
  it("names the chat while it exists and what taught the bullet when no chat did", () => {
    const base = { from_seq: null, to_seq: null, run_id: null, quote: "", at: "2026-10-01" };
    expect(sourceLabel({ ...base, origin: "remember", session_id: "s1", session_title: "Kitchen plans" })).toBe("Kitchen plans");
    expect(sourceLabel({ ...base, origin: "remember", session_id: "s1", session_title: null })).toBe("a deleted chat");
    expect(sourceLabel({ ...base, origin: "operator", session_id: null, session_title: null })).toBe("you wrote it");
    expect(sourceLabel({ ...base, origin: "weird", session_id: null, session_title: null })).toBe("elsewhere");
  });

  it("counts a turn's Remember calls that created or merged a note, once each", () => {
    const tool = (id: string, result: string | undefined, over: Partial<{ error: boolean; running: boolean; name: string }> = {}): Activity => ({ kind: "tool", id, name: over.name ?? "Remember", args: {}, result, error: over.error, running: over.running ?? false });
    expect(savedMemories([
      tool("1", "created memory mem-a (scope=global; category=fact; topics=-)"),
      tool("2", "merged memory mem-b (scope=global; category=fact; topics=-)"),
      tool("3", "skipped memory mem-c (scope=global)"),
      tool("4", "Not saved: sensitive", { error: true }),
      tool("5", undefined, { running: true }),
      tool("6", "created memory mem-a (scope=global)"),
      tool("7", "created memory mem-z", { name: "Recall" }),
    ])).toEqual(["mem-a", "mem-b"]);
  });
});

describe("the import preview", () => {
  const item = (key: number, title: string, over: Partial<MemoryImportItem> = {}): MemoryImportItem => ({
    key, text: `note ${key}`, category: "fact", topics: [], section: "topic", slug: title.toLowerCase(), title, project_id: null, date: null,
    sensitive: [], injection: [], duplicate: false, forgotten: false, checked: true, ...over,
  });

  it("groups the notes by the entry they would land in and never lets an instruction be ticked", () => {
    const items = [item(0, "Profile"), item(1, "Recent work"), item(2, "Profile", { injection: ["an instruction to ignore earlier instructions"], checked: false })];
    expect(byTarget(items).map(([title, list]) => [title, list.map((i) => i.key)])).toEqual([["Profile", [0, 2]], ["Recent work", [1]]]);
    expect(items.map(importable)).toEqual([true, true, false]);
  });
});

describe("the review of held notes", () => {
  const held = (id: number, over: Partial<MemorySuggestion> = {}): MemorySuggestion => ({
    id, kind: "addition", memory_ids: [], keep_id: "", text: `note ${id}`, score: 0, reason: "", created_at: "2026-10-20T10:00:00Z", action: "add",
    entry_id: "e-a", entry_title: "Sprout", session_id: "s1", session_title: "Planner chat", pass: "memory-s1-aa", pass_at: "2026-10-20T10:00:00Z", ...over,
  });
  const list = [
    held(1),
    held(2, { entry_id: "e-b", entry_title: "Garden", pass: "memory-s2-bb", session_id: "s2", session_title: "Garden chat", pass_at: "2026-10-21T09:00:00Z" }),
    held(3),
    held(4, { kind: "revision", action: "update", memory_ids: ["r1"], entry_id: "e-b", entry_title: "Garden" }),
    held(5, { kind: "duplicate", entry_id: null, entry_title: null, pass: undefined }),
    held(6, { entry_id: "", entry_title: "Hosting", pass: "memory-s2-bb", session_id: "s2", session_title: null, pass_at: "2026-10-21T09:00:00Z" }),
  ];

  it("groups by entry, busiest first, with the ids accept all and dismiss all send; the tidying's merges stay out", () => {
    const groups = groupSuggestions(list, "entry");
    expect(groups.map((g) => [g.title, g.bulk])).toEqual([["Garden", [2, 4]], ["Sprout", [1, 3]], ["Hosting", [6]]]);
  });

  it("groups by the pass that filed them, the latest first, named by the chat or as gone", () => {
    const groups = groupSuggestions(list, "pass");
    expect(groups.map((g) => [g.title, g.bulk, g.sessionId])).toEqual([["Garden chat", [2, 6], "s2"], ["Planner chat", [1, 3, 4], "s1"]]);
  });

  it("narrows to one entry when opened from it", () => {
    expect(suggestionsFor(list, "e-b").map((s) => s.id)).toEqual([2, 4]);
    expect(suggestionsFor(list, null).map((s) => s.id)).toEqual([1, 2, 3, 4, 6]);
  });

  it("names the notes archived to make room in the last pass's counts", () => {
    const report = { at: new Date().toISOString(), trigger: "manual", model: "m", chats: 1, parts: 1, calls: 1, cost_usd: 0, added: 2, merged: 0, updated: 0, retired: 0, new_entries: 0, summaries: 1, held: 0, archived: 3, dropped: {}, stopped: "" };
    expect(learningLines(status({ report })).map((l) => l.text).join("\n")).toContain("3 older notes archived to make room in full entries");
  });

  it("names the notes a pass folded together and brought back from the archive", () => {
    const report = { at: new Date().toISOString(), trigger: "manual", model: "m", chats: 1, parts: 1, calls: 1, cost_usd: 0, added: 0, merged: 4, updated: 0, retired: 0, new_entries: 0, summaries: 0, held: 0, consolidated: 5, revived: 1, dropped: {}, stopped: "" };
    const text = learningLines(status({ report })).map((l) => l.text).join("\n");
    expect(text).toContain("5 notes folded into others while making room in full entries");
    expect(text).toContain("1 archived note brought back: a chat said it again");
  });
});

describe("the tidy-up", () => {
  const found = (over: Partial<MemoryTidy> = {}): MemoryTidy => ({ dry_run: true, merged: 0, merge_groups: 0, folded: 0, probes: 0, closed: 0, restored: 0, emptied_entries: 0, probe_list: [], history_id: null, ...over });

  it("says nothing when there is nothing to do", () => {
    expect(tidyLines(found())).toEqual([]);
  });

  it("lists each kind of change in both languages", () => {
    const plan = found({ merged: 2, folded: 1, probes: 3, closed: 5, restored: 21 });
    expect(tidyLines(plan)).toEqual([
      "2 reworded notes merged into their twins", "1 archived note folded into a live one", "3 notes left by early tests deleted",
      "5 repeated suggestions closed", "21 archived notes brought back",
    ]);
    setLang("ru");
    expect(tidyLines(plan)[4]).toBe("21 архивная заметка возвращена");
    expect(tidyLines(plan)[3]).toBe("5 повторяющихся предложений закрыты");
    setLang("en");
  });
});
