// The Details tab: what the session is and what it is set to, as key and value in four groups — how it
// was launched, where it stands, what it spent, how full its context is — then the chat's own settings,
// one line each, which open in place of the list with a way back. It used to be seventeen identical
// rows with a chevron each, the name at one edge and its state at the other; the facts now read as a
// table, and only a line that opens something looks like it does. The context and the spend have
// tools of their own in the panel (their pages are here, exported); what one does to the session —
// rename it, move it, clear it, delete it — is in the session's menu. The parts a page is built from
// are in detailsparts.tsx; the right panel hosts the tab on a desktop and a full sheet on a phone.

import { OriginSection } from "./imports/ImportedChat";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, LoopView, ProviderUsage, Schedule, SessionDetail } from "./api";
import { projectPath } from "./folders";
import { ServiceRow, Switch, ToolPicker, fmtInt, fmtUsd, loopLabel, statusWord, timeAgo } from "./ui/components";
import { absTime, clock, duration, planName, relTimeLong, shortDateTime, shortModel, untilShort } from "./format";
import { Icon, type IconName } from "./icons";
import { CopyValue, Fact, Facts, Headline, Meter, Note, Stat, Stats, Until } from "./detailsparts";
import { contextSegments, contextTone, quotaTone } from "./detailsmodel";
import { confirmAsync, errorText, fmtTok } from "./ui";
import { plural, t } from "./i18n";
import { SessionToolGroups } from "./toolgroupsview";
import { ServerGrantButton, StandingGrants, type Standing } from "./servergrant";
import { McpQuickAdd } from "./mcpquickadd";
import { SessionSubagentModels } from "./subagentmodels";
import { SessionVisionModel, visionValue } from "./sessionvision";
import type { SubagentModelsView } from "./api";
import { HostMark } from "./runon";
import { DiskUsage } from "./diskusage";
import { MoveLive, useFolderMove } from "./project/moves";
import { useLayer } from "./ui/dialogs";
import { Select } from "./ui/primitives";

export type DetailsActions = {
  rename: (title: string) => void;
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
  /** Another tool of the panel: the context's and the spend's pages are tools, not sections here. */
  openTool: (tool: "context" | "usage") => void;
  /** The model picker, under the line that names the model. */
  openModel?: (anchor: HTMLElement) => void;
};

export type SessionDetailsProps = {
  ids: string;
  id: string;
  /** What the session is: an ordinary one, a project's orchestrator, or the main orchestrator. The
   *  two orchestrators are the same Details less what would break them or has no place beside
   *  them: a loop, the tool switches their role fixes, and (for a project's, which has no workspace
   *  of its own and reads every folder of the project from its Files tab) the workspace. */
  role?: "session" | "orchestrator" | "main";
  detail: SessionDetail;
  busy: boolean;
  schedules: Schedule[];
  provider: string;
  providerUsage: ProviderUsage | null;
  onOpen?: (id: string) => void;
  toast: (text: string) => void;
  reload: (quiet?: boolean) => void;
  on: DetailsActions;
  /** The section to open (a chip, the strip or a menu item named it); "session" is the list itself. */
  focus?: string | null;
  /** Changes with every request to open `focus`, so asking for the same section twice opens it twice. */
  focusKey?: number;
  /** The reasoning effort as a word, "" for a model without one. */
  effort?: string;
};

type PageId = "origin" | "provider" | "workspace" | "disk" | "loop" | "cron" | "services" | "mcp" | "grants" | "helpers" | "vision" | "toolgroups" | "tools" | "brief" | "spend" | "advanced";

/** One line of the inspector that opens a page: what it opens, its icon and name, and its state in a few words. */
type Prop = { id: PageId; icon: IconName; label: string; value?: ReactNode; title?: string; attn?: boolean; body: () => ReactNode };

/** One fact: a name and its value. With `onPress` it is a control (the model opens the picker, the
 *  cost opens the spend), drawn as the same line with a hover; with `prop` it is named for the checks
 *  and the keyboard like a section's line. */
type Fact = { key: string; label: string; value: ReactNode; title?: string; prop?: string; onPress?: (anchor: HTMLElement) => void; tone?: "attn" | "live" };

