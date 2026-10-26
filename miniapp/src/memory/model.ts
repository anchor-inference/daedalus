// The Memory screen's arithmetic, kept apart from the views so it is tested without a page: how the
// entries are grouped, what a natural-language edit would do to an entry, how a date and the status
// pill read.

import type { MemoryEntry, MemoryOp, MemoryRecord, MemorySource, MemoryStatus, MemorySuggestion, MemoryTidy } from "../api";
import { absDate, relTimeLong } from "../format";
import { plural, t } from "../i18n";

export type GroupId = "you" | "projects" | "topics" | "recent";
export const GROUPS: GroupId[] = ["you", "projects", "topics", "recent"];

/** The You pages and People first, in that order; within every other group the latest change first. */
const YOU_ORDER = ["you/preferences", "you/profile", "topic/people"];

function groupOf(entry: MemoryEntry): GroupId {
  if (entry.section === "you" || (entry.section === "topic" && entry.slug === "people")) return "you";
  if (entry.section === "area") return "projects";
  if (entry.slug === "recent-work") return "recent";
  return "topics";
}

/** The entries as the screen lists them: your preferences, profile and people; projects; topics;
 *  recent work. A group with nothing in it is left out, so an empty memory shows no headings. */
export function groupEntries(entries: MemoryEntry[]): { id: GroupId; entries: MemoryEntry[] }[] {
  const out = new Map<GroupId, MemoryEntry[]>(GROUPS.map((g) => [g, []]));
  for (const entry of entries) out.get(groupOf(entry))!.push(entry);
  const rank = (e: MemoryEntry) => YOU_ORDER.indexOf(`${e.section}/${e.slug}`);
  out.get("you")!.sort((a, b) => rank(a) - rank(b));
  for (const id of ["projects", "topics", "recent"] as GroupId[]) out.get(id)!.sort((a, b) => b.updated_at.localeCompare(a.updated_at) || a.title.localeCompare(b.title));
  return GROUPS.map((id) => ({ id, entries: out.get(id)! })).filter((g) => g.entries.length > 0);
}

/** Entries whose title, summary or aliases hold the query, case- and accent-blind enough for a filter box. */
export function filterEntries(entries: MemoryEntry[], query: string): MemoryEntry[] {
  const q = query.trim().toLocaleLowerCase();
  if (!q) return entries;
  return entries.filter((e) => [e.title, e.summary, e.project_name ?? "", ...e.aliases].some((field) => field.toLocaleLowerCase().includes(q)));
}

export type DiffRow = { kind: "same" | "changed" | "removed" | "added"; id: string | null; text: string; before?: string; op?: number };

/** The entry after the chosen operations, as the preview draws it: every bullet with what happens to it
 *  (the index of the operation acting on it, so a row can be unticked), then the added ones. The host
 *  computes the same; it is drawn here so that unticking an operation redraws without a round trip. */
export function diffRows(records: MemoryRecord[], ops: MemoryOp[], skipped: ReadonlySet<number> = new Set()): DiffRow[] {
  const acting = new Map<string, number>();
  ops.forEach((op, i) => op.id && !skipped.has(i) && acting.set(op.id, i));
  const rows: DiffRow[] = records.map((r) => {
    const i = acting.get(r.id);
    if (i === undefined) return { kind: "same", id: r.id, text: r.text };
    const op = ops[i];
    return op.op === "remove" ? { kind: "removed", id: r.id, text: r.text, op: i } : { kind: "changed", id: r.id, text: op.text ?? r.text, before: r.text, op: i };
  });
  ops.forEach((op, i) => op.op === "add" && !skipped.has(i) && rows.push({ kind: "added", id: null, text: op.text ?? "", op: i }));
  return rows;
}

/** How many of each change the chosen operations make: the preview's one-line summary. */
export function diffCounts(rows: DiffRow[]): { added: number; removed: number; changed: number } {
  return { added: rows.filter((r) => r.kind === "added").length, removed: rows.filter((r) => r.kind === "removed").length, changed: rows.filter((r) => r.kind === "changed").length };
}

