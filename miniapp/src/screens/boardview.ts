// What the board of every project shows, as values: which cards wait on the operator, and how the
// cards are grouped. Pure, so the filter and the grouping are tested without a browser.

export type NeedsReason = "question" | "approval" | "review";

type Card = { id: string; status: string; priority: number; updated_at: string; project_id?: string | null; project_name?: string | null; needs_you?: NeedsReason | null };

export type Grouping = "status" | "project";

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

/** The grouping the operator last chose, or `fallback` when nothing usable was kept. */
export function storedGrouping(read: () => string | null, fallback: Grouping): Grouping {
  try {
    const value = read();
    return value === "project" || value === "status" ? value : fallback;
  } catch { return fallback; }
}