export function SessionDetails({ ids, id, role = "session", detail, busy, schedules, provider, providerUsage, onOpen, toast, reload, on, focus, focusKey, effort = "" }: SessionDetailsProps) {
  const container = useRef<HTMLDivElement>(null);
  const conductor = role !== "session";
  const sessionFolder = detail.project?.folders?.find((folder) => folder.id === detail.folder_id) ?? detail.project?.folders?.[0] ?? null;
  const folderMove = useFolderMove(sessionFolder?.id);
  const ctxPct = detail.context && detail.context.window > 0 ? Math.round((100 * detail.context.tokens) / detail.context.window) : null;
  // Bumped when the MCP page grants a server, so the standing grants reload beside it.
  const [grantsTick, setGrantsTick] = useState(0);
  const mcp = useMcp(id, toast, () => setGrantsTick((n) => n + 1));
  const grants = useStanding(id, grantsTick);
  const helpers = useHelperModels(id);
  const vision = visionValue(detail);
  const [page, setPage] = useState<PageId | null>(null);
  // The line a page was opened from, so Back puts the keyboard where it was.
  const from = useRef<PageId | null>(null);
  const open = (next: PageId | null) => {
    if (next) from.current = next;
    setPage(next);
  };

  const facts = launchFacts(detail, { effort, conductor, folder: role !== "orchestrator", moving: !!folderMove, onModel: on.openModel, onOpen: (page) => open(page) });
  const state = stateFacts(detail, busy);
  const spend = spendFacts(detail, () => on.openTool("usage"));
  const groups: { key: string; props: Prop[] }[] = [
    { key: "now", props: [
      ...(detail.imported ? [{ id: "origin" as const, icon: "fork" as IconName, label: t("imp.origin"), value: detail.imported.harness_name ?? detail.imported.harness, body: () => <OriginSection sessionId={id} origin={detail.imported!} toast={toast} onPulled={() => reload(true)} /> }] : []),
      ...(provider ? [{ id: "provider" as const, icon: "bolt" as IconName, label: providerLabel(provider), value: <ProviderSummary usage={providerUsage} />, body: () => <ProviderUsageBody usage={providerUsage} /> }] : []),
    ] },
    { key: "where", props: role === "orchestrator" ? [] : [
      { id: "disk", icon: "archive", label: t("disk.title"), body: () => <DiskUsage kind="session" id={id} toast={toast} /> },
    ] },
    { key: "own", props: [
      ...(!conductor ? [{ id: "loop" as const, icon: "loop" as IconName, label: t("session.loop"), value: detail.loop ? <span className={`badge loop ${detail.loop.status}`}>{statusWord(detail.loop.status).toLowerCase()}</span> : <span className="quiet">{t("details.prop.none")}</span>,
        body: () => <>
          {detail.loop && <div className="dt-hint">{loopLabel(detail.loop).replace(/^\S+ · /, "")}{detail.loop.next_run_at && detail.loop.status === "active" ? t("agents.loop.next", { t: untilShort(detail.loop.next_run_at) }) : ""}</div>}
          <LoopPanel sessionId={id} loop={detail.loop ?? null} onChange={() => reload(true)} toast={toast} />
        </> }] : []),
      ...(schedules.length > 0 ? [{ id: "cron" as const, icon: "calendar" as IconName, label: t("session.cron"), value: cronWord(schedules),
        body: () => <>{schedules.map((sc) => <ScheduleRow key={sc.id} sc={sc} sessionId={id} onAction={on.scheduleAction} />)}</> }] : []),
      ...(detail.services && detail.services.length > 0 ? [{ id: "services" as const, icon: "globe" as IconName, label: t("session.services"),
        value: <>{detail.services.some((s) => s.status === "running") && <span className="dt-dot ok" aria-hidden />}{t("session.services.running", { n: detail.services.filter((s) => s.status === "running").length })}</>,
        body: () => <>{detail.services!.map((s) => <ServiceRow key={s.name} s={s} sessionId={id} onChange={() => reload(true)} toast={toast} onLogs={(text) => on.showLog(`service ${s.name} · log`, text)} />)}</> }] : []),
    ] },
    { key: "uses", props: [
      { id: "mcp", icon: "plug", label: t("session.mcp"), attn: !!mcp.data?.servers.some((s) => s.error && mcp.data!.enabled.includes(s.name)),
        value: mcp.data ? (mcp.data.servers.length ? t("details.prop.of", { n: mcp.data.enabled.filter((name) => mcp.data!.servers.some((s) => s.name === name)).length, of: mcp.data.servers.length }) : <span className="quiet">{t("details.prop.none")}</span>) : undefined,
        body: () => <McpPanel sessionId={id} toast={toast} mcp={mcp} /> },
      // What stands granted in this session ("Allow similar", "Allow all from a server"), each with its time
      // left and revocable; reloaded when the MCP page grants a server.
      { id: "grants", icon: "shield", label: t("grant.section"),
        value: grants === null ? undefined : grants.length ? String(grants.length) : <span className="quiet">{t("details.prop.none")}</span>,
        body: () => <>
          <p className="dt-hint">{t("grant.section.sub")}</p>
          <StandingGrants sessionId={id} toast={toast} refresh={grantsTick} />
        </> },
      // Which models this session's helpers may run on; its own choice, or the default from Settings.
      ...(helpers ? [{ id: "helpers" as const, icon: "spawn" as IconName, label: t("subm.title"),
        value: helpers.inherited ? <span className="quiet">{t("subm.inherited")}</span> : helpers.mode === "chosen" ? t("details.prop.models", { n: helpers.presets.length }) : t(`subm.mode.${helpers.mode}`),
        body: () => <SessionSubagentModels sessionId={id} toast={toast} /> }] : []),
      // The model that looks at this session's pictures: its own choice, or the global one from Settings.
      { id: "vision", icon: "eye", label: t("details.vision"),
        value: <span className={`truncate${vision.own ? "" : " quiet"}`}>{vision.text}</span>,
        body: () => <SessionVisionModel sessionId={id} detail={detail} toast={toast} onSaved={() => reload(true)} /> },
      { id: "toolgroups", icon: "skill", label: t("session.toolgroups"), body: () => <SessionToolGroups sessionId={id} toast={toast} /> },
      ...(!conductor ? [{ id: "tools" as const, icon: "wrench" as IconName, label: t("session.tools"),
        value: (detail.tools_off?.length ?? 0) > 0 ? t("details.prop.off", { n: detail.tools_off!.length }) : <span className="quiet">{t("details.prop.allon")}</span>,
        body: () => <ToolPicker off={detail.tools_off ?? []} note={t("session.tools.note")} onChange={async (off) => {
          try {
            await api.post(`/api/sessions/${id}/tools`, { tools_off: off });
            reload(true);
          } catch (e) {
            toast(errorText(e));
          }
        }} /> }] : []),
      { id: "brief", icon: "journal", label: t("session.brief"), title: detail.brief ?? undefined,
        value: detail.brief ? <span className="truncate">«{detail.brief.slice(0, 60)}»</span> : <span className="quiet">{t("details.prop.none")}</span>,
        body: () => <BriefBody id={id} detail={detail} reload={reload} toast={toast} /> },
      { id: "spend", icon: "flag", label: t("session.cap"), value: detail.usd_cap != null ? fmtUsd(detail.usd_cap) : <span className="quiet">{t("details.prop.none")}</span>,
        body: () => <CapBody id={id} detail={detail} reload={reload} toast={toast} /> },
    ] },
    { key: "session", props: [
      { id: "advanced", icon: "settings", label: t("session.advanced"), value: <span className="mono">{id.length > 10 ? `${id.slice(0, 10)}…` : id}</span>, title: id,
        body: () => <>
          <Facts>
            <Fact label={t("session.id")}><CopyValue value={id} shown={id} toast={toast} /></Fact>
            <Fact label={t("session.workspace")}><CopyValue value={detail.workspace} shown={detail.workspace_own === false ? detail.workspace_name : undefined} max={28} toast={toast} /></Fact>
            {detail.run_id && <Fact label={t("session.runid")} mono>{detail.run_id}</Fact>}
          </Facts>
          <div className="btnrow">
            <button className="btn small" onClick={on.exportMarkdown}><Icon name="download" size={14} /> {t("session.export")}</button>
          </div>
        </> },
    ] },
  ];
  // The folder's page opens from its fact in the launch group, not from a line of its own.
  const workspacePage: Prop = { id: "workspace", icon: "folder", label: t("session.workspace"), title: detail.project ? projectPath(detail.project) : detail.workspace,
    body: () => <WorkspaceBody detail={detail} conductor={conductor} folderMove={folderMove} onOpen={onOpen} on={on} /> };
  const all = [...groups.flatMap((g) => g.props), ...(role === "orchestrator" ? [] : [workspacePage])];
  const shown = page ? all.find((p) => p.id === page) ?? null : null;

  // A chip, the strip or a menu item asked for a section: open it; "session" asks for the list.
  useEffect(() => {
    if (!focus) return;
    if (focus === "session") setPage(null);
    else if (all.some((p) => p.id === focus)) open(focus as PageId);
    // `all` is rebuilt each render; the request is what matters, and `focusKey` repeats it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, focusKey]);
  // A section that went away under an open page (the last schedule deleted) leaves the page for the list.
  useEffect(() => {
    if (page && !shown) setPage(null);
  }, [page, shown]);
  // Back to the list puts the keyboard on the line the page was opened from.
  useEffect(() => {
    if (page) {
      container.current?.querySelector<HTMLElement>(".dt-drillhead .dt-back")?.focus({ preventScroll: true });
    } else if (from.current) {
      container.current?.querySelector<HTMLElement>(`[data-prop="${from.current}"]`)?.focus({ preventScroll: true });
    }
  }, [page]);

  if (shown) {
    return (
      <div ref={container} className="details inspector">
        <Drill ids={ids} prop={shown} onBack={() => open(null)} container={container} />
      </div>
    );
  }
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
    const rows = Array.from(container.current?.querySelectorAll<HTMLElement>("[data-prop]") ?? []);
    const i = rows.indexOf(document.activeElement as HTMLElement);
    const next = e.key === "Home" ? 0 : e.key === "End" ? rows.length - 1 : i < 0 ? 0 : Math.max(0, Math.min(rows.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)));
    rows[next]?.focus();
    e.preventDefault();
  };
  return (
    <div ref={container} className="details inspector" onKeyDown={onKey}>
      <FactGroup ids={ids} id="launch" facts={facts} />
      <FactGroup ids={ids} id="state" facts={state} />
      <FactGroup ids={ids} id="spend" facts={spend} />
      {detail.context && <ContextLine ids={ids} context={detail.context} percent={ctxPct} busy={busy} onCompact={on.compact} onMore={() => on.openTool("context")} />}
      {groups.filter((g) => g.props.length > 0).map((g) => (
        <section key={g.key} className="dt-props" aria-labelledby={`${ids}-grp-${g.key}`}>
          <h3 className="dt-grp" id={`${ids}-grp-${g.key}`}>{t(`details.group.${g.key}`)}</h3>
          {g.props.map((p) => (
            <button key={p.id} type="button" className="dt-prop" id={`${ids}-info-${p.id}`} data-prop={p.id} title={p.title} onClick={() => open(p.id)}>
              <span className="dt-prop-k"><Icon name={p.icon} size={16} /><span className="dt-prop-name">{p.label}</span></span>
              {p.attn && <span className="dt-prop-attn" title={t("details.prop.attn")} aria-label={t("details.prop.attn")}><Icon name="alert" size={12} /></span>}
              <span className="dt-prop-v">{p.value}</span>
              <Icon name="forward" size={14} />
            </button>
          ))}
        </section>
      ))}
    </div>
  );
}

