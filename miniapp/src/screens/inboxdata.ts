// What the Inbox shows, apart from how it is drawn: the four views the header offers, the counts that
// stand on their tabs, and the one line a row says about an entry. The desktop and the phone read
// the same lists through these, so the tabs, the counts and the project filter cannot drift apart.

import type { Notification } from "../api";
import { useNotifications, type NoticeView } from "../notifications";

/** The header's views. "Unread" is not one of them: unread is the dot on a row and "mark unread" on an
 *  entry, so a view of it would only be a second way to see what the dot already says. */
export type InboxFilter = "all" | "needs" | "problems" | "done";

export const INBOX_FILTERS: { id: InboxFilter; view: NoticeView; label: string }[] = [
  { id: "all", view: "all", label: "inbox.filter.all" },
  { id: "needs", view: "needs_you", label: "inbox.filter.needs" },
  { id: "problems", view: "problems", label: "inbox.filter.problems" },
  { id: "done", view: "done", label: "inbox.filter.done" },
];

/** The page asks for plenty: the lists are one read each, and a tab's count is the length of its list. */
export const INBOX_LIMIT = 200;

export function viewOf(filter: InboxFilter): NoticeView {
  return INBOX_FILTERS.find((f) => f.id === filter)!.view;
}

/** The four lists at once (they share their cache keys with whichever is on screen), so each tab can
 *  say how many it holds. Undefined until a list has arrived. */
export function useInboxLists(project: string | null) {
  const all = useNotifications("all", project, INBOX_LIMIT);
  const needs = useNotifications("needs_you", project, INBOX_LIMIT);
  const problems = useNotifications("problems", project, INBOX_LIMIT);
  const done = useNotifications("done", project, INBOX_LIMIT);
  const by = { all, needs, problems, done };
  return {
    by,
    count: (filter: InboxFilter, extra = 0): number | undefined => (by[filter].data ? by[filter].data!.entries.length + (filter === "all" || filter === "needs" ? extra : 0) : undefined),
  };
}

/** A request that still waits for the operator: the list's first group. An answered one is no longer. */
export const waiting = (e: Notification) => e.needs_you && !e.resolved;

/** The first line of an entry's text: the command, the question. */
export function firstLine(body: string): string {
  return body.split("\n").find((line) => line.trim()) ?? "";
}

export type Tint = "attention" | "live" | "quiet";

/** The colour an entry carries on its dot, its icon and its kind word: orange for what waits for the
 *  operator or went wrong, cyan for finished work, grey for the rest. */
export function tintOf(e: Notification): Tint {
  if (waiting(e)) return "attention";
  if (e.level === "quiet") return "quiet";
  if (e.tone === "warning" || e.tone === "error") return "attention";
  if (e.tone === "ok") return "live";
  return "quiet";
}
