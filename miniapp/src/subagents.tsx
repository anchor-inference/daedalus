// The Subagents tab: a session's helpers beside it. The list says who is working, on what, on which
// model and since when; a click shows that helper's conversation in the tab itself, read-only and
// following its run, with a button to open it as a full chat. A helper is opened here wherever the app
// names one — the leader's transcript, the left column, a notification — so reading what a helper did
// no longer takes the leader off the screen.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, type SessionList, type SubagentView } from "./api";
import { useEvent, useStreamUp } from "./events";
import { duration, relTime } from "./format";
import { t } from "./i18n";
import { Icon } from "./icons";
import { pathFor, sessionPath } from "./router";
import { peek } from "./store";
import { statusWord } from "./ui/components";
import { errorText } from "./ui";

/** Several events of one run arrive together; the list is read once for the burst. */
export const SUBAGENTS_EVENT_DEBOUNCE_MS = 250;
/** How often "working · 3m" is drawn again; nothing is fetched for it. */
export const SUBAGENTS_TICK_MS = 15000;
/** The net under the events while the stream is down. */
export const SUBAGENTS_POLL_MS = 5000;

/** Where a subagent opens by default: its leader's chat with the Subagents tab on the helper. */
export function subagentPath(leaderId: string, childId: string): string {
  return pathFor("agents", leaderId, { panel: "subagents", sub: childId });
}

/** The leader of a session the cached listings know to be a subagent whose leader is still listed, or
 *  null. Read from what the left column already fetched: an address is decided without a request, and
 *  a helper whose leader is gone opens as the chat it then is. */
export function listedLeaderOf(id: string, lists: (SessionList | undefined)[] = [peek<SessionList>("/api/sessions?view=all"), peek<SessionList>("/api/sessions")]): string | null {
  for (const list of lists) {
    const sessions = list?.sessions ?? [];
    const leader = sessions.find((s) => s.id === id)?.metadata?.subagent_of;
    if (leader && sessions.some((s) => s.id === leader)) return leader;
  }
  return null;
}

/** The address a session opens at: beside its leader for a subagent, its own chat otherwise. */
export function openPathOf(id: string): string {
  const leader = listedLeaderOf(id);
  return leader ? subagentPath(leader, id) : sessionPath(id);
}

/** Which of the helpers a reference names: by session id first, then by the name the leader gave it. */
export function subagentIdFor(children: readonly SubagentView[], ref: { id?: string; name?: string }): string | null {
  if (ref.id && children.some((c) => c.session_id === ref.id)) return ref.id;
  const named = ref.name ? children.find((c) => c.name === ref.name) : undefined;
  return named?.session_id ?? null;
}

/** Whether an event changes this session's list of helpers: one of them changing state or gone, or a
 *  new one made under it. A helper is known by the list already holding it. */
export function touchesSubagents(event: { type: string; session_id: string | null; payload: Record<string, unknown> }, leaderId: string, children: ReadonlySet<string>): boolean {
  if (event.type === "session.created") return event.payload.parent === leaderId;
  return !!event.session_id && children.has(event.session_id);
}

/** The state a helper's dot shows: working, waiting on a question, failed, or at rest. */
export function subagentState(child: SubagentView): "running" | "waiting" | "failed" | "idle" {
  if (child.status === "waiting") return "waiting";
  if (child.running || child.status === "running" || child.status === "compacting") return "running";
  return child.status === "failed" ? "failed" : "idle";
}

/** The words under a helper's name: how it stands and for how long, and the model it runs on. */
export function subagentLine(child: SubagentView, now: number): string {
  const state = subagentState(child);
  const since = child.run_started_at ? Date.parse(child.run_started_at) : NaN;
  const word = state === "running"
    ? [t("subagents.working"), Number.isNaN(since) ? "" : duration(Math.max(0, now - since))].filter(Boolean).join(" ")
    : state === "idle"
      ? [child.kept || child.idle_start ? t("subagents.waits") : statusWord("done"), child.last_activity_at ? relTime(child.last_activity_at, now) : ""].filter(Boolean).join(" · ")
      : statusWord(state).toLowerCase();
  return [word, child.model].filter(Boolean).join(" · ");
}

