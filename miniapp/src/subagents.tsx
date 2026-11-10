// The Subagents tab: a session's helpers beside it. The list says who is working, on what, on which
// model and since when; a click shows that helper's conversation in the tab itself, read-only and
// following its run, with a button to open it as a full chat. A helper is opened here wherever the app
// names one — the leader's transcript, the left column, a notification — so reading what a helper did
// no longer takes the leader off the screen.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, type SessionList, type SubagentView } from "./api";
import { contextTone } from "./detailsmodel";
import { useEvent, useStreamUp } from "./events";
import { absDate, clock, duration, relTime, relTimeLong, shortModel } from "./format";
import { t } from "./i18n";
import { Icon, type IconName } from "./icons";
import { REASONING_EFFORTS } from "./models";
import { pathFor, sessionPath } from "./router";
import { peek } from "./store";
import { fmtUsd, statusWord } from "./ui/components";
import { ProviderMark } from "./ui/provider-mark";
import { errorText, fmtTok } from "./ui";

/** Several events of one run arrive together; the list is read once for the burst. */
export const SUBAGENTS_EVENT_DEBOUNCE_MS = 250;
/** How often the list is read while a helper works, or while the stream is down. The global stream
 *  says when a helper starts and stops, not which tool it is on or what it has spent, so the card's
 *  live line and its cost come from this read; with nothing working nothing is fetched. */
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

/** The state a helper's dot shows: working, waiting on a question, parked by the daily cap, failed, or at rest. */
export function subagentState(child: SubagentView): "running" | "waiting" | "parked" | "failed" | "idle" {
  if (child.status === "waiting") return "waiting";
  if (child.running || child.status === "running" || child.status === "compacting") return "running";
  // Before failed: a helper the cap stopped did not fail, and it goes on by itself when the cap reopens.
  if (child.status === "parked" || child.parked) return "parked";
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
      : state === "parked"
        ? t("subagents.parked")
        : statusWord(state).toLowerCase();
  const held = child.reports_held ? t("subagents.held") : "";
  return [word, held, child.model].filter(Boolean).join(" · ");
}

/** The state a card names, finer than the dot's: a helper at rest has finished, been stopped, or never had work. */
export type SubagentPhase = "working" | "question" | "parked" | "failed" | "stopped" | "done" | "standby";

export function subagentPhase(child: SubagentView): SubagentPhase {
  const state = subagentState(child);
  if (state === "running") return "working";
  if (state === "waiting") return "question";
  if (state === "parked" || state === "failed") return state;
  const cause = child.report?.cause;
  if (cause === "cancelled") return "stopped";
  // A run a cap or the provider ended reads as failed even once the session's own error is cleared.
  if (cause && cause !== "completed" && cause !== "budget") return "failed";
  // A standing helper never handed work has nothing to have finished.
  return !cause && child.idle_start ? "standby" : "done";
}

const PHASE_ICON: Record<SubagentPhase, IconName> = { working: "bolt", question: "question", parked: "pause", failed: "alert", stopped: "stop", done: "check", standby: "clock" };

/** Whether a helper belongs with the working ones at the top: at work, asking, or parked to go on by itself. */
export function subagentActive(phase: SubagentPhase): boolean {
  return phase === "working" || phase === "question" || phase === "parked";
}

const time = (iso: string | null | undefined) => (iso ? Date.parse(iso) || 0 : 0);

/** The two groups the tab draws: the active ones by when they started, newest first, then the rest by when they ended. */
export function subagentGroups(children: readonly SubagentView[]): { active: SubagentView[]; finished: SubagentView[] } {
  const active = children.filter((c) => subagentActive(subagentPhase(c)));
  const finished = children.filter((c) => !subagentActive(subagentPhase(c)));
  active.sort((a, b) => time(b.created_at) - time(a.created_at));
  finished.sort((a, b) => time(b.finished_at ?? b.last_activity_at) - time(a.finished_at ?? a.last_activity_at));
  return { active, finished };
}

/** The tab's first line: how many work, have finished and have failed, and what all of them cost.
 *  ``partial`` is set when some call had no known price, so the sum is a floor and is shown as one. */
export function subagentSummary(children: readonly SubagentView[]): { running: number; done: number; failed: number; usd: number; partial: boolean } {
  const phases = children.map(subagentPhase);
  return {
    running: phases.filter(subagentActive).length,
    done: phases.filter((p) => p === "done").length,
    failed: phases.filter((p) => p === "failed").length,
    usd: children.reduce((sum, c) => sum + (c.usage?.usd ?? 0), 0),
    partial: children.some((c) => (c.usage?.unpriced ?? 0) > 0),
  };
}

/** The effort as the card's chip names it: the composer's words, "no thinking" for off, and an effort
 *  this app has no word for (a provider's own) as it came. Empty when the host did not say. */