/** A group of facts under its label: a 150 px column of names, the values beside them. */
function FactGroup({ ids, id, facts }: { ids: string; id: string; facts: Fact[] }) {
  if (facts.length === 0) return null;
  return (
    <section className="dt-facts" aria-labelledby={`${ids}-grp-${id}`} data-facts={id}>
      <h3 className="dt-grp" id={`${ids}-grp-${id}`}>{t(`details.group.${id}`)}</h3>
      {facts.map((f) => {
        const body = <><span className="dt-kv-k">{f.label}</span><span className={`dt-kv-v ${f.tone ?? ""}`}>{f.value}</span></>;
        return f.onPress
          ? <button key={f.key} type="button" className="dt-kv press" data-prop={f.prop} data-fact={f.key} title={f.title} onClick={(e) => f.onPress!(e.currentTarget)}>{body}</button>
          : <div key={f.key} className="dt-kv" data-fact={f.key} title={f.title}>{body}</div>;
      })}
    </section>
  );
}

/** How the session was launched: the model and its effort, what it may do without asking, the
 *  project, the folder and the branch it works on. */
/** `folder`: a project's orchestrator has no folder of its own; it reads every folder of the project from Files. */
function launchFacts(detail: SessionDetail, { effort, conductor, folder, moving, onModel, onOpen }: { effort: string; conductor: boolean; folder: boolean; moving: boolean; onModel?: (anchor: HTMLElement) => void; onOpen: (page: PageId) => void }): Fact[] {
  const model = detail.fallback?.to ?? detail.model;
  const vendor = detail.effective_provider || detail.provider;
  return [
    { key: "model", label: t("details.fact.model"), value: <span className="truncate">{model ? [shortModel(model, 40), vendor ? providerLabel(vendor) : ""].filter(Boolean).join(" · ") : t("session.model.global")}</span>, title: model, prop: "model", onPress: onModel },
    ...(effort ? [{ key: "effort", label: t("details.fact.effort"), value: effort }] : []),
    { key: "access", label: t("details.fact.access"), value: t(detail.bypass ? "composer.bypass" : "details.fact.access.ask"), tone: detail.bypass ? "attn" as const : undefined },
    ...(!conductor && detail.project && !detail.project.system ? [{ key: "project", label: t("session.project"), value: <span className="truncate">{detail.project.name}</span> }] : []),
    ...(!folder ? [] : [{ key: "workspace", label: t("details.fact.folder"), value: <>{moving && <span className="badge attn">{t("details.prop.moving")}</span>}<span className="truncate mono">{shortPath(detail.workspace)}</span><HostMark env={detail.env} /></>, title: detail.workspace, prop: "workspace", onPress: () => onOpen("workspace") }]),
    ...(detail.branch ? [{ key: "branch", label: t("details.fact.branch"), value: <span className="truncate mono">{detail.branch}</span> }] : []),
  ];
}