export function SubagentsTab({ leaderId, selected, onSelect, onOpenFull, chat, toast }: {
  leaderId: string;
  /** The helper whose conversation is shown, or null for the list. */
  selected: string | null;
  onSelect: (id: string | null) => void;
  onOpenFull: (id: string) => void;
  /** The conversation itself, drawn by the screen that knows how to draw one. */
  chat: (id: string) => ReactNode;
  toast: (text: string) => void;
}) {
  const [children, setChildren] = useState<SubagentView[] | null>(null);
  const load = (gone: () => boolean = () => false) =>
    api.get<{ subagents: SubagentView[] }>(`/api/sessions/${leaderId}/subagents`)
      .then((answer) => !gone() && setChildren(answer.subagents ?? []))
      .catch(() => !gone() && setChildren((held) => held ?? []));
  useEffect(() => {
    let gone = false;
    setChildren(null);
    void load(() => gone);
    return () => { gone = true; };
  }, [leaderId]);
  const known = useRef<Set<string>>(new Set());
  known.current = new Set((children ?? []).map((c) => c.session_id));
  const soon = useRef<number | null>(null);
  useEffect(() => () => { if (soon.current) window.clearTimeout(soon.current); }, [leaderId]);
  useEvent(["session.created", "session.status", "session.deleted", "run.finished"], (event) => {
    if (!touchesSubagents(event, leaderId, known.current) || soon.current) return;
    soon.current = window.setTimeout(() => { soon.current = null; void load(); }, SUBAGENTS_EVENT_DEBOUNCE_MS);
  }, [leaderId]);
  const live = useStreamUp();
  const anyRunning = !!children?.some((c) => subagentState(c) === "running");
  const [, tick] = useState(0);
  useEffect(() => {
    if (!anyRunning && live) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      if (live) tick((n) => n + 1);
      else void load();
    }, live ? SUBAGENTS_TICK_MS : SUBAGENTS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [leaderId, anyRunning, live]);

  const current = selected ? children?.find((c) => c.session_id === selected) : undefined;
  if (selected) {
    const state = current ? subagentState(current) : "idle";
    const stop = async () => {
      try {
        await api.post(`/api/sessions/${selected}/stop`);
        void load();
      } catch (error) {
        toast(errorText(error));
      }
    };
    return (
      <div className="subagents subagent-open">
        <div className="subagent-head">
          <button type="button" className="iconbtn small" onClick={() => onSelect(null)} aria-label={t("subagents.back")} title={t("subagents.back")}><Icon name="back" size={16} /></button>
          <span className={`sub-dot ${state}`} aria-hidden="true" />
          <span className="subagent-head-copy">
            <b className="truncate">{current?.name || current?.title || selected}</b>
            {current && <small className="truncate">{subagentLine(current, Date.now())}</small>}
          </span>
          {state === "running" && <button type="button" className="iconbtn small" onClick={stop} aria-label={t("task.stop")} title={t("task.stop")}><Icon name="stop" size={16} /></button>}
          <button type="button" className="iconbtn small subagent-full" onClick={() => onOpenFull(selected)} aria-label={t("subagents.full")} title={t("subagents.full")}><Icon name="expand" size={16} /></button>
        </div>
        {children !== null && !current ? <div className="empty">{t("subagents.gone")}</div> : chat(selected)}
      </div>
    );
  }
  const working = (children ?? []).filter((c) => subagentState(c) === "running").length;
  return (
    <div className="subagents">
      {children && children.length > 0 && (
        <section className="dt-section">
          <div className="dt-label"><span>{t("panel.tab.subagents")}</span><span className="dt-aside">{working ? t("panel.jobs.aside", { running: working, total: children.length }) : children.length}</span></div>
          {children.map((child) => {
            const state = subagentState(child);
            return (
              <button key={child.session_id} type="button" className={`aside-row subagent-row ${state}`} data-subagent={child.session_id} onClick={() => onSelect(child.session_id)} title={child.session_id}>
                <span className={`sub-dot ${state}`} aria-hidden="true" />
                <span className="grow subagent-copy">
                  <span className="name truncate">{child.name || child.session_id}</span>
                  {child.title && child.title !== child.name && <span className="subagent-title truncate">{child.title}</span>}
                  <span className="sub truncate">{subagentLine(child, Date.now())}</span>
                </span>
                <Icon name="forward" size={14} />
              </button>
            );
          })}
        </section>
      )}
      {children && children.length === 0 && (
        <div className="empty">
          <b>{t("subagents.empty.title")}</b>
          <div>{t("subagents.empty.body")}</div>
        </div>
      )}
      {children === null && <div className="sub subagents-loading">{t("common.loading")}</div>}
    </div>
  );
}