export function effortLabel(effort: string | undefined): string {
  if (!effort) return "";
  if (effort === "off") return t("subagents.effort.off");
  return (REASONING_EFFORTS as readonly string[]).includes(effort) ? t(`add.effort.${effort}`) : effort;
}

/** How full its context is, in percent of the window; null when the window is unknown. */
export function contextPercent(context: SubagentView["context"]): number | null {
  if (!context || context.window <= 0) return null;
  return Math.min(100, Math.round((100 * context.tokens) / context.window));
}

/** What a working helper is doing now: the tool in flight, or thinking, with the steps of the run. */
export function activityLine(activity: SubagentView["activity"]): { what: string; tool: boolean; steps: string } | null {
  if (!activity) return null;
  return {
    what: activity.tool || t("subagents.now.thinking"),
    tool: !!activity.tool,
    steps: activity.steps ? t("subagents.now.steps", { n: activity.steps }) : "",
  };
}

const PHASE_WORD: Record<SubagentPhase, string> = { working: "subagents.phase.working", question: "subagents.phase.question", parked: "subagents.phase.parked", failed: "subagents.phase.failed", stopped: "subagents.phase.stopped", done: "subagents.phase.done", standby: "subagents.waits" };

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
  useEffect(() => {
    if (!anyRunning && live) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, SUBAGENTS_POLL_MS);
    return () => window.clearInterval(timer);
  }, [leaderId, anyRunning, live]);
  const stopOne = async (id: string) => {
    try {
      await api.post(`/api/sessions/${id}/stop`);
      void load();
    } catch (error) {
      toast(errorText(error));
    }
  };

  const current = selected ? children?.find((c) => c.session_id === selected) : undefined;
  if (selected) {
    const state = current ? subagentState(current) : "idle";
    const stop = () => stopOne(selected);
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
  if (children === null) return <div className="subagents"><div className="sub subagents-loading">{t("common.loading")}</div></div>;
  if (children.length === 0) {
    return (
      <div className="subagents">
        <div className="subagents-empty">
          <span className="subagents-empty-mark" aria-hidden="true"><Icon name="spawn" size={20} /></span>
          <b>{t("subagents.empty.title")}</b>
          <p>{t("subagents.empty.body")}</p>
        </div>
      </div>
    );
  }
  const groups = subagentGroups(children);
  const summary = subagentSummary(children);
  const now = Date.now();
  const card = (child: SubagentView) => <SubagentCard key={child.session_id} child={child} now={now} onOpen={() => onSelect(child.session_id)} onOpenFull={() => onOpenFull(child.session_id)} onStop={() => void stopOne(child.session_id)} />;
  return (
    <div className="subagents">
      <div className="subagents-summary" data-subagents-summary>
        <span className="subagents-tally" data-tally="working" data-none={summary.running ? undefined : ""}><i aria-hidden="true" />{t("subagents.sum.running", { n: summary.running })}</span>
        <span className="subagents-tally" data-tally="done" data-none={summary.done ? undefined : ""}><i aria-hidden="true" />{t("subagents.sum.done", { n: summary.done })}</span>
        <span className="subagents-tally" data-tally="failed" data-none={summary.failed ? undefined : ""}><i aria-hidden="true" />{t("subagents.sum.failed", { n: summary.failed })}</span>
        <span className="grow" />
        <span className="subagents-total" title={summary.partial ? t("subagents.cost.partial") : t("subagents.cost.total")}>{fmtUsd(summary.usd)}{summary.partial ? "+" : ""}</span>
      </div>
      {groups.active.length > 0 && (
        <section className="subagents-group" data-group="active">
          <div className="dt-label"><span>{t("subagents.group.active")}</span><span className="dt-aside">{groups.active.length}</span></div>
          {groups.active.map(card)}
        </section>
      )}
      {groups.finished.length > 0 && (
        <section className="subagents-group" data-group="finished">
          <div className="dt-label"><span>{t("subagents.group.finished")}</span><span className="dt-aside">{groups.finished.length}</span></div>
          {groups.finished.map(card)}
        </section>
      )}
    </div>
  );
}

/** One helper as a card: who and how it stands, what it does now, its task, what it runs on and has
 *  spent, and how its last run ended. The head opens its conversation in the tab; the task unfolds in place. */
