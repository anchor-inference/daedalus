// The Details tab: what the session is and what it is set to, one card per section. A card opens on
// its headline (the context's fill, the spend, the quota windows) and keeps the rest beneath it in
// tiles, quiet facts or a "More" fold; the parts it is built from are in detailsparts.tsx. It is the
// old aside's cards and the "Session info" sheet in one place; the right panel hosts it on a desktop
// and a full sheet on a phone.

import { OriginSection } from "./imports/ImportedChat";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, LoopView, ProviderUsage, Schedule, SessionDetail } from "./api";
import { projectPath } from "./folders";
import { ServiceRow, Switch, ToolPicker, fmtInt, fmtUsd, loopLabel, statusWord, timeAgo } from "./ui/components";
import { absTime, clock, planName, relTimeLong, shortDateTime, untilShort } from "./format";
import { Icon } from "./icons";
import { CopyValue, Fact, Facts, Headline, Meter, Note, Section, Stat, Stats, Until } from "./detailsparts";
import { contextSegments, contextTone, quotaTone } from "./detailsmodel";
import { confirmAsync, errorText, fmtTok } from "./ui";
import { plural, t } from "./i18n";
import { SessionToolGroups } from "./toolgroupsview";
import { HostMark } from "./runon";
import { DiskUsage } from "./diskusage";

export type DetailsActions = {
  rename: (title: string) => void;
  setMode: (mode: string) => void;
  openPicker: () => void;
  compact: () => void;
  clearHistory: () => void;
  remove: () => void;
  exportMarkdown: () => void;
  loopAction: (action: string) => void;
  scheduleAction: (sc: Schedule, action: "run" | "delete") => void;
  move: () => void;
  showLog: (line: string, text: string) => void;
  openFiles: () => void;
};

export type SessionDetailsProps = {
  ids: string;
  id: string;
  /** What the session is: an ordinary one, a project's orchestrator, or the main orchestrator. The
   *  two orchestrators are the same Details less what would break them or has no place beside
   *  them: a loop, the tool switches their role fixes, moving or deleting the session from here,
   *  and (for a project's, whose panel has no Files tab) the workspace row that opens one. */
  role?: "session" | "orchestrator" | "main";
  detail: SessionDetail;
  busy: boolean;
  modes: string[];
  schedules: Schedule[];
  provider: string;
  providerUsage: ProviderUsage | null;
  onOpen?: (id: string) => void;
  toast: (text: string) => void;
  reload: (quiet?: boolean) => void;
  on: DetailsActions;
  /** The section to scroll to once mounted (a chip or a menu item named it). */
  focus?: string | null;
};