/** Where the session stands: its status, when the run on it began and for how long. */
function stateFacts(detail: SessionDetail, busy: boolean): Fact[] {
  const since = detail.run_started_at;
  return [
    { key: "status", label: t("details.fact.status"), value: statusWord(detail.status), tone: busy ? "live" : undefined },
    ...(since ? [{ key: "started", label: t("details.fact.started"), value: shortDateTime(since), title: absTime(since) }, { key: "running", label: t("details.fact.running"), value: <Elapsed since={since} /> }] : []),
    ...(detail.context ? [{ key: "messages", label: t("details.fact.messages"), value: fmtInt(detail.context.messages), title: t("details.context.messages") }] : []),
  ];
}

/** What it spent: tokens in and out, how much of what went in the cache served, and the sum, which
 *  opens the spend's own page. */
function spendFacts(detail: SessionDetail, openUsage: () => void): Fact[] {
  const usage = detail.usage;
  const cached = usage.i && usage.ch ? Math.round((100 * usage.ch) / usage.i) : null;
  return [
    { key: "in", label: t("details.usage.in"), value: t("details.fact.tokens", { n: fmtTok(usage.i) }) },
    { key: "out", label: t("details.usage.out"), value: t("details.fact.tokens", { n: fmtTok(usage.o) }) },
    ...(cached !== null ? [{ key: "cache", label: t("details.fact.cache"), value: `${cached}%` }] : []),
    { key: "cost", label: t("details.fact.cost"), value: <>{fmtUsd(usage.usd)}<span className="quiet">{plural("usage.calls", usage.c ?? 0)}</span></>, prop: "usage", onPress: openUsage },
  ];
}

