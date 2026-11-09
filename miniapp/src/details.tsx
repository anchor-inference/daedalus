// The Details tab: what the session is and what it is set to, as an inspector. Each section is one
// line — its name on the left, its state in a few words on the right — so all of them fit on one screen
// and the list reads as a table of contents; a press opens the section in place of the list, with a way
// back. The tab used to be a column of cards of equal weight, and the two numbers that matter (the
// context and the cost) sank among a folder's path and a disk's size; those two now live in the strip
// over every tab (panelstrip.tsx) and lead the list here. What one does to the session — rename it, move
// it, clear it, delete it — is in its menu (the strip's ⋯ and the chat header's ⋯ are one list), not in a
// card at the foot of the tab. The parts a page is built from are in detailsparts.tsx; the right panel
// hosts the tab on a desktop and a full sheet on a phone.

import { OriginSection } from "./imports/ImportedChat";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, LoopView, ProviderUsage, Schedule, SessionDetail } from "./api";
import { projectPath } from "./folders";
import { ServiceRow, Switch, ToolPicker, fmtInt, fmtUsd, loopLabel, statusWord, timeAgo } from "./ui/components";
import { absTime, clock, duration, planName, relTimeLong, shortDateTime, untilShort } from "./format";
import { Icon, type IconName } from "./icons";
import { CopyValue, Fact, Facts, Headline, Meter, Note, Stat, Stats, Until } from "./detailsparts";
import { contextSegments, contextTone, quotaTone } from "./detailsmodel";
import { confirmAsync, errorText, fmtTok } from "./ui";
import { plural, t } from "./i18n";
import { SessionToolGroups } from "./toolgroupsview";
import { HostMark } from "./runon";
import { DiskUsage } from "./diskusage";
import { MoveLive, useFolderMove } from "./project/moves";
import { useLayer } from "./ui/dialogs";

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
};

type PageId = "origin" | "context" | "usage" | "provider" | "workspace" | "disk" | "loop" | "cron" | "services" | "mcp" | "toolgroups" | "tools" | "brief" | "spend" | "advanced";

/** One line of the inspector: what it opens, its icon and name, and its state in a few words. */
type Prop = { id: PageId; icon: IconName; label: string; value?: ReactNode; title?: string; attn?: boolean; body: () => ReactNode };

