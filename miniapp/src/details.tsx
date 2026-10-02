// The Details tab: what the session is and what it is set to, in flat sections — a 12 px label, a
// value or a row or two, a hairline between sections. It is the old aside's cards and the "Session
// info" sheet in one place; the right panel hosts it on a desktop and a full sheet on a phone.

import { useCallback, useEffect, useRef, useState } from "react";
import { api, LoopView, ProviderUsage, Schedule, SessionDetail } from "./api";
import { projectPath } from "./folders";
import { Dot, ServiceRow, ToolPicker, copyText, fmtInt, fmtUsd, loopLabel, statusWord, timeAgo } from "./ui/components";
import { readLayout, writeLayout } from "./layout";
import { clock, planName, relTimeLong, shortDateTime, untilShort } from "./format";
import { Icon } from "./icons";
import { confirmAsync, errorText, fmtTok } from "./ui";
import { plural, t } from "./i18n";
import { SessionToolGroups } from "./toolgroupsview";

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

export function Section({ ids, id, label, children, className, aside }: { ids: string; id: string; label: string; children: React.ReactNode; className?: string; aside?: React.ReactNode }) {
  const [open, setOpen] = useState(() => readLayout(`details.${id}`) !== "closed");
  return (
    <details className={`dt-section ${className ?? ""}`} id={`${ids}-info-${id}`} open={open} onToggle={(e) => { const next = e.currentTarget.open; setOpen(next); writeLayout(`details.${id}`, next ? "open" : "closed"); }}>
      <summary className="dt-label" onClick={(e) => {
        // Persist at the click, before a navigation can discard the native toggle event's task.
        e.preventDefault();
        setOpen(!open);
        writeLayout(`details.${id}`, open ? "closed" : "open");
      }}>
        <span>{label}</span>
        {aside && <span className="dt-aside">{aside}</span>}
      </summary>
      <div className="dt-content">{children}</div>
    </details>
  );
}

