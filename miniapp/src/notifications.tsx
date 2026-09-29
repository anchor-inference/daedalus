// The notification centre's parts, shared by the bell's popover, the toasts and the Inbox screen:
// the reads, the row, the buttons that answer a request from where it is shown, "Needs you", and
// the list with its headings.
//
// The host renders every title and every button label in the operator's language, so nothing here
// translates what a notification says; only the frame around it (headings, outcomes, the category
// word) comes from the dictionary.

import { useState, type ReactNode } from "react";
import { ApiError, api, type Notification, type NotificationPage, type NotificationSummary, type Project, type SessionList } from "./api";
import { absTime, relTime } from "./format";
import { Icon, type IconName } from "./icons";
import { canonical, navigate, pathFor, sessionPath } from "./router";
import { invalidate, peek, prime, useQuery } from "./store";
import { SUMMARY_KEY, streamUp, useStreamUp } from "./events";
import { toast } from "./dialogs";
import { t } from "./i18n";
import { errorText } from "./ui";

/** The host's categories, as `daedalus/extensions/notifications.py` names them; each has a word. */
export const NOTICE_CATEGORIES = ["run_finished", "question", "permission", "run_failed", "staff_turn", "staff_review", "orchestrator_report", "agent_notify", "reminder", "spend", "system"] as const;
/** How a request can end. Anything else the host says is shown as it said it. */
export const NOTICE_RESOLUTIONS = ["allow", "deny", "answered", "expired", "withdrawn"] as const;

export type NoticeView = "all" | "unseen" | "problems" | "needs_you";

/** Every list key starts with this, so one invalidation reaches every list on screen. */
export const LIST_PREFIX = "/api/notifications?";

export function listKey(view: NoticeView, project?: string | null, limit = 100): string {
  return `${LIST_PREFIX}view=${view}&limit=${limit}${project ? `&project=${encodeURIComponent(project)}` : ""}`;
}

/** The counts behind every badge. Primed by the event stream; polled only while the stream is down. */
export function useSummary(enabled = true): NotificationSummary {
  const live = useStreamUp();
  const { data } = useQuery<NotificationSummary>(enabled ? SUMMARY_KEY : null, { pollMs: live ? 0 : 20000, staleMs: 5000 });
  return data ?? { unseen: 0, needs_you: 0 };
}

/** One view of the list, optionally one project's. Re-read when a `notify` event invalidates it. */
export function useNotifications(view: NoticeView, project?: string | null, limit = 100) {
  const live = useStreamUp();
  return useQuery<NotificationPage>(listKey(view, project, limit), { pollMs: live ? 0 : 15000, staleMs: 5000 });
}

const KIND_ICON: Record<string, IconName> = { rebuild: "wrench", run_cap: "stop", schedule: "clock", schedule_run: "clock", service: "globe", loop: "loop", loop_paused: "pause", heartbeat: "dot", inbound: "inbox", board_stale: "board", webhook_failed: "globe", learning_digest: "bulb", boot_guard: "wrench", change_proposal: "changes", terminal: "terminal", balance: "chart", budget: "chart", browser: "globe", browser_needs_you: "globe" };
const CATEGORY_ICON: Record<string, IconName> = { run_finished: "check", question: "question", permission: "key", run_failed: "stop", staff_turn: "bots", staff_review: "board", orchestrator_report: "spawn", agent_notify: "bolt", reminder: "clock", spend: "chart", system: "inbox" };

/** The kind names the producer's own sub-kind when it has a better picture than the category's. */
export function noticeIcon(entry: Pick<Notification, "kind" | "category">): IconName {
  return KIND_ICON[entry.kind] ?? CATEGORY_ICON[entry.category] ?? "inbox";
}

/** The icon's colour: the entry's tone, except that a quiet record stays grey whatever it says. */
export function toneClass(entry: Pick<Notification, "level" | "tone">): string {
  return entry.level === "quiet" && entry.tone === "info" ? "quiet" : entry.tone;
}

export function categoryLabel(category: string): string {
  return (NOTICE_CATEGORIES as readonly string[]).includes(category) ? t(`notice.cat.${category}`) : category.replace(/_/g, " ");
}

export function resolutionLabel(resolution: string): string {
  return (NOTICE_RESOLUTIONS as readonly string[]).includes(resolution) ? t(`notice.resolution.${resolution}`) : resolution;
}