function SubagentCard({ child, now, onOpen, onOpenFull, onStop }: { child: SubagentView; now: number; onOpen: () => void; onOpenFull: () => void; onStop: () => void }) {
  const [open, setOpen] = useState(false);
  const phase = subagentPhase(child);
  const active = subagentActive(phase);
  const name = child.name || child.session_id;
  const brief = (child.brief || child.title || "").trim();
  const since = child.run_started_at ? Date.parse(child.run_started_at) : NaN;
  const ended = child.finished_at ?? (active ? null : child.last_activity_at ?? null);
  const when = phase === "working" || phase === "question"
    ? Number.isNaN(since) ? "" : duration(Math.max(0, now - since))
    : ended ? relTime(ended, now) : "";
  const whenTitle = active
    ? Number.isNaN(since) ? "" : t("subagents.time.since", { at: clock(child.run_started_at) })
    : ended ? t("subagents.time.ended", { at: `${absDate(ended)} ${clock(ended)}`, ago: relTimeLong(ended, now) }) : "";
  const doing = phase === "working" ? activityLine(child.activity) : null;
  const percent = contextPercent(child.context);
  const tone = percent !== null ? contextTone(percent) : null;
  const usage = child.usage;
  const effort = effortLabel(child.effort);
  const report = child.report;
  const held = (child.reports_held ?? 0) > 0;
  return (
    <article className="subagent-card" data-subagent={child.session_id} data-phase={phase}>
      <button type="button" className="subagent-card-head" onClick={onOpen} title={t("subagents.open.transcript")}>
        <span className="subagent-name truncate">{name}</span>
        <span className="subagent-chip" data-phase={phase}><span className={`sub-dot ${subagentState(child)}`} aria-hidden="true" /><Icon name={PHASE_ICON[phase]} size={12} />{t(PHASE_WORD[phase])}</span>
        <span className="grow" />
        {when && <span className="subagent-when" title={whenTitle}>{when}</span>}
        <Icon name="forward" size={14} />
      </button>
      {phase === "parked" && <div className="subagent-note attn">{t("subagents.parked")}</div>}
      {doing && (
        <div className="subagent-now" data-subagent-now>
          <span className="subagent-now-pulse" aria-hidden="true" />
          {doing.tool ? <code className="truncate">{doing.what}</code> : <span className="truncate">{doing.what}</span>}
          {doing.steps && <span className="subagent-now-steps">{doing.steps}</span>}
        </div>
      )}
      {brief && (
        <button type="button" className={`subagent-brief ${open ? "open" : "clamp-2"}`} aria-expanded={open} onClick={() => setOpen((v) => !v)} title={open ? t("subagents.brief.less") : t("subagents.brief.more")}>
          {brief}
        </button>
      )}
      {!report && held && <div className="subagent-note attn">{t("subagents.held")}</div>}
      {report && (report.summary || phase !== "done") && (
        <div className="subagent-report" data-cause={report.cause} data-subagent-report>
          {report.summary && <p className="clamp-3">{report.summary}</p>}
          <span className={`subagent-delivery ${report.delivered && !held ? "ok" : "wait"}`}>
            <Icon name={report.delivered && !held ? "check" : "clock"} size={12} />
            {report.delivered && !held ? t("subagents.report.delivered", { when: relTimeLong(report.delivered_at ?? report.at, now) }) : held ? t("subagents.held") : t("subagents.report.waiting")}
          </span>
        </div>
      )}
      <div className="subagent-run">
        <span className="subagent-model" title={child.model}>
          {child.provider && <ProviderMark id={child.provider} className="small" />}
          <span className="truncate">{shortModel(child.model, 32)}</span>
        </span>
        {effort && <span className={`subagent-effort ${child.effort === "off" ? "off" : ""}`} title={t("subagents.effort.title")}><Icon name="bulb" size={11} />{effort}</span>}
        <span className="grow" />
        {usage && usage.calls > 0 && (
          <span className="subagent-spend" title={t("subagents.spend.title", { calls: usage.calls, cached: fmtTok(usage.cache_read) })}>
            <span>{t("subagents.spend.tokens", { in: fmtTok(usage.input), out: fmtTok(usage.output) })}</span>
            <b>{fmtUsd(usage.usd)}{usage.unpriced ? "+" : ""}</b>
          </span>
        )}
      </div>
      {percent !== null && child.context && (
        <div className="subagent-context" title={t("subagents.context.title", { used: fmtTok(child.context.tokens), window: fmtTok(child.context.window) })}>
          <div className={`subagent-bar ${tone ?? ""}`} role="img" aria-label={t("strip.context.bar", { n: percent })}><i style={{ width: `${percent}%` }} /></div>
          <span>{t("subagents.context.used", { n: percent, window: fmtTok(child.context.window) })}</span>
        </div>
      )}
      <div className="subagent-actions">
        <button type="button" className="btn small ghost" onClick={onOpen}><Icon name="chat" size={14} />{t("subagents.action.transcript")}</button>
        <button type="button" className="btn small ghost" onClick={onOpenFull}><Icon name="expand" size={14} />{t("subagents.action.full")}</button>
        {phase === "working" && <button type="button" className="btn small ghost subagent-stop" onClick={onStop}><Icon name="stop" size={14} />{t("subagents.action.stop")}</button>}
      </div>
    </article>
  );
}