export function SessionDetails({ ids, id, role = "session", detail, busy, modes, schedules, provider, providerUsage, onOpen, toast, reload, on, focus }: SessionDetailsProps) {
  const container = useRef<HTMLDivElement>(null);
  const conductor = role !== "session";
  const usage = detail.usage;
  const ctxPct = detail.context && detail.context.window > 0 ? Math.round((100 * detail.context.tokens) / detail.context.window) : null;
  useEffect(() => {
    if (!focus || focus === "session") return;
    const timer = window.setTimeout(() => { const section = container.current?.querySelector<HTMLDetailsElement>(`#${CSS.escape(`${ids}-info-${focus}`)}`); if (section) { section.open = true; section.scrollIntoView({ block: "start" }); } }, 30);
    return () => window.clearTimeout(timer);
  }, [focus, ids]);
  return (
    <div ref={container} className="details">
      <Section ids={ids} id="session" label={t("session.card")}>
        <input className="field" defaultValue={detail.title} aria-label={t("session.title")} onBlur={(e) => on.rename(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
        <div className="dt-row">
          <span className="dt-key">{t("session.model")}</span>
          <button className="linkbtn grow truncate" onClick={on.openPicker} title={detail.model}>{detail.model || t("session.model.global")}</button>
          <button className="btn small" onClick={on.openPicker}>{t("session.model.change")}</button>
        </div>
        <div className="dt-row">
          <span className="dt-key">{t("session.mode")}</span>
          <select className="field grow" value={detail.mode || "default"} onChange={(e) => on.setMode(e.target.value)}>
            {["default", ...modes].map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </div>
      </Section>

      {detail.context && (
        <Section ids={ids} id="context" label={t("session.context")} aside={ctxPct !== null ? `${ctxPct}%` : undefined}>
          {ctxPct !== null && (
            <div className={`bar ${ctxPct >= 90 ? "bad" : ctxPct >= 60 ? "attn" : ""}`} title={t("session.ctx.title", { used: fmtInt(detail.context.tokens), window: fmtInt(detail.context.window) })} style={{ ["--v" as string]: Math.min(100, ctxPct) }}><i /></div>
          )}
          <div className="dt-row sub">
            <span>{fmtTok(detail.context.tokens)}{detail.context.window > 0 ? ` / ${fmtTok(detail.context.window)}` : ""}</span>
            <span className="dt-sep">·</span>
            <span>{t(detail.context.estimated ? "session.context.estimated" : "session.context.measured")}</span>
          </div>
          <p className="sub">{t("session.context.messages", { n: plural("session.context.count.messages", detail.context.messages), s: plural("session.context.count.summaries", detail.context.summaries), o: plural("session.context.count.originals", detail.context.operator_turns) })}</p>
          {detail.context.last_compaction?.at && (
            <p className="sub" data-last-compaction>{t("session.context.lastcompaction", { when: relTimeLong(detail.context.last_compaction.at), reason: detail.context.last_compaction.reason || "—" })}</p>
          )}
          {detail.context.breakdown && (
            <div className="context-breakdown">
              {(["instructions", "tools", "conversation", "attachments", "reserved_response"] as const).map((key) => (
                <div className="dt-row sub" key={key}>
                  <span className="grow">{t(`session.context.part.${key}`)}</span>
                  <span>{fmtTok(detail.context?.breakdown?.[key] ?? 0)}</span>
                </div>
              ))}
              <p className="sub">{t("session.context.breakdown.estimated")}</p>
            </div>
          )}
          {detail.context.recent_cache && (
            <p className="sub">{t("session.context.cache", { percent: detail.context.recent_cache?.hit_percent ?? 0, tokens: fmtTok(detail.context.recent_cache?.read_tokens ?? 0) })}</p>
          )}
          {!!detail.context.prefix_changed?.length && (
            <p className="sub">{t("session.context.prefix", { reasons: detail.context.prefix_changed?.map((reason: string) => t(`session.context.prefix.${reason}`)).join(", ") ?? "" })}</p>
          )}
          <div className="btnrow">
            <button className="btn small" onClick={on.compact} disabled={busy}><Icon name="compact" size={14} /> {t("session.compact")}</button>
          </div>
        </Section>
      )}

      <Section ids={ids} id="usage" label={t("session.usage")} aside={fmtUsd(detail.usage.usd)}>
        <p className="sub">{t("session.usage.scope")}</p>
        <div className="dt-row sub">
          {/* The arrow says which way the tokens went, and an arrow is neither translatable nor
              announced: the label carries the word and the glyph stays decoration. */}
          <span title={t("session.usage.in")} aria-label={`${fmtTok(detail.usage.i)} ${t("session.usage.in")}`}>{fmtTok(detail.usage.i)}<span aria-hidden>↑</span></span>
          <span title={t("session.usage.out")} aria-label={`${fmtTok(detail.usage.o)} ${t("session.usage.out")}`}>{fmtTok(detail.usage.o)}<span aria-hidden>↓</span></span>
          {!!detail.usage.ch && <span title={t("session.usage.cache")}>{t("panel.usage.cache", { n: fmtTok(detail.usage.ch) })}</span>}
          <span className="dt-sep">·</span>
          <span>{plural("usage.calls", detail.usage.c ?? 0)}</span>
        </div>
        {usage.c_today != null && (
          <div className="dt-row sub" data-usage-today>
            <span className="dt-key">{t("session.usage.today")}</span>
            <span className="grow">{t("session.usage.today.line", { usd: fmtUsd(usage.usd_today ?? 0), in: fmtTok(usage.i_today ?? 0), out: fmtTok(usage.o_today ?? 0), calls: plural("usage.calls", usage.c_today ?? 0) })}</span>
          </div>
        )}
        <ToolTiming sessionId={id} />
      </Section>

      {role !== "orchestrator" && <Section ids={ids} id="workspace" label={t("session.workspace")}>
        <button className="aside-row link" onClick={on.openFiles} title={detail.project ? projectPath(detail.project) : detail.workspace}>
          <Icon name="folder" size={16} />
          <span className="grow name">{detail.project ? t("session.aside.project", { name: detail.project.name }) : detail.workspace_own === false ? t("session.aside.workspace", { name: detail.workspace_name ?? "" }) : t("session.aside.own")}</span>
          <span className="sub">{t("panel.tab.files")}</span>
        </button>
        <div className="dt-row sub path" title={detail.workspace}>
          <span className="mono grow">{detail.project ? projectPath(detail.project) : detail.workspace}</span>
          {detail.project && (
            <span className="badge" title={t(detail.project.settings.snapshots ? "session.aside.snapshots.title" : "session.aside.nosnapshots.title")}>
              {t(detail.project.settings.snapshots ? "project.snapshots.badge" : "session.aside.nosnapshots")}
            </span>
          )}
        </div>
        {detail.workspace_sessions && detail.workspace_sessions.length > 0 && (
          <div className="dt-row sub">
            <span className="dt-key">{t("panel.shared")}</span>
            <span className="grow truncate">
              {detail.workspace_sessions.slice(0, 4).map((w, i) => (
                <span key={w.id}>
                  {i > 0 && ", "}
                  <button className="linkbtn" onClick={() => onOpen?.(w.id)} title={t("session.aside.sameworkspace")}>{w.title}</button>
                </span>
              ))}
            </span>
          </div>
        )}
        {!conductor && (
          <div className="dt-row sub prose">
            <span className="dt-key">{t("session.project")}</span>
            <span className="grow" title={projectPath(detail.project)}>
              {t("session.project.inside", { name: detail.project.name })}
            </span>
            <button className="linkbtn" onClick={on.move}>{t("session.project.move")}</button>
          </div>
        )}
      </Section>}

      {provider && <ProviderUsageCard ids={ids} provider={provider} usage={providerUsage} />}

      {!conductor && <Section ids={ids} id="loop" label={t("session.loop")} aside={detail.loop ? <span className={`badge loop ${detail.loop.status}`}>{statusWord(detail.loop.status).toLowerCase()}</span> : undefined}>
        {detail.loop && (
          <div className="sub">{loopLabel(detail.loop).replace(/^\S+ · /, "")}{detail.loop.next_run_at && detail.loop.status === "active" ? t("agents.loop.next", { t: untilShort(detail.loop.next_run_at) }) : ""}</div>
        )}
        <LoopPanel sessionId={id} loop={detail.loop ?? null} onChange={() => reload(true)} toast={toast} />
      </Section>}

      {schedules.length > 0 && (
        <Section ids={ids} id="cron" label={t("session.cron")} aside={t("session.cron.on", { n: schedules.filter((x) => x.enabled).length })}>
          {schedules.map((sc) => <ScheduleRow key={sc.id} sc={sc} sessionId={id} onAction={on.scheduleAction} />)}
        </Section>
      )}

      {detail.services && detail.services.length > 0 && (
        <Section ids={ids} id="services" label={t("session.services")} aside={t("session.services.running", { n: detail.services.filter((s) => s.status === "running").length })}>
          {detail.services.map((s) => (
            <ServiceRow key={s.name} s={s} sessionId={id} onChange={() => reload(true)} toast={toast} onLogs={(text) => on.showLog(`service ${s.name} · log`, text)} />
          ))}
        </Section>
      )}

      <Section ids={ids} id="mcp" label={t("session.mcp")}>
        <McpPanel sessionId={id} toast={toast} />
      </Section>

      <Section ids={ids} id="toolgroups" label={t("session.toolgroups")}>
        <SessionToolGroups sessionId={id} toast={toast} />
      </Section>

      {!conductor && <Section ids={ids} id="tools" label={t("session.tools")}>
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

      <Section ids={ids} id="brief" label={t("session.brief")}>
        <label className="field">{t("session.brief.label")}{detail.spawned_by ? t("session.brief.by", { id: detail.spawned_by }) : ""}</label>
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

      <Section ids={ids} id="spend" label={t("session.cap")} aside={t("settings.limits.spent", { sum: fmtUsd(detail.usage.usd) })}>
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

      <Section ids={ids} id="advanced" label={t("session.advanced")}>
        <div className="dt-row sub"><span className="dt-key">{t("session.id")}</span><button className="linkbtn mono" onClick={async () => toast((await copyText(id)) ? t("session.id.copied") : id)} title={t("common.copy")}>{id}</button></div>
        <div className="dt-row sub"><span className="dt-key">{t("session.workspace")}</span><button className="linkbtn mono truncate" onClick={async () => toast((await copyText(detail.workspace)) ? t("session.path.copied") : detail.workspace)} title={detail.workspace}>{detail.workspace_own === false ? detail.workspace_name : detail.workspace}</button></div>
        {detail.run_id && <div className="dt-row sub"><span className="dt-key">{t("session.runid")}</span><span className="mono">{detail.run_id}</span></div>}
        <div className="btnrow">
          <button className="btn small" onClick={on.exportMarkdown}><Icon name="download" size={14} /> {t("session.export")}</button>
        </div>
      </Section>

      {!conductor && <Section ids={ids} id="danger" label={t("session.danger")} className="danger">
        <div className="btnrow" style={{ marginTop: 0 }}>
          <button className="btn small danger" onClick={on.clearHistory} disabled={busy}><Icon name="trash" size={14} /> {t("session.clear.short")}</button>
          <button className="btn small danger" onClick={on.remove}><Icon name="trash" size={14} /> {t("session.delete.short")}</button>
        </div>
      </Section>}
    </div>
  );
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
      <div className="btnrow" style={{ marginTop: 0 }}>
        <span className="sub">{t("session.loop.none")}</span>
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

function resetIn(at: number | string | null | undefined): string {
  if (!at) return "";
  const d = typeof at === "number" ? new Date(at * 1000) : new Date(at);
  if (Number.isNaN(d.getTime())) return "";
  const mins = Math.max(0, Math.round((d.getTime() - Date.now()) / 60000));
  if (mins < 60) return t("fmt.min", { n: mins });
  return mins < 48 * 60 ? t("fmt.hour", { n: Math.round(mins / 60) }) : t("fmt.day", { n: Math.round(mins / 1440) });
}

/** What the session's provider has left: a subscription's windows, or the day's metered spend and balance. */
function ProviderUsageCard({ ids, provider, usage }: { ids: string; provider: string; usage: ProviderUsage | null }) {
  const sub = usage?.subscription;
  const today = usage?.today ?? {};
  return (
    <Section ids={ids} id="provider" label={providerLabel(provider)} aside={<>
      {sub?.plan && <span className="badge">{planName(sub.plan)}</span>}
      {sub?.limit_reached && <span className="badge" style={{ color: "var(--bad)" }}>{t("usage.limitreached")}</span>}
    </>}>
      {!usage && <div className="sub">…</div>}
      {sub && !sub.logged_in && <div className="sub">{t("usage.notloggedin")}</div>}
      {sub?.error && <div className="sub" style={{ color: "var(--bad)" }}>{sub.error}</div>}
      {(sub?.windows ?? []).map((w) => (
        <div key={w.name} className="quota">
          <div className="sub quota-line">
            <span className="grow">{w.name}</span>
            <span>{Math.round(w.used_percent)}%{w.resets_at ? t("session.provider.resets", { t: resetIn(w.resets_at) }) : ""}</span>
          </div>
          <div className="quota-bar">
            <i style={{ width: `${Math.min(100, Math.max(0, w.used_percent))}%`, background: w.used_percent >= 100 ? "var(--bad)" : w.used_percent >= 80 ? "var(--warn)" : "var(--ok)" }} />
          </div>
        </div>
      ))}
      {usage && (
        <div className="sub" style={{ marginTop: sub ? 6 : 0 }}>
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

/** A file waiting in the composer: a thumbnail for images, a glyph and the size for the rest. */
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
    <div>
      <div className="sub" style={{ marginBottom: 8 }}>{t("session.mcp.sub")}</div>
      {data.servers.map((s) => {
        const on = data.enabled.includes(s.name);
        return (
          <div key={s.name} className="card">
            <div className="row">
              <div className="grow">
                <div className="title">{s.name}</div>
                <div className="sub">{t(`session.mcp.state.${s.state}`)}{s.in_flight > 0 ? ` · ${t("session.mcp.inflight", { n: s.in_flight })}` : ""}</div>
                <div className="sub">{s.description || t("session.mcp.nodesc")}{s.error && t("session.mcp.error", { error: s.error })}</div>
                {s.tools.length > 0 && <div className="sub">{s.tools.join(", ")}</div>}
              </div>
              <button className={`btn small ${on ? "primary" : ""}`} disabled={busy === s.name} onClick={() => toggle(s.name, !on)}>
                {t(on ? "common.on" : "common.off")}
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