/** The operations the operator kept ticked: what Apply sends. */
export function chosenOps(ops: MemoryOp[], skipped: ReadonlySet<number>): MemoryOp[] {
  return ops.filter((_, i) => !skipped.has(i));
}

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** "Updated today", "Updated yesterday", "Updated 14 Jul" — an entry's last change, as the list shows it. */
export function updatedLabel(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  const today = new Date(now);
  if (sameDay(at, today)) return t("memory.updated.today");
  if (sameDay(at, new Date(now - 86400000))) return t("memory.updated.yesterday");
  return t("memory.updated.on", { date: absDate(at) });
}

export type Pill = { tone: "on" | "paused" | "off" | "busy" | "budget"; text: string };

/** The status pill: paused, not learning, today's budget used, learning now, or when memory last
 *  changed by itself. Until the background pass runs for the first time it says so. */
export function statusPill(status: MemoryStatus | undefined, now: number = Date.now()): Pill | null {
  if (!status) return null;
  if (status.paused) return { tone: "paused", text: t("memory.status.paused") };
  if (!status.generate) return { tone: "off", text: t("memory.status.manual") };
  if (status.budget_usd > 0 && status.spent_today_usd >= status.budget_usd) return { tone: "budget", text: t("memory.status.budget") };
  if (status.learning > 0) return { tone: "busy", text: t("memory.status.learning", { n: status.learning }) };
  if (status.last_synthesis_at) return { tone: "on", text: t("memory.status.updated", { when: relTimeLong(status.last_synthesis_at, now) }) };
  return { tone: "on", text: t("memory.status.waiting") };
}

export type LearningLine = { tone: "" | "warn" | "busy"; text: string };

/** Settings → Memory's account of the background pass: the model it runs on (and why, when it is not the
 *  one chosen), what the last pass did, today's spend, why it is not running. Empty lines are left out. */
export function learningLines(status: MemoryStatus | undefined, now: number = Date.now()): LearningLine[] {
  if (!status) return [];
  const out: LearningLine[] = [];
  const money = (n: number) => `$${n.toFixed(n > 0 && n < 0.01 ? 4 : 2)}`;
  if (status.blocked) out.push({ tone: "warn", text: t(`settings.memory.learning.blocked.${status.blocked}`) });
  if (status.running || status.learning > 0) out.push({ tone: "busy", text: t("settings.memory.learning.running") });
  if (status.model_reason === "none") out.push({ tone: "warn", text: t("settings.memory.learning.model.none") });
  else if (status.model) {
    out.push({ tone: "", text: t("settings.memory.learning.model", { model: status.model }) });
    if (status.model_reason) out.push({ tone: "warn", text: t(`settings.memory.learning.model.${status.model_reason}`, { model: status.model, missing: status.model_missing ?? "" }) });
  }
  const r = status.report;
  if (!r) out.push({ tone: "", text: t("settings.memory.learning.never") });
  else {
    out.push({ tone: "", text: t("settings.memory.learning.last", { when: relTimeLong(r.at, now), chats: r.chats }) });
    const dropped = Object.values(r.dropped ?? {}).reduce((a, b) => a + b, 0);
    out.push({ tone: "", text: t("settings.memory.learning.counts", { added: r.added, updated: r.updated, merged: r.merged, held: r.held, dropped }) });
    if (r.archived) out.push({ tone: "", text: plural("settings.memory.learning.archived", r.archived) });
    if (r.consolidated) out.push({ tone: "", text: plural("settings.memory.learning.consolidated", r.consolidated) });
    if (r.revived) out.push({ tone: "", text: plural("settings.memory.learning.revived", r.revived) });
    if (r.stopped && r.stopped !== "paused" && r.stopped !== "off" && r.stopped !== "no_budget") out.push({ tone: "warn", text: t(`settings.memory.learning.stopped.${r.stopped === "budget" ? "budget" : "no_model"}`) });
  }
  out.push({ tone: "", text: t("settings.memory.learning.spent", { spent: money(status.spent_today_usd), budget: money(status.budget_usd) }) });
  if (status.failing_chats) out.push({ tone: "warn", text: t("settings.memory.learning.failing", { n: status.failing_chats }) });
  return out;
}