/** The context as a bar under its group's label, its figures, and the compaction beside them. The page
 *  with what fills it is the Context tool. */
function ContextLine({ ids, context, percent, busy, onCompact, onMore }: { ids: string; context: ContextView; percent: number | null; busy: boolean; onCompact: () => void; onMore: () => void }) {
  const tone = percent !== null ? contextTone(percent) : null;
  return (
    <section className="dt-facts dt-context" aria-labelledby={`${ids}-grp-context`} data-facts="context">
      <h3 className="dt-grp" id={`${ids}-grp-context`}>{t("session.context")}</h3>
      {percent !== null && <div className={`dt-context-bar ${tone ?? ""}`} aria-hidden><i style={{ width: `${Math.min(100, percent)}%` }} /></div>}
      <div className="dt-context-line">
        <button type="button" className="dt-context-more" data-prop="context" onClick={onMore} title={context.window > 0 ? t("session.ctx.title", { used: fmtInt(context.tokens), window: fmtInt(context.window) }) : undefined}>
          {context.window > 0 ? t("details.context.line", { used: fmtTok(context.tokens), window: fmtTok(context.window), n: percent ?? 0 }) : t("details.context.used", { used: fmtTok(context.tokens) })}
        </button>
        <button type="button" className="dt-context-compact" onClick={onCompact} disabled={busy}>{t("details.context.compact")}</button>
      </div>
    </section>
  );
}

/** A run's length, drawn again every quarter minute; nothing is fetched for it. */
export function Elapsed({ since }: { since: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15000);
    return () => window.clearInterval(timer);
  }, []);
  return <>{elapsedWord(now - Date.parse(since))}</>;
}

/** "6 мин", "1 ч 12 мин": a run's length to the minute, seconds only in its first minute. */
export function elapsedWord(ms: number): string {
  if (!Number.isFinite(ms)) return "";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return t("fmt.dur.s", { n: s });
  const m = Math.floor(s / 60);
  if (m < 60) return t("fmt.dur.m", { n: m });
  return `${t("fmt.dur.h", { n: Math.floor(m / 60) })}${m % 60 ? ` ${t("fmt.dur.m", { n: m % 60 })}` : ""}`;
}