export function SessionDetails({ ids, id, role = "session", detail, busy, modes, schedules, provider, providerUsage, onOpen, toast, reload, on, focus }: SessionDetailsProps) {
  const container = useRef<HTMLDivElement>(null);
  const conductor = role !== "session";
  const ctxPct = detail.context && detail.context.window > 0 ? Math.round((100 * detail.context.tokens) / detail.context.window) : null;
  useEffect(() => {
    if (!focus || focus === "session") return;
    const timer = window.setTimeout(() => { const section = container.current?.querySelector<HTMLDetailsElement>(`#${CSS.escape(`${ids}-info-${focus}`)}`); if (section) { section.open = true; section.scrollIntoView({ block: "start" }); } }, 30);
    return () => window.clearTimeout(timer);
  }, [focus, ids]);
  return (
    <div ref={container} className="details">
      {detail.imported && (
        <Section ids={ids} id="origin" icon="fork" label={t("imp.origin")}>
          <OriginSection sessionId={id} origin={detail.imported} toast={toast} onPulled={() => reload(true)} />
        </Section>
      )}
      <Section ids={ids} id="session" icon="sliders" label={t("session.card")}>
        <input className="field" defaultValue={detail.title} aria-label={t("session.title")} onBlur={(e) => on.rename(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
        <div className="dt-control">
          <span className="dt-control-label">{t("session.model")}</span>
          <button className="linkbtn grow truncate" onClick={on.openPicker} title={detail.model}>{detail.model || t("session.model.global")}</button>
          <button className="btn small" onClick={on.openPicker}>{t("session.model.change")}</button>
        </div>
        <label className="dt-control">
          <span className="dt-control-label">{t("session.mode")}</span>
          <select className="field grow" value={detail.mode || "default"} onChange={(e) => on.setMode(e.target.value)}>
            {["default", ...modes].map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </label>
      </Section>

      {detail.context && (
        <Section ids={ids} id="context" icon="braces" label={t("session.context")} aside={ctxPct !== null ? `${ctxPct}%` : undefined}>
          <ContextBody context={detail.context} percent={ctxPct} busy={busy} onCompact={on.compact} />
        </Section>
      )}

      <Section ids={ids} id="usage" icon="chart" label={t("session.usage")} aside={fmtUsd(detail.usage.usd)}>
        <UsageBody sessionId={id} usage={detail.usage} />
      </Section>

      {role !== "orchestrator" && <Section ids={ids} id="workspace" icon="folder" label={t("session.workspace")}>
        <button className="aside-row link dt-workspace" onClick={on.openFiles} title={detail.project ? projectPath(detail.project) : detail.workspace}>
          <Icon name="folder" size={16} />
          <span className="grow name">{detail.project ? t("session.aside.project", { name: detail.project.name }) : detail.workspace_own === false ? t("session.aside.workspace", { name: detail.workspace_name ?? "" }) : t("session.aside.own")}</span>
          <HostMark env={detail.env} />
          <span className="dt-link">{t("panel.tab.files")}<Icon name="forward" size={12} /></span>
        </button>
        <div className="dt-path" title={detail.workspace}>
          <span className="mono grow">{detail.project ? projectPath(detail.project) : detail.workspace}</span>
          {detail.project && (
            <span className="badge" title={t(detail.project.settings.snapshots ? "session.aside.snapshots.title" : "session.aside.nosnapshots.title")}>
              {t(detail.project.settings.snapshots ? "project.snapshots.badge" : "session.aside.nosnapshots")}
            </span>
          )}
        </div>
        <Facts>
          {detail.workspace_sessions && detail.workspace_sessions.length > 0 && (
            <Fact label={t("panel.shared")}>
              {detail.workspace_sessions.slice(0, 4).map((w, i) => (
                <span key={w.id}>
                  {i > 0 && ", "}
                  <button className="linkbtn" onClick={() => onOpen?.(w.id)} title={t("session.aside.sameworkspace")}>{w.title}</button>
                </span>
              ))}
            </Fact>
          )}
          {!conductor && (
            <Fact label={t("session.project")} title={projectPath(detail.project)}>
              <span className="dt-prose">{t("session.project.inside", { name: detail.project.name })}</span>
            </Fact>
          )}
        </Facts>
        {!conductor && <div className="btnrow"><button className="btn small" onClick={on.move}>{t("session.project.move")}</button></div>}
      </Section>}

      {role !== "orchestrator" && <Section ids={ids} id="disk" icon="archive" label={t("disk.title")} closed>
        <DiskUsage kind="session" id={id} toast={toast} />
      </Section>}

      {provider && <ProviderUsageCard ids={ids} provider={provider} usage={providerUsage} />}

      {!conductor && <Section ids={ids} id="loop" icon="loop" label={t("session.loop")} aside={detail.loop ? <span className={`badge loop ${detail.loop.status}`}>{statusWord(detail.loop.status).toLowerCase()}</span> : undefined}>
        {detail.loop && (
          <div className="dt-hint">{loopLabel(detail.loop).replace(/^\S+ · /, "")}{detail.loop.next_run_at && detail.loop.status === "active" ? t("agents.loop.next", { t: untilShort(detail.loop.next_run_at) }) : ""}</div>
        )}
        <LoopPanel sessionId={id} loop={detail.loop ?? null} onChange={() => reload(true)} toast={toast} />
      </Section>}

      {schedules.length > 0 && (
        <Section ids={ids} id="cron" icon="calendar" label={t("session.cron")} aside={t("session.cron.on", { n: schedules.filter((x) => x.enabled).length })}>
          {schedules.map((sc) => <ScheduleRow key={sc.id} sc={sc} sessionId={id} onAction={on.scheduleAction} />)}
        </Section>
      )}

      {detail.services && detail.services.length > 0 && (
        <Section ids={ids} id="services" icon="globe" label={t("session.services")} aside={t("session.services.running", { n: detail.services.filter((s) => s.status === "running").length })}>
          {detail.services.map((s) => (
            <ServiceRow key={s.name} s={s} sessionId={id} onChange={() => reload(true)} toast={toast} onLogs={(text) => on.showLog(`service ${s.name} · log`, text)} />
          ))}
        </Section>
      )}

      <Section ids={ids} id="mcp" icon="plug" label={t("session.mcp")}>
        <McpPanel sessionId={id} toast={toast} />
      </Section>

      <Section ids={ids} id="toolgroups" icon="skill" label={t("session.toolgroups")}>
        <SessionToolGroups sessionId={id} toast={toast} />
      </Section>

      {!conductor && <Section ids={ids} id="tools" icon="wrench" label={t("session.tools")}>
        <ToolPicker
          off={detail.tools_off ?? []}
          note={t("session.tools.note")}
          onChange={async (off) => {
            try {
              await api.post(`/api/sessions/${id}/tools`, { tools_off: off });
              reload(true);
            } catch (e) {
              toast(errorText(e));
            }
          }}
        />
      </Section>}

      <Section ids={ids} id="brief" icon="journal" label={t("session.brief")}>
        <label className="field dt-hint">{t("session.brief.label")}{detail.spawned_by ? t("session.brief.by", { id: detail.spawned_by }) : ""}</label>
        <textarea
          className="field"
          rows={4}
          defaultValue={detail.brief ?? ""}
          placeholder={t("session.brief.placeholder")}
          onBlur={async (e) => {
            const brief = e.target.value.trim();
            if (brief === (detail.brief ?? "")) return;
            try {
              await api.post(`/api/sessions/${id}/brief`, { brief });
              toast(t(brief ? "session.brief.saved" : "session.brief.removed"));
              reload();
            } catch (err) {
              toast(errorText(err));
            }
          }}
        />
      </Section>

      <Section ids={ids} id="spend" icon="flag" label={t("session.cap")} aside={t("settings.limits.spent", { sum: fmtUsd(detail.usage.usd) })}>
        <input
          className="field"
          type="number"
          step="0.5"
          min={0}
          aria-label={t("session.cap.label")}
          placeholder={t("session.cap.none")}
          defaultValue={detail.usd_cap ?? ""}
          onBlur={async (e) => {
            const raw = e.target.value.trim();
            const cap = raw === "" ? null : Number(raw);
            if (cap !== null && Number.isNaN(cap)) return;
            if (cap === (detail.usd_cap ?? null)) return;
            try {
              await api.post(`/api/sessions/${id}/cap`, { usd_cap: cap });
              toast(cap === null ? t("session.cap.removed") : t("session.cap.set", { n: cap }));
              reload();
            } catch (err) {
              toast(errorText(err));
            }
          }}
        />
      </Section>

      <Section ids={ids} id="advanced" icon="settings" label={t("session.advanced")}>
        <Facts>
          <Fact label={t("session.id")}><CopyValue value={id} shown={id} toast={toast} copied={t("session.id.copied")} /></Fact>
          <Fact label={t("session.workspace")}><CopyValue value={detail.workspace} shown={detail.workspace_own === false ? detail.workspace_name : undefined} max={28} toast={toast} copied={t("session.path.copied")} /></Fact>
          {detail.run_id && <Fact label={t("session.runid")} mono>{detail.run_id}</Fact>}
        </Facts>
        <div className="btnrow">
          <button className="btn small" onClick={on.exportMarkdown}><Icon name="download" size={14} /> {t("session.export")}</button>
        </div>
      </Section>

      {!conductor && <Section ids={ids} id="danger" icon="alert" label={t("session.danger")} className="danger">
        <div className="btnrow" style={{ marginTop: 0 }}>
          <button className="btn small danger" onClick={on.clearHistory} disabled={busy}><Icon name="trash" size={14} /> {t("session.clear.short")}</button>
          <button className="btn small danger" onClick={on.remove}><Icon name="trash" size={14} /> {t("session.delete.short")}</button>
        </div>
      </Section>}
    </div>
  );
}

type ContextView = NonNullable<SessionDetail["context"]>;

/** The context: how full the window is, what fills it, what the cache reused, and when it was last
 *  compacted beside the button that compacts it. Each was a sentence of its own before. */
function ContextBody({ context, percent, busy, onCompact }: { context: ContextView; percent: number | null; busy: boolean; onCompact: () => void }) {
  const tone = percent !== null ? contextTone(percent) : null;
  const segments = context.breakdown ? contextSegments(context.breakdown, context.window) : null;
  const measured = t(context.estimated ? "session.context.estimated" : "session.context.measured");
  return <>
    <Headline
      value={percent !== null ? `${percent}%` : fmtTok(context.tokens)}
      tone={tone}
      title={context.window > 0 ? t("session.ctx.title", { used: fmtInt(context.tokens), window: fmtInt(context.window) }) : undefined}
      caption={context.window > 0 ? t("details.context.of", { used: fmtTok(context.tokens), window: fmtTok(context.window) }) : t("details.context.used", { used: fmtTok(context.tokens) })}
      sub={measured}
    />
    {segments ? (
      <div className="dt-segments" data-context-segments>
        <div className="dt-segbar" role="img" aria-label={t("details.context.split")}>
          {segments.filter((part) => part.percent > 0).map((part) => <i key={part.key} data-part={part.key} style={{ width: `${part.percent}%` }} title={`${t(`session.context.part.${part.key}`)}: ${fmtTok(part.tokens)}`} />)}
        </div>
        <ul className="dt-legend">
          {segments.map((part) => (
            <li key={part.key} data-segment={part.key}>
              <span className="dt-swatch" data-part={part.key} aria-hidden />
              <span className="grow">{t(`session.context.part.${part.key}`)}</span>
              <b>{fmtTok(part.tokens)}</b>
            </li>
          ))}
        </ul>
        <p className="dt-hint">{t("session.context.breakdown.estimated")}</p>
      </div>
    ) : percent !== null && (
      <div className={`dt-meter-bar solo ${tone ?? ""}`}><i style={{ width: `${Math.min(100, percent)}%` }} /></div>
    )}
    <Stats>
      <Stat label={t("details.context.messages")} value={fmtInt(context.messages)}
        sub={`${plural("session.context.count.summaries", context.summaries)} · ${plural("details.context.yours", context.operator_turns)}`}
        title={t("session.context.messages", { n: plural("session.context.count.messages", context.messages), s: plural("session.context.count.summaries", context.summaries), o: plural("session.context.count.originals", context.operator_turns) })} />
      {context.recent_cache && <Stat label={t("details.context.cache")} value={`${context.recent_cache.hit_percent ?? 0}%`} sub={t("details.context.cache.read", { tokens: fmtTok(context.recent_cache.read_tokens ?? 0) })} />}
    </Stats>
    <p className="dt-hint">{t("details.context.archive")}</p>
    {!!context.prefix_changed?.length && (
      <Note tone="attn">{t("session.context.prefix", { reasons: context.prefix_changed.map((reason: string) => t(`session.context.prefix.${reason}`)).join(", ") })}</Note>
    )}
    <div className="dt-action">
      {context.last_compaction?.at ? (
        <span className="dt-hint grow" data-last-compaction title={absTime(context.last_compaction.at)}>
          <Icon name="clock" size={12} /> {t("session.context.lastcompaction", { when: relTimeLong(context.last_compaction.at), reason: context.last_compaction.reason || "—" })}
        </span>
      ) : <span className="grow" />}
      <button className="btn small" onClick={onCompact} disabled={busy}><Icon name="compact" size={14} /> {t("session.compact")}</button>
    </div>
  </>;
}

/** What the session spent: the sum as the headline with today beside it, then the four counts as
 *  tiles, each with today's share under its total. */
function UsageBody({ sessionId, usage }: { sessionId: string; usage: SessionDetail["usage"] }) {
  const today = usage.c_today != null;
  return <>
    <Headline
      value={fmtUsd(usage.usd)}
      caption={t("details.usage.total")}
      side={today ? (
        <span className="dt-today" data-usage-today>
          <span className="dt-today-label">{t("session.usage.today")}</span>
          <b>{fmtUsd(usage.usd_today ?? 0)}</b>
        </span>
      ) : undefined}
    />
    <Stats>
      {/* The arrows of the old line said which way the tokens went; the tiles say it in words. */}
      <Stat label={t("details.usage.in")} value={fmtTok(usage.i)} title={t("session.usage.in")} sub={today ? t("details.usage.today", { n: fmtTok(usage.i_today ?? 0) }) : undefined} />
      <Stat label={t("details.usage.out")} value={fmtTok(usage.o)} title={t("session.usage.out")} sub={today ? t("details.usage.today", { n: fmtTok(usage.o_today ?? 0) }) : undefined} />
      <Stat label={t("details.usage.cached")} value={fmtTok(usage.ch ?? 0)} title={t("session.usage.cache")} />
      <Stat label={t("details.usage.calls")} value={fmtInt(usage.c ?? 0)} title={plural("usage.calls", usage.c ?? 0)} sub={today ? t("details.usage.today", { n: fmtInt(usage.c_today ?? 0) }) : undefined} />
    </Stats>
    <ToolTiming sessionId={sessionId} />
    <p className="dt-hint">{t("session.usage.scope")}</p>
  </>;
}

function LoopPanel({ sessionId, loop, onChange, toast }: { sessionId: string; loop: LoopView | null; onChange: () => void; toast: (t: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(loop?.instruction ?? "");
  const [mode, setMode] = useState<"interval" | "dynamic">(loop?.mode ?? "interval");
  const [minutes, setMinutes] = useState(String(loop?.interval_seconds ? Math.round(loop.interval_seconds / 60) : 10));
  const [maxRuns, setMaxRuns] = useState(loop?.max_runs ? String(loop.max_runs) : "");
  async function action(a: string) {
    try {
      await api.post(`/api/sessions/${sessionId}/loop/action`, { action: a });
      toast(t("session.loop.action", { action: a }));
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function save() {
    if (!text.trim()) return;
    try {
      await api.post(`/api/sessions/${sessionId}/loop`, { instruction: text.trim(), mode, interval_minutes: mode === "interval" ? Math.max(1, Number(minutes) || 10) : null, max_runs: maxRuns.trim() ? Math.max(1, Number(maxRuns) || 1) : null, start_now: true });
      toast(t(loop ? "session.loop.updated" : "session.loop.started"));
      setEditing(false);
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  if (!loop && !editing) {
    return (
      <div className="dt-empty">
        <span className="grow">{t("session.loop.none")}</span>
        <button className="btn small" onClick={() => setEditing(true)}>{t("session.loop.add")}</button>
      </div>
    );
  }
  return (
    <div className="loop-panel">
      {loop && !editing && (
        <>
          <div className="sub loop-instruction">{loop.instruction}</div>
          <div className="sub">
            {loop.status === "active" && loop.next_run_at
              ? t("session.loop.next", { time: clock(loop.next_run_at) })
              : loop.status === "active"
                ? t("session.loop.dynamic")
                : `${statusWord(loop.status)}${loop.stop_reason || loop.pause_note ? `: ${loop.stop_reason || loop.pause_note}` : ""}`}
            {loop.last_reason ? t("session.loop.lastreason", { reason: loop.last_reason }) : ""}
          </div>
          <div className="btnrow" style={{ marginTop: 6 }}>
            {loop.status === "active" ? <button className="btn small" onClick={() => action("pause")}>{t("common.pause")}</button> : <button className="btn small primary" onClick={() => action("resume")}>{t("common.resume")}</button>}
            {loop.status === "active" && <button className="btn small" onClick={() => action("run")}>{t("common.runnow")}</button>}
            <button className="btn small" onClick={() => setEditing(true)}>{t("session.loop.edit")}</button>
            {loop.status !== "stopped" && loop.status !== "done" && <button className="btn small danger" onClick={() => action("stop")}>{t("session.loop.stop")}</button>}
            <button className="btn small danger" onClick={async () => { if (await confirmAsync(t("session.loop.remove.title"))) action("remove"); }}>{t("session.loop.remove")}</button>
          </div>
        </>
      )}
      {editing && (
        <>
          <textarea className="field" rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder={t("session.loop.placeholder")} />
          <div className="composer-row">
            <select className="field" value={mode} onChange={(e) => setMode(e.target.value as "interval" | "dynamic")}>
              <option value="interval">{t("newagent.loop.interval")}</option>
              <option value="dynamic">{t("loop.selfpaced")}</option>
            </select>
            {mode === "interval" && <input className="field" type="number" min={1} style={{ maxWidth: 110 }} value={minutes} onChange={(e) => setMinutes(e.target.value)} aria-label={t("newagent.loop.minutes")} />}
            <input className="field" type="number" min={1} style={{ maxWidth: 130 }} placeholder={t("newagent.loop.max")} value={maxRuns} onChange={(e) => setMaxRuns(e.target.value)} aria-label={t("newagent.loop.max")} />
          </div>
          <div className="btnrow" style={{ marginTop: 6 }}>
            <button className="btn small primary" onClick={save} disabled={!text.trim()}>{t(loop ? "session.loop.save" : "session.loop.start")}</button>
            <button className="btn small" onClick={() => setEditing(false)}>{t("common.cancel")}</button>
          </div>
        </>
      )}
    </div>
  );
}

// ── side panels: usage, cron, attachments ─────────────────────────────────────────────────

// The section is titled by the provider alone and the plan is the badge beside it. The title used
// to carry a plan too ("Claude · Max") for some providers and not for others ("opencode"), and the
// Claude one then said its plan twice.
const PROVIDER_LABEL: Record<string, string> = { codex: "Codex", claude: "Claude", grok: "Grok", opencode: "OpenCode Go", openrouter: "OpenRouter", deepseek: "DeepSeek" };

function providerLabel(id: string): string {
  return PROVIDER_LABEL[id] ?? (id ? id[0].toUpperCase() + id.slice(1) : id);
}

/** What the session's provider has left: a subscription's windows, or the day's metered spend and balance. */
function ProviderUsageCard({ ids, provider, usage }: { ids: string; provider: string; usage: ProviderUsage | null }) {
  const sub = usage?.subscription;
  const today = usage?.today ?? {};
  return (
    <Section ids={ids} id="provider" icon="bolt" label={providerLabel(provider)} aside={<>
      {sub?.plan && <span className="badge">{planName(sub.plan)}</span>}
      {sub?.limit_reached && <span className="badge bad">{t("usage.limitreached")}</span>}
    </>}>
      {!usage && <div className="dt-hint">…</div>}
      {sub && !sub.logged_in && <div className="dt-hint">{t("usage.notloggedin")}</div>}
      {sub?.error && <Note tone="bad">{sub.error}</Note>}
      {(sub?.windows ?? []).map((w) => (
        <Meter key={w.name} label={w.name} percent={w.used_percent} tone={quotaTone(w.used_percent)} data={{ quota: w.name }}
          detail={w.resets_at ? <>{t("details.quota.resets")} <Until at={w.resets_at} /></> : undefined} />
      ))}
      {usage && (
        <div className="dt-hint dt-provider-today">
          {t("session.provider.today", { calls: plural("usage.calls", today.calls ?? 0), in: fmtTok(today.input_tokens), out: fmtTok(today.output_tokens) })}
          {!sub && ` · ${fmtUsd(today.cost_usd)}`}
          {usage.balance !== undefined && usage.balance !== null && t("session.provider.balance", { sum: fmtUsd(usage.balance) })}
        </div>
      )}
    </Section>
  );
}

/** One scheduled task of the session: cadence, next run, and the two things one does with it. */
function ScheduleRow({ sc, sessionId, onAction }: { sc: Schedule; sessionId: string; onAction: (sc: Schedule, action: "run" | "delete") => void }) {
  const where = t(sc.kind === "message" ? "session.sched.reminder" : sc.kind === "lazy" ? "session.sched.lazy" : sc.run_in === "self" || sc.target_session === sessionId ? "session.sched.here" : "session.sched.own");
  return (
    <div className="sched-row" title={sc.prompt}>
      <div className="grow" style={{ minWidth: 0 }}>
        <div className="sched-name">
          {!sc.enabled && <span title={t("session.sched.off")}>⏸ </span>}
          {sc.name} <span className="badge" title={sc.kind === "lazy" ? t("session.sched.lazy.title") : undefined}>{where}</span>
          {sc.active_session_id === sessionId && <span className="live-dot" title={t("sched.running")} />}
        </div>
        <div className="sub sched-when">
          {sc.cron ? t("session.sched.cron", { cron: sc.cron }) : t("session.sched.once", { when: shortDateTime(sc.run_at) })}
          {sc.next_run_at && sc.enabled ? t("session.sched.next", { when: shortDateTime(sc.next_run_at) }) : ""}
          {sc.last_run_at ? t("session.sched.last", { t: timeAgo(sc.last_run_at) }) : ""}
          {sc.failure_count > 0 && <span style={{ color: "var(--bad)" }}>{t("session.sched.failed", { n: sc.failure_count })}</span>}
        </div>
      </div>
      <button className="iconbtn small" onClick={() => onAction(sc, "run")} title={t("common.runnow")} aria-label={t("common.runnow")}><Icon name="play" size={14} /></button>
      <button className="iconbtn small" onClick={() => onAction(sc, "delete")} title={t("common.delete")} aria-label={t("common.delete")}><Icon name="trash" size={14} /></button>
    </div>
  );
}

/** The time each tool took over the session, folded under its total. */
function ToolTiming({ sessionId }: { sessionId: string }) {
  const [rows, setRows] = useState<{ name: string; calls: number; errors: number; total_ms: number; mean_ms: number }[]>([]);
  useEffect(() => {
    api.get<{ items: { name: string; calls: number; errors: number; total_ms: number; mean_ms: number }[] }>(`/api/sessions/${sessionId}/tools/timing`).then((r) => setRows(Array.isArray(r?.items) ? r.items : [])).catch(() => setRows([]));
  }, [sessionId]);
  if (!rows.length) return null;
  const total = rows.reduce((a, r) => a + r.total_ms, 0);
  return (
    <details className="tool-timing sub">
      <summary>{t("session.tooltime", { n: Math.round(total / 1000) })}</summary>
      {rows.map((r) => <div className="dt-row" key={r.name}>
        <span className="grow">{r.name}</span>
        <span>{t("session.tooltime.row", { n: Math.round(r.total_ms / 1000), c: r.calls, e: r.errors })}</span>
      </div>)}
    </details>
  );
}

type McpServer = { name: string; description: string; connected: boolean; error: string | null; tools: string[]; state: "disabled" | "connecting" | "ready" | "auth_required" | "unavailable" | "draining"; catalog_revision: number; last_error_code: string | null; in_flight: number };

function McpPanel({ sessionId, toast }: { sessionId: string; toast: (t: string) => void }) {
  const [data, setData] = useState<{ enabled: string[]; servers: McpServer[] } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(() => {
    // An older bot has no MCP route and answers with something else: that is "no servers", not a broken tab.
    api
      .get<{ enabled: string[]; servers: McpServer[] }>(`/api/sessions/${sessionId}/mcp`)
      .then((d) => setData({ enabled: Array.isArray(d?.enabled) ? d.enabled : [], servers: Array.isArray(d?.servers) ? d.servers : [] }))
      .catch((e) => toast(errorText(e)));
  }, [sessionId, toast]);
  useEffect(load, [load]);
  async function toggle(server: string, enabled: boolean) {
    setBusy(server);
    try {
      setData(await api.put(`/api/sessions/${sessionId}/mcp`, { server, enabled }));
      toast(t("session.mcp.toggled", { name: server, state: t(enabled ? "session.mcp.enabled" : "session.mcp.disabled") }));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(null);
    }
  }
  if (!data) return <div className="empty">…</div>;
  if (data.servers.length === 0) return <div className="empty">{t("session.mcp.none")}</div>;
  return (
    <div className="dt-mcp">
      <p className="dt-hint">{t("session.mcp.sub")}</p>
      {data.servers.map((s) => {
        const on = data.enabled.includes(s.name);
        return (
          <div key={s.name} className="dt-server" data-mcp-server={s.name}>
            <div className="dt-server-head">
              <span className="dt-server-name">{s.name}</span>
              <span className="dt-state" data-tone={MCP_TONE[s.state] ?? ""}>
                {t(`session.mcp.state.${s.state}`)}{s.in_flight > 0 ? ` · ${t("session.mcp.inflight", { n: s.in_flight })}` : ""}
              </span>
              <span className="grow" />
              <Switch checked={on} disabled={busy === s.name} label={s.name} onChange={(next) => void toggle(s.name, next)} />
            </div>
            <div className={`dt-hint${s.description ? "" : " quiet"}`}>{s.description || t("session.mcp.nodesc")}</div>
            {s.error && <Note tone="bad">{t("session.mcp.error", { error: s.error }).replace(/^ · /, "")}</Note>}
            {s.tools.length > 0 && <div className="dt-tags">{s.tools.map((tool) => <span key={tool} className="dt-tag mono">{tool}</span>)}</div>}
          </div>
        );
      })}
    </div>
  );
}

/** A server's state as a coloured dot: ready is good, a passing state is amber, one the operator must fix is red. */
const MCP_TONE: Partial<Record<McpServer["state"], string>> = { ready: "ok", connecting: "attn", draining: "attn", auth_required: "bad", unavailable: "bad" };