export function SessionDetails({ ids, id, role = "session", detail, busy, schedules, provider, providerUsage, onOpen, toast, reload, on, focus, focusKey }: SessionDetailsProps) {
  const container = useRef<HTMLDivElement>(null);
  const conductor = role !== "session";
  const sessionFolder = detail.project?.folders?.find((folder) => folder.id === detail.folder_id) ?? detail.project?.folders?.[0] ?? null;
  const folderMove = useFolderMove(sessionFolder?.id);
  const ctxPct = detail.context && detail.context.window > 0 ? Math.round((100 * detail.context.tokens) / detail.context.window) : null;
  const mcp = useMcp(id, toast);
  const [page, setPage] = useState<PageId | null>(null);
  // The line a page was opened from, so Back puts the keyboard where it was.
  const from = useRef<PageId | null>(null);
  const open = (next: PageId | null) => {
    if (next) from.current = next;
    setPage(next);
  };

  const groups: { key: string; props: Prop[] }[] = [
    { key: "now", props: [
      ...(detail.imported ? [{ id: "origin" as const, icon: "fork" as IconName, label: t("imp.origin"), value: detail.imported.harness_name ?? detail.imported.harness, body: () => <OriginSection sessionId={id} origin={detail.imported!} toast={toast} onPulled={() => reload(true)} /> }] : []),
      ...(detail.context ? [{
        id: "context" as const, icon: "braces" as IconName, label: t("session.context"), attn: !!detail.context.prefix_changed?.length,
        value: <>{ctxPct !== null && <Mini percent={ctxPct} kind="context" tone={contextTone(ctxPct)} />}{ctxPct !== null ? `${ctxPct}%` : fmtTok(detail.context.tokens)}{detail.context.recent_cache && <span className="quiet">{t("details.prop.cache", { n: detail.context.recent_cache.hit_percent ?? 0 })}</span>}</>,
        body: () => <ContextBody context={detail.context!} percent={ctxPct} busy={busy} onCompact={on.compact} />,
      }] : []),
      { id: "usage", icon: "chart", label: t("session.usage"), value: <>{fmtUsd(detail.usage.usd)}<span className="quiet">· {plural("usage.calls", detail.usage.c ?? 0)}</span></>, body: () => <UsageBody sessionId={id} usage={detail.usage} /> },
      ...(provider ? [{ id: "provider" as const, icon: "bolt" as IconName, label: providerLabel(provider), value: <ProviderSummary usage={providerUsage} />, body: () => <ProviderUsageBody usage={providerUsage} /> }] : []),
    ] },
    { key: "where", props: role === "orchestrator" ? [] : [
      { id: "workspace", icon: "folder", label: t("session.workspace"), title: detail.project ? projectPath(detail.project) : detail.workspace,
        value: <>{folderMove && <span className="badge attn">{t("details.prop.moving")}</span>}<span className="truncate">{workspaceWord(detail)}</span><HostMark env={detail.env} /></>,
        body: () => <WorkspaceBody detail={detail} conductor={conductor} folderMove={folderMove} onOpen={onOpen} on={on} /> },
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
        body: () => <McpPanel mcp={mcp} /> },
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
  const all = groups.flatMap((g) => g.props);
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
      {groups.filter((g) => g.props.length > 0).map((g) => (
        <section key={g.key} className="dt-props" aria-labelledby={`${ids}-grp-${g.key}`}>
          <h3 className="dt-grp" id={`${ids}-grp-${g.key}`}>{t(`details.group.${g.key}`)}</h3>
          {g.props.map((p) => (
            <button key={p.id} type="button" className="dt-prop" id={`${ids}-info-${p.id}`} data-prop={p.id} title={p.title} onClick={() => open(p.id)}>
              <Icon name={p.icon} size={16} />
              <span className="dt-prop-k">{p.label}</span>
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

/** What the session spent, as one table: in, of it from the cache, out, calls and the tools' time, in
 *  total and today. It used to be the sum as a headline, today's sum beside it, and four tiles, each
 *  with today's share under it — three places for the same few numbers. */
function UsageBody({ sessionId, usage }: { sessionId: string; usage: SessionDetail["usage"] }) {
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

/** The provider's busiest windows as two short bars, or the day's metered spend, in a line of the list. */
function ProviderSummary({ usage }: { usage: ProviderUsage | null }) {
  const sub = usage?.subscription;
  if (!usage) return null;
  if (sub?.limit_reached) return <span className="badge bad">{t("usage.limitreached")}</span>;
  if (sub && !sub.logged_in) return <span className="quiet">{t("usage.notloggedin")}</span>;
  const windows = (sub?.windows ?? []).slice(0, 2);
  if (windows.length) return <>{windows.map((w, i) => <span key={w.name} className="dt-window">{i > 0 && <span className="quiet">·</span>}{w.name} <Mini percent={w.used_percent} tone={quotaTone(w.used_percent)} />{Math.round(w.used_percent)}%</span>)}</>;
  return <>{fmtUsd(usage.today?.cost_usd)}<span className="quiet">{t("details.prop.today")}</span></>;
}

/** What the session's provider has left: a subscription's windows, or the day's metered spend and balance. */
function ProviderUsageBody({ usage }: { usage: ProviderUsage | null }) {
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

type Mcp = { data: { enabled: string[]; servers: McpServer[] } | null; busy: string | null; toggle: (server: string, enabled: boolean) => Promise<void> };

/** The session's MCP servers, read once for both the line of the list (how many are on) and the page. */
function useMcp(sessionId: string, toast: (t: string) => void): Mcp {
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
  return { data, busy, toggle };
}

function McpPanel({ mcp }: { mcp: Mcp }) {
  const { data, busy, toggle } = mcp;
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