/** A folder as its last two parts: the start of a long path says nothing the end does not. */
function shortPath(path: string): string {
  const parts = (path || "").split("/").filter(Boolean);
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

/** A section opened in place of the list: the way back, where it is, and the section itself. Escape goes
 *  back too, before it would close the panel, unless the reader is typing outside the panel. */
function Drill({ ids, prop, onBack, container }: { ids: string; prop: Prop; onBack: () => void; container: React.RefObject<HTMLDivElement | null> }) {
  useLayer(() => {
    const active = document.activeElement as HTMLElement | null;
    const typing = !!active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.isContentEditable);
    if (typing && !container.current?.contains(active)) return;
    onBack();
  });
  return (
    <section className="dt-drill" id={`${ids}-info-${prop.id}`} data-page={prop.id} aria-labelledby={`${ids}-page-${prop.id}`}>
      <div className="dt-drillhead">
        <button type="button" className="iconbtn small dt-back" onClick={onBack} aria-label={t("details.back")} title={t("details.back")}><Icon name="back" size={16} /></button>
        <button type="button" className="dt-crumb" onClick={onBack}>{t("panel.tab.details")}</button>
        <span className="dt-crumb-sep" aria-hidden>›</span>
        <h3 id={`${ids}-page-${prop.id}`}>{prop.label}</h3>
      </div>
      <div className="dt-page">{prop.body()}</div>
    </section>
  );
}

/** A short bar inside a line of the list: a context's fill or a quota window's use. */
function Mini({ percent, kind, tone }: { percent: number; kind?: "context"; tone?: string | null }) {
  return <span className={`dt-mini ${kind ?? ""} ${tone ?? ""}`} aria-hidden><i style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} /></span>;
}

function workspaceWord(detail: SessionDetail): string {
  return detail.project ? t("session.aside.project", { name: detail.project.name }) : detail.workspace_own === false ? t("session.aside.workspace", { name: detail.workspace_name ?? "" }) : t("session.aside.own");
}

function cronWord(schedules: Schedule[]): string {
  const on = schedules.filter((x) => x.enabled);
  const next = on.map((x) => x.next_run_at).filter((x): x is string => !!x).sort()[0];
  return t("session.cron.on", { n: on.length }) + (next ? t("details.prop.next", { when: clock(next) }) : "");
}

/** Where the session works, the other chats in the same folder, and a folder being carried between the
 *  host and the container. Moving the chat or its folder is in the session's menu. */
function WorkspaceBody({ detail, conductor, folderMove, onOpen, on }: { detail: SessionDetail; conductor: boolean; folderMove: ReturnType<typeof useFolderMove>; onOpen?: (id: string) => void; on: DetailsActions }) {
  return <>
    <button className="aside-row link dt-workspace" onClick={on.openFiles} title={detail.project ? projectPath(detail.project) : detail.workspace}>
      <Icon name="folder" size={16} />
      <span className="grow name">{workspaceWord(detail)}</span>
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
    {folderMove && <MoveLive job={folderMove} />}
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
    {!conductor && <p className="dt-hint">{t("details.workspace.moves")}</p>}
  </>;
}

function BriefBody({ id, detail, reload, toast }: { id: string; detail: SessionDetail; reload: (quiet?: boolean) => void; toast: (text: string) => void }) {
  return <>
    <label className="field dt-hint" htmlFor={`brief-${id}`}>{t("session.brief.label")}{detail.spawned_by ? t("session.brief.by", { id: detail.spawned_by }) : ""}</label>
    <textarea
      id={`brief-${id}`}
      className="field"
      rows={6}
      defaultValue={detail.brief ?? ""}
      placeholder={t("session.brief.placeholder")}
      onBlur={async (e) => {
        const brief = e.target.value.trim();
        if (brief === (detail.brief ?? "")) return;
        try {
          await api.post(`/api/sessions/${id}/brief`, { brief });
          reload();
        } catch (err) {
          toast(errorText(err));
        }
      }}
    />
  </>;
}

function CapBody({ id, detail, reload, toast }: { id: string; detail: SessionDetail; reload: (quiet?: boolean) => void; toast: (text: string) => void }) {
  return <>
    <p className="dt-hint">{t("settings.limits.spent", { sum: fmtUsd(detail.usage.usd) })}</p>
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
          reload();
        } catch (err) {
          toast(errorText(err));
        }
      }}
    />
  </>;
}

