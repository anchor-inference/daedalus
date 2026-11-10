// What a canvas's history list says about each revision: who saved it, what changed in words a person
// reads, and under which day it sits. The counts come from the host, which compares the documents when
// it records a revision; node ids never reach the reader, they mean nothing to them.

import { locale, plural, t } from "../i18n";

export type ChangeSummary = {
  created?: boolean;
  oldest?: boolean;
  added?: Record<string, number>;
  removed?: Record<string, number>;
  changed?: number;
  renamed?: boolean;
  restored_from?: number | null;
};

export type RevisionItem = {
  version: number;
  title: string;
  saved_at: string;
  started_at?: string | null;
  source: "operator" | "agent";
  kind: "create" | "edit" | "restore" | "import";
  restored_from?: number | null;
  summary: ChangeSummary;
};

const CATEGORIES = ["card", "group", "link", "file", "edge"] as const;

/** "+3 cards, 1 edge removed, 2 changed, renamed", or what the revision was when it is not an edit. */
export function changeSummary(summary: ChangeSummary, kind: RevisionItem["kind"] = "edit"): string {
  if (kind === "create" || summary.created) return t("canvas.change.created");
  if (kind === "import") return t("canvas.change.imported");
  if (summary.oldest) return t("canvas.change.oldest");
  const parts: string[] = [];
  // A version number means nothing to the reader, so a restore says what it did, and the counts
  // after it say how far the board moved.
  if (kind === "restore") parts.push(t("canvas.change.restored"));
  for (const category of CATEGORIES) {
    const n = summary.added?.[category] ?? 0;
    if (n) parts.push(`+${plural(`canvas.change.${category}`, n)}`);
  }
  for (const category of CATEGORIES) {
    const n = summary.removed?.[category] ?? 0;
    if (n) parts.push(t("canvas.change.removed", { what: plural(`canvas.change.${category}`, n) }));
  }
  if (summary.changed) parts.push(plural("canvas.change.changed", summary.changed));
  if (summary.renamed) parts.push(t("canvas.change.renamed"));
  if (!parts.length) return t("canvas.change.none");
  return parts.join(", ");
}

function dayKey(at: Date): string {
  return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-${String(at.getDate()).padStart(2, "0")}`;
}

/** The heading a day's revisions sit under: Today, Yesterday, or the date (with the year only when it is not this one). */
export function dayLabel(iso: string, now = new Date()): string {
  const at = new Date(iso);
  const today = dayKey(now);
  const yesterday = dayKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1));
  const key = dayKey(at);
  if (key === today) return t("canvas.day.today");
  if (key === yesterday) return t("canvas.day.yesterday");
  return at.toLocaleDateString(locale(), { weekday: "short", day: "numeric", month: "long", ...(at.getFullYear() === now.getFullYear() ? {} : { year: "numeric" }) });
}

/** Revisions, newest first, grouped under their day in the reader's time zone. */
export function groupByDay<T extends { saved_at: string }>(items: readonly T[], now = new Date()): { key: string; label: string; items: T[] }[] {
  const groups: { key: string; label: string; items: T[] }[] = [];
  const sorted = [...items].sort((a, b) => Date.parse(b.saved_at) - Date.parse(a.saved_at));
  for (const item of sorted) {
    const key = dayKey(new Date(item.saved_at));
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.items.push(item);
    else groups.push({ key, label: dayLabel(item.saved_at, now), items: [item] });
  }
  return groups;
}

/** The clock time a revision was saved, "14:05", for a row under its day heading. */
export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
}