/** The second line of a row: what kind of thing it is, the project and the agent it came from. */
export function noticeLine(entry: Notification, projects: Map<string, string>): string {
  const parts = [categoryLabel(entry.category)];
  const project = entry.project_id ? projects.get(entry.project_id) : undefined;
  // Every line names where the entry came from, the whole app when no project: a bare "System" or
  // "Spending" among "Finished · Bakery site · …" lines read as a line that had failed to fill in.
  parts.push(project ?? t("notice.anywhere"));
  const session = entry.session_id ? peek<SessionList>("/api/sessions")?.sessions.find((s) => s.id === entry.session_id) : undefined;
  // An orchestrator's session is titled "Orchestrator · <project>", so its whole title repeated the
  // project already named and the line read "Anchor Inference · Orchestrator · Anchor Inference".
  const own = (session?.title ?? "").split(" · ").filter((part) => part !== project).join(" · ");
  if (own) parts.push(own);
  return parts.join(" · ");
}

export function projectNames(projects: Project[] | undefined): Map<string, string> {
  return new Map((projects ?? []).map((p) => [p.id, p.name]));
}

// ── grouping ─────────────────────────────────────────────────────────────────────────────

export type Group = { key: string; label: string; entries: Notification[] };

function sameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** Today and Earlier, in the reader's own day; the list arrives newest first and keeps that order. */
export function byDay(entries: Notification[], now = new Date()): Group[] {
  const today: Notification[] = [];
  const earlier: Notification[] = [];
  for (const entry of entries) (sameDay(new Date(entry.updated_at), now) ? today : earlier).push(entry);
  const groups: Group[] = [];
  if (today.length) groups.push({ key: "today", label: t("centre.today"), entries: today });
  if (earlier.length) groups.push({ key: "earlier", label: t("centre.earlier"), entries: earlier });
  return groups;
}

/** One group per project, named; the project with the newest entry first, and the rest after them. */
export function byProject(entries: Notification[], names: Map<string, string>): Group[] {
  const groups = new Map<string, Group>();
  let outside: Group | null = null;
  for (const entry of entries) {
    const name = entry.project_id ? names.get(entry.project_id) : undefined;
    if (!entry.project_id || !name) {
      outside ??= { key: "", label: t("centre.noproject"), entries: [] };
      outside.entries.push(entry);
      continue;
    }
    let group = groups.get(entry.project_id);
    if (!group) groups.set(entry.project_id, (group = { key: entry.project_id, label: name, entries: [] }));
    group.entries.push(entry);
  }
  return outside ? [...groups.values(), outside] : [...groups.values()];
}

// ── doing things with an entry ───────────────────────────────────────────────────────────

export type ActResult = { resolution: string | null; notification: Notification | null; conflict: boolean };

/**
 * Take one of an entry's actions. A 409 is not a failure to report but an answer: somebody (the
 * phone, Telegram, the orchestrator) answered first, and the body says how. `api.post` would keep
 * only the status of it, so this reads the response itself.
 */
export async function act(id: number, action: string, value?: string): Promise<ActResult> {
  const response = await fetch(`/api/notifications/${id}/act`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...api.authHeaders() },
    body: JSON.stringify(value === undefined ? { action } : { action, value }),
  });
  let body: { resolution?: string | null; notification?: Notification | null; detail?: unknown } = {};
  try {
    body = await response.json();
  } catch {
    /* a proxy's page: the status says enough */
  }
  if (response.status === 409) return { resolution: body.resolution ?? null, notification: body.notification ?? null, conflict: true };
  if (!response.ok) throw new ApiError(response.status, typeof body.detail === "string" && body.detail ? body.detail : `Request failed (${response.status})`);
  return { resolution: body.resolution ?? null, notification: body.notification ?? null, conflict: false };
}

/** After an answer the lists are read again, and the badge too when no stream is there to bring it. */
export function afterChange(): void {
  invalidate(LIST_PREFIX);
  if (!streamUp()) invalidate(SUMMARY_KEY);
}

export async function markSeen(ids: number[]): Promise<void> {
  if (!ids.length) return;
  try {
    const r = await api.post<{ marked: number; summary: NotificationSummary }>("/api/notifications/seen", { ids });
    if (r?.summary) prime(SUMMARY_KEY, r.summary);
    invalidate(LIST_PREFIX);
  } catch (e) {
    toast(errorText(e));
  }
}

export async function markAllSeen(): Promise<void> {
  try {
    const r = await api.post<{ marked: number; summary: NotificationSummary }>("/api/notifications/seen", { all: true });
    if (r?.summary) prime(SUMMARY_KEY, r.summary);
    invalidate(LIST_PREFIX);
  } catch (e) {
    toast(errorText(e));
  }
}

/** Where an entry takes the reader: its own link, else its session, else the centre. */
export function entryPath(entry: Pick<Notification, "link" | "session_id">): string {
  // The host's links to a project or the main chat are older than orchestration mode's addresses.
  if (entry.link && entry.link.startsWith("/app/")) return canonical(entry.link);
  if (entry.session_id) return sessionPath(entry.session_id);
  return pathFor("inbox");
}

/** Opening an entry is reading it. */
export function openEntry(entry: Notification): void {
  if (!entry.seen) void markSeen([entry.id]);
  navigate(entryPath(entry));
}