/** What a tidy-up would do, one line per kind of change it makes; empty when there is nothing to do. */
export function tidyLines(found: MemoryTidy): string[] {
  const out: string[] = [];
  if (found.merged) out.push(plural("settings.memory.tidynow.merged", found.merged));
  if (found.folded) out.push(plural("settings.memory.tidynow.folded", found.folded));
  if (found.probes) out.push(plural("settings.memory.tidynow.probes", found.probes));
  if (found.closed) out.push(plural("settings.memory.tidynow.closed", found.closed));
  if (found.restored) out.push(plural("settings.memory.tidynow.restored", found.restored));
  return out;
}

/** Where a bullet came from, in a few words: the chat by its title, or what taught it when no chat did. */
export function sourceLabel(source: MemorySource): string {
  if (source.session_id) return source.session_title || t("memory.source.gone");
  return t(`memory.source.${["operator", "import", "migration", "synthesis", "remember"].includes(source.origin) ? source.origin : "other"}`);
}

/** The bullets shown in the entry (newest first) and the archived ones folded under "Older". */
export function splitArchived<T extends MemoryRecord>(records: T[]): { live: T[]; older: T[] } {
  return { live: records.filter((r) => !r.archived), older: records.filter((r) => r.archived) };
}

export type ReviewBy = "entry" | "pass";
export const REVIEW_BY: ReviewBy[] = ["entry", "pass"];

/** One group of the review: an entry's suggestions or one pass's, with the ids "accept all" and
 *  "dismiss all" act on. */
export type SuggestionGroup = { key: string; title: string; at: string | null; sessionId: string | null; items: MemorySuggestion[]; bulk: number[] };

/** Whether a suggestion is the background pass's own — the review's business. The tidying's merges each
 *  change the records they name and stay in All facts, decided one at a time. */
export const isLearned = (s: MemorySuggestion) => s.kind === "revision" || s.kind === "addition";

/** The background pass's suggestions grouped by the entry they are for (the busiest first) or by the
 *  pass that filed them (the latest first). After a cold start the pass held dozens; deciding them an
 *  entry or a chat at a time is what makes the review doable. */
export function groupSuggestions(list: MemorySuggestion[], by: ReviewBy): SuggestionGroup[] {
  const groups = new Map<string, SuggestionGroup>();
  for (const s of list.filter(isLearned)) {
    const key = by === "entry" ? (s.entry_id ? `entry:${s.entry_id}` : `new:${s.entry_title ?? ""}`) : s.pass ? `pass:${s.pass}` : `chat:${s.session_id ?? ""}`;
    const title = by === "entry" ? s.entry_title || t("memory.review.noentry") : s.session_title || t("memory.source.gone");
    const group = groups.get(key) ?? { key, title, at: null, sessionId: by === "pass" ? s.session_id ?? null : null, items: [], bulk: [] };
    group.items.push(s);
    group.bulk.push(s.id);
    const at = s.pass_at || s.created_at;
    if (at && (!group.at || at > group.at)) group.at = at;
    groups.set(key, group);
  }
  return [...groups.values()].sort((a, b) => (by === "entry" ? b.items.length - a.items.length : (b.at ?? "").localeCompare(a.at ?? "")) || a.title.localeCompare(b.title));
}

/** The background pass's suggestions, of one entry when the review is opened from that entry. */
export function suggestionsFor(list: MemorySuggestion[], entryId: string | null | undefined): MemorySuggestion[] {
  return list.filter((s) => isLearned(s) && (!entryId || s.entry_id === entryId));
}
