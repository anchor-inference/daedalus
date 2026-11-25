// What the board of every project shows, as values: which cards wait on the operator, how the cards
// are grouped, and which part of the board is in view. Pure, so the filter, the grouping and the scope
// are tested without a browser.

import type { NeedsReason } from "../board/tasks";

type Card = { id: string; title?: string; status: string; priority: number; updated_at: string; project_id?: string | null; project_name?: string | null; needs_you?: NeedsReason | null; origin_session_id?: string | null };

export type Grouping = "status" | "project";

/** Which part of the board is in view: every card, one project's, or the ones one chat created. */
export type Scope = { kind: "all" } | { kind: "project"; id: string } | { kind: "chat"; id: string };

export type Group<T> = { key: string; projectId: string | null; name: string; items: T[] };

/** The order a card's status reads in inside a project's group: the work in the order it flows. */
const STATUS_ORDER = ["review", "doing", "blocked", "todo", "done", "dropped"];

/** Whether a card waits on the operator. A host older than the field sends none, and then none does:
 *  guessing from the status alone would put every card in review there and none of the questions. */
export const needsYou = (card: Card): boolean => Boolean(card.needs_you);

/** The cards a filter leaves; `needs` keeps only the ones that wait on the operator. */
export function filtered<T extends Card>(cards: T[], needs: boolean): T[] {
  return needs ? cards.filter(needsYou) : cards;
}

/** The cards whose title or id holds every word typed, in any order and any case. */
export function searched<T extends Card>(cards: T[], query: string): T[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return cards;
  return cards.filter((card) => {
    const text = `${card.title ?? ""} ${card.id}`.toLowerCase();
    return words.every((word) => text.includes(word));
  });
}

const byPriority = (a: Card, b: Card) => a.priority - b.priority || Date.parse(b.updated_at) - Date.parse(a.updated_at);

/** The cards by project, each project's in status order and then by priority; projects by name, the
 *  tasks of no project last. */
export function byProject<T extends Card>(cards: T[]): Group<T>[] {
  const groups = new Map<string, Group<T>>();
  for (const card of cards) {
    const key = card.project_id ?? "";
    if (!groups.has(key)) groups.set(key, { key: key || "none", projectId: card.project_id ?? null, name: card.project_name ?? card.project_id ?? "", items: [] });
    groups.get(key)!.items.push(card);
  }
  const rank = (status: string) => { const at = STATUS_ORDER.indexOf(status); return at < 0 ? STATUS_ORDER.length : at; };
  for (const group of groups.values()) group.items.sort((a, b) => rank(a.status) - rank(b.status) || byPriority(a, b));
  return [...groups.values()].sort((a, b) => Number(!a.projectId) - Number(!b.projectId) || a.name.localeCompare(b.name));
}

/** A column's cards in lanes: by project when every card is in view, by the chat that created them
 *  in one project's, and one lane without a label in a chat's own. Lanes keep the order of their
 *  first card, so the most urgent lane stands first; the cards of no project or no chat go last. */
export function lanes<T extends Card>(cards: T[], scope: Scope["kind"], chatTitle: (id: string) => string | undefined): { key: string; label: string | null; chat: string | null; items: T[] }[] {
  if (scope === "chat") return cards.length ? [{ key: "chat", label: null, chat: null, items: cards }] : [];
  const out = new Map<string, { key: string; label: string | null; chat: string | null; items: T[] }>();
  for (const card of cards) {
    const key = scope === "project" ? card.origin_session_id ?? "" : card.project_id ?? "";
    if (!out.has(key)) {
      const label = scope === "project" ? (key ? chatTitle(key) ?? "" : "") : (card.project_name ?? card.project_id ?? "");
      out.set(key, { key: key || "none", label, chat: scope === "project" && key ? key : null, items: [] });
    }
    out.get(key)!.items.push(card);
  }
  return [...out.values()].sort((a, b) => Number(a.key === "none") - Number(b.key === "none"));
}

/** The scope a board address asks for: `?scope=chat&session=<id>`, `?scope=project&project=<id>`, or
 *  the shell's project lens when the address names none, and every card otherwise. */
export function scopeFrom(query: URLSearchParams, lens: string | null): Scope {
  const kind = query.get("scope");
  const session = query.get("session");
  const project = query.get("project");
  if (kind === "chat" && session) return { kind: "chat", id: session };
  if (kind === "project" && project) return { kind: "project", id: project };
  if (kind === "all") return { kind: "all" };
  if (lens) return { kind: "project", id: lens };
  return { kind: "all" };
}

/** The address query for a scope, as `scopeFrom` reads it back. */
export function scopeQuery(scope: Scope): Record<string, string | null> {
  if (scope.kind === "chat") return { scope: "chat", session: scope.id };
  if (scope.kind === "project") return { scope: "project", project: scope.id };
  return { scope: "all" };
}

/** The listing that serves a scope: always with the finished cards, which the filter bar counts. */
export function scopeKey(scope: Scope): string {
  const base = "/api/board?include_done=1";
  if (scope.kind === "chat") return `${base}&session=${encodeURIComponent(scope.id)}`;
  if (scope.kind === "project") return `${base}&project=${encodeURIComponent(scope.id)}`;
  return base;
}

/** The grouping the operator last chose, or `fallback` when nothing usable was kept. */
export function storedGrouping(read: () => string | null, fallback: Grouping): Grouping {
  try {
    const value = read();
    return value === "project" || value === "status" ? value : fallback;
  } catch { return fallback; }
}