type ContextView = NonNullable<SessionDetail["context"]>;

/** The context: how full the window is, what fills it, what the cache reused, and when it was last
 *  compacted beside the button that compacts it. Each was a sentence of its own before. */
export function ContextBody({ context, percent, busy, onCompact }: { context: ContextView; percent: number | null; busy: boolean; onCompact: () => void }) {
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

/** What the session spent, as one table: in, of it from the cache, out, calls and the tools' time, in
 *  total and today. It used to be the sum as a headline, today's sum beside it, and four tiles, each
 *  with today's share under it — three places for the same few numbers. */
export function UsageBody({ sessionId, usage }: { sessionId: string; usage: SessionDetail["usage"] }) {
  const today = usage.c_today != null;
  const timing = useToolTiming(sessionId);
  const total = timing.reduce((sum, row) => sum + row.total_ms, 0);
  const row = (label: string, all: ReactNode, day: ReactNode, title?: string) => (
    <tr title={title}>
      <th scope="row">{label}</th>
      <td>{all}</td>
      {today && <td>{day}</td>}
    </tr>
  );
  return <>
    <Headline value={fmtUsd(usage.usd)} caption={t("details.usage.total")} side={today ? (
      <span className="dt-today" data-usage-today>
        <span className="dt-today-label">{t("session.usage.today")}</span>
        <b>{fmtUsd(usage.usd_today ?? 0)}</b>
      </span>
    ) : undefined} />
    <table className="dt-table" data-usage-table>
      <thead>
        <tr><td /><th scope="col">{t("details.usage.col.total")}</th>{today && <th scope="col">{t("details.usage.col.today")}</th>}</tr>
      </thead>
      <tbody>
        {row(t("details.usage.in"), fmtTok(usage.i), fmtTok(usage.i_today ?? 0), t("session.usage.in"))}
        {row(t("details.usage.cachepart"), fmtTok(usage.ch ?? 0), "—", t("session.usage.cache"))}
        {row(t("details.usage.out"), fmtTok(usage.o), fmtTok(usage.o_today ?? 0), t("session.usage.out"))}
        {row(t("details.usage.calls"), fmtInt(usage.c ?? 0), fmtInt(usage.c_today ?? 0))}
        {timing.length > 0 && row(t("details.usage.tooltime"), duration(total), "—")}
      </tbody>
    </table>
    {timing.length > 0 && (
      <details className="tool-timing sub">
        <summary>{t("details.usage.bytool")}</summary>
        {timing.map((r) => <div className="dt-row" key={r.name}>
          <span className="grow">{r.name}</span>
          <span>{t("details.usage.toolrow", { time: duration(r.total_ms), c: r.calls, e: r.errors })}</span>
        </div>)}
      </details>
    )}
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
      onChange();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function save() {
    if (!text.trim()) return;
    try {
      await api.post(`/api/sessions/${sessionId}/loop`, { instruction: text.trim(), mode, interval_minutes: mode === "interval" ? Math.max(1, Number(minutes) || 10) : null, max_runs: maxRuns.trim() ? Math.max(1, Number(maxRuns) || 1) : null, start_now: true });
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
            <Select<"interval" | "dynamic"> size="md" label={t("session.loop")} value={mode} onChange={setMode}
              options={[{ id: "interval", label: t("newagent.loop.interval") }, { id: "dynamic", label: t("loop.selfpaced") }]} />
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
const PROVIDER_LABEL: Record<string, string> = { codex: "Codex", claude: "Claude", grok: "Grok", antigravity: "Google (Antigravity)", opencode: "OpenCode Go", openrouter: "OpenRouter", deepseek: "DeepSeek" };

export function providerLabel(id: string): string {
  return PROVIDER_LABEL[id] ?? (id ? id[0].toUpperCase() + id.slice(1) : id);
}

/** The provider's busiest windows as two short bars, or the day's metered spend, in a line of the list. */
export function ProviderSummary({ usage }: { usage: ProviderUsage | null }) {
  const sub = usage?.subscription;
  if (!usage) return null;
  if (sub?.limit_reached) return <span className="badge bad">{t("usage.limitreached")}</span>;
  if (sub && !sub.logged_in) return <span className="quiet">{t("usage.notloggedin")}</span>;
  const windows = (sub?.windows ?? []).slice(0, 2);
  if (windows.length) return <>{windows.map((w, i) => <span key={w.name} className="dt-window">{i > 0 && <span className="quiet">·</span>}{w.name} <Mini percent={w.used_percent} tone={quotaTone(w.used_percent)} />{Math.round(w.used_percent)}%</span>)}</>;
  return <>{fmtUsd(usage.today?.cost_usd)}<span className="quiet">{t("details.prop.today")}</span></>;
}

/** What the session's provider has left: a subscription's windows, or the day's metered spend and balance. */
export function ProviderUsageBody({ usage }: { usage: ProviderUsage | null }) {
  const sub = usage?.subscription;
  const today = usage?.today ?? {};
  return <>
    {(sub?.plan || sub?.limit_reached) && <div className="dt-badges">
      {sub?.plan && <span className="badge">{planName(sub.plan)}</span>}
      {sub?.limit_reached && <span className="badge bad">{t("usage.limitreached")}</span>}
    </div>}
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
  </>;
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

type ToolTime = { name: string; calls: number; errors: number; total_ms: number; mean_ms: number };

/** The time each tool took over the session. */
function useToolTiming(sessionId: string): ToolTime[] {
  const [rows, setRows] = useState<ToolTime[]>([]);
  useEffect(() => {
    api.get<{ items: ToolTime[] }>(`/api/sessions/${sessionId}/tools/timing`).then((r) => setRows(Array.isArray(r?.items) ? r.items : [])).catch(() => setRows([]));
  }, [sessionId]);
  return rows;
}

type McpServer = { name: string; description: string; connected: boolean; error: string | null; tools: string[]; state: "disabled" | "connecting" | "ready" | "auth_required" | "unavailable" | "draining"; catalog_revision: number; last_error_code: string | null; in_flight: number };

type Mcp = { data: { enabled: string[]; servers: McpServer[] } | null; busy: string | null; load: () => void; toggle: (server: string, enabled: boolean) => Promise<void>; grant: (server: string, hours: number) => Promise<void> };

/** The session's MCP servers, read once for both the line of the list (how many are on) and the page. */
function useMcp(sessionId: string, toast: (t: string) => void, onGranted: () => void): Mcp {
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
  const toggle = async (server: string, enabled: boolean) => {
    setBusy(server);
    try {
      setData(await api.put(`/api/sessions/${sessionId}/mcp`, { server, enabled }));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(null);
    }
  };
  const grant = async (server: string, hours: number) => {
    try {
      await api.post(`/api/sessions/${sessionId}/policy/grant-server`, { server, hours });
      onGranted();
    } catch (e) {
      toast(errorText(e));
    }
  };
  return { data, busy, load, toggle, grant };
}

/** The session's standing grants, for the count on their line; the page lists and revokes them itself. */
function useStanding(sessionId: string, refresh: number): Standing[] | null {
  const [rows, setRows] = useState<Standing[] | null>(null);
  useEffect(() => {
    api.get<{ standing: Standing[] }>(`/api/sessions/${sessionId}/policy/standing`).then((r) => setRows(Array.isArray(r?.standing) ? r.standing : [])).catch(() => setRows(null));
  }, [sessionId, refresh]);
  return rows;
}

/** The session's helper models, for the state on their line; null leaves the line out, as the page would. */
function useHelperModels(sessionId: string): SubagentModelsView | null {
  const [view, setView] = useState<SubagentModelsView | null>(null);
  useEffect(() => {
    let current = true;
    api.get<SubagentModelsView>(`/api/sessions/${encodeURIComponent(sessionId)}/subagent-models`)
      .then((v) => { if (current) setView(Array.isArray(v?.allowed) && Array.isArray(v?.options) ? v : null); })
      .catch(() => undefined);
    return () => { current = false; };
  }, [sessionId]);
  return view;
}

function McpPanel({ sessionId, toast, mcp }: { sessionId: string; toast: (t: string) => void; mcp: Mcp }) {
  const { data, busy, toggle, grant, load } = mcp;
  if (!data) return <div className="empty">…</div>;
  const quick = <McpQuickAdd sessionId={sessionId} toast={toast} onAdded={load} />;
  if (data.servers.length === 0) return <><div className="empty">{t("session.mcp.none")}</div>{quick}</>;
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
            {on && s.state === "ready" && <div className="dt-server-grant"><ServerGrantButton server={s.name} onGrant={(hours) => void grant(s.name, hours)} /></div>}
          </div>
        );
      })}
      {quick}
    </div>
  );
}

/** A server's state as a coloured dot: ready is good, a passing state is amber, one the operator must fix is red. */
const MCP_TONE: Partial<Record<McpServer["state"], string>> = { ready: "ok", connecting: "attn", draining: "attn", auth_required: "bad", unavailable: "bad" };