// ── the parts ────────────────────────────────────────────────────────────────────────────

const STYLE: Record<string, string> = { primary: "btn small primary", default: "btn small", danger: "btn small danger", ghost: "btn small ghost" };

/**
 * The entry's buttons, answered where they are. Once the request is answered — here or anywhere
 * else — the buttons give way to what the answer was.
 */
export function ActionButtons({ entry, onDone }: { entry: Notification; onDone?: (result: ActResult) => void }) {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);
  const resolved = outcome ?? entry.resolved;
  if (resolved) {
    return (
      <div className="notice-actions">
        <span className={`notice-outcome ${resolved}`}>{resolutionLabel(resolved)}</span>
      </div>
    );
  }
  if (!entry.needs_you || entry.actions.length === 0) return null;
  async function take(action: string) {
    setBusy(true);
    try {
      const result = await act(entry.id, action);
      if (result.resolution) setOutcome(result.resolution);
      if (result.conflict) toast(t("notice.conflict", { outcome: resolutionLabel(result.resolution ?? "answered") }));
      afterChange();
      onDone?.(result);
    } catch (e) {
      toast(t("notice.failed", { error: errorText(e) }));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="notice-actions" onClick={(e) => e.stopPropagation()}>
      {entry.actions.map((a) => (
        <button
          key={a.id}
          className={STYLE[a.style] ?? STYLE.default}
          data-action={a.id}
          disabled={busy}
          onClick={() => {
            if (a.id === "open") {
              openEntry(entry);
              onDone?.({ resolution: null, notification: entry, conflict: false });
            } else void take(a.id);
          }}
        >
          {a.label || (a.id === "open" ? t("notice.open") : a.id)}
        </button>
      ))}
    </div>
  );
}

/** One entry: its picture in the tone's colour, the title, where it came from, when, and how many times. */
export function NotificationRow({ entry, names, onActivate, expanded, children, card, showBody }: { entry: Notification; names: Map<string, string>; onActivate: () => void; expanded?: boolean; children?: ReactNode; card?: boolean; showBody?: boolean }) {
  const tone = toneClass(entry);
  const classes = ["notice-row", card ? "card" : "flat", entry.seen ? "" : "unread", entry.level === "quiet" ? "quiet" : "", entry.needs_you ? "needs" : ""].filter(Boolean).join(" ");
  return (
    <div
      className={classes}
      data-notice={entry.id}
      role="button"
      tabIndex={0}
      aria-expanded={expanded}
      onClick={onActivate}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onActivate();
        }
      }}
    >
      <span className={`kind ${tone}`} aria-label={categoryLabel(entry.category)}>
        <Icon name={noticeIcon(entry)} size={16} />
      </span>
      <div className="notice-main">
        <div className="notice-head">
          <span className={`notice-title ${expanded ? "" : "clamp-2"}`}>{entry.title}</span>
          {entry.count > 1 && <span className="notice-count num">×{entry.count}</span>}
          <span className="notice-time num" title={absTime(entry.updated_at)}>{relTime(entry.updated_at)}</span>
        </div>
        <div className="notice-meta">{noticeLine(entry, names)}</div>
        {showBody && entry.body && !expanded && <div className="notice-body clamp-2">{entry.body}</div>}
        {children}
      </div>
    </div>
  );
}

/** The open requests, first: a permission to grant, a question to answer. Nothing when nothing waits. */
export function NeedsYou({ names, onOpened, card, limit = 20, empty }: { names: Map<string, string>; onOpened?: () => void; card?: boolean; limit?: number; empty?: ReactNode }) {
  const { data } = useNotifications("needs_you", null, limit);
  const entries = data?.entries ?? [];
  if (entries.length === 0) return <>{data ? empty ?? null : null}</>;
  return (
    <section className="needs-you" aria-label={t("centre.needs")}>
      <div className="section-title">{t("centre.needs")} <span className="n">{entries.length}</span></div>
      {entries.map((entry) => (
        <NotificationRow key={entry.id} entry={entry} names={names} card={card} showBody onActivate={() => { openEntry(entry); onOpened?.(); }}>
          <ActionButtons entry={entry} onDone={(r) => { if (!r.resolution) onOpened?.(); }} />
        </NotificationRow>
      ))}
    </section>
  );
}

/** Entries under headings: the day (Today, Earlier) or the project. `row` draws each one. */
export function NotificationList({ groups, row }: { groups: Group[]; row: (entry: Notification) => ReactNode }) {
  return (
    <>
      {groups.map((group) => (
        <section key={group.key || "-"} className="notice-group">
          <div className="section-title">{group.label}{group.key !== "today" && group.key !== "earlier" && <span className="n">{group.entries.length}</span>}</div>
          {group.entries.map((entry) => row(entry))}
        </section>
      ))}
    </>
  );
}
