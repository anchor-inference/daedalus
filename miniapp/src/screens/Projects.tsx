// The desktop's page of projects: one table in the conversation's place, where the "All projects"
// sheet used to stand over everything. A project is a folder its chats work in, so the table leads
// with the name and the path, then how many chats, what is live in them now, where they run and when
// anything last happened. The projects with an orchestrator are not here: they live in orchestration
// mode, and a note at the foot says how many and goes there.

import { useEffect, useMemo, useState } from "react";
import { api, Project, ProjectFolder, SessionList } from "../api";
import { AddProjectSheet, ProjectSettingsSheet, useProjects } from "../projects";
import { isProject } from "../isproject";
import { projectPath, projectReachable } from "../folders";
import { agentsListingOf, isMainProject, modeHome } from "../mode";
import { kindOf } from "../grouping";
import { relTime } from "../format";
import { EnvPill } from "../envpill";
import { Icon } from "../icons";
import { navigate, pathFor, projectPagePath } from "../router";
import { invalidate, useOffline, useQuery } from "../store";
import { useStreamUp } from "../events";
import { OverflowMenu, type MenuItem } from "../ui/dialogs";
import { errorText } from "../ui";
import { plural, t } from "../i18n";

type Filter = "all" | "waiting" | "working";

/** What is live in one project, counted over the listing's top-level chats. */
type Live = { waiting: number; working: number; loops: number };

/** The page. `onPickProject` narrows the column to one project, the lens the old sheet set. */
export function ProjectsPage({ toast, onPickProject }: { toast: (text: string) => void; onPickProject?: (id: string) => void }) {
  const projects = useProjects();
  const stream = useStreamUp();
  const { data: listing } = useQuery<SessionList>("/api/sessions?view=all", { pollMs: stream ? 60000 : 5000, staleMs: 3000 });
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Project | null>(null);
  const [restoring, setRestoring] = useState("");
  const offline = useOffline();

  const all = projects.data ?? [];
  // Agents mode's projects: kept on purpose, not orchestrated, not the main orchestrator's own.
  const mine = all.filter((p) => isProject(p) && !p.settings.orchestrator?.enabled && !isMainProject(p));
  const listed = mine.filter((p) => !p.settings.archived);
  const archived = mine.filter((p) => p.settings.archived);
  const elsewhere = all.filter((p) => !!p.settings.orchestrator?.enabled && !p.settings.archived);
  // An installation with no project yet opens straight onto adding one, as the sheet this page
  // replaced did: an empty table with one button is a step that says nothing. Once, on arrival.
  const loaded = !!projects.data;
  const empty = loaded && mine.length === 0;
  useEffect(() => {
    if (empty) setAdding(true);
  }, [empty]);

  const folders = useMemo(() => new Map((listing?.projects ?? []).map((folder) => [folder.id, folder])), [listing]);
  const live = useMemo(() => {
    const out = new Map<string, Live>();
    for (const s of agentsListingOf(listing)?.sessions ?? []) {
      if (s.metadata?.subagent_of || s.archived) continue;
      const kind = kindOf(s, false);
      if (kind === "idle") continue;
      const row = out.get(s.project_id) ?? { waiting: 0, working: 0, loops: 0 };
      if (kind === "waiting") row.waiting += 1;
      else if (kind === "working") row.working += 1;
      else row.loops += 1;
      out.set(s.project_id, row);
    }
    return out;
  }, [listing]);
  const liveOf = (id: string): Live => live.get(id) ?? { waiting: 0, working: 0, loops: 0 };

  const needle = query.trim().toLowerCase();
  const counts = { waiting: listed.filter((p) => liveOf(p.id).waiting > 0).length, working: listed.filter((p) => liveOf(p.id).working > 0).length };
  const last = (p: Project) => Date.parse(folders.get(p.id)?.last_message_at ?? "") || 0;
  const rows = listed
    .filter((p) => !needle || `${p.name} ${projectPath(p)}`.toLowerCase().includes(needle))
    .filter((p) => filter === "all" || (filter === "waiting" ? liveOf(p.id).waiting > 0 : liveOf(p.id).working > 0))
    .sort((a, b) => last(b) - last(a) || a.name.localeCompare(b.name));

  const startIn = (p: Project) => navigate(pathFor("agents", null, { in: p.id }));
  async function restore(project: Project) {
    if (restoring || offline || !Number.isInteger(project.entity_revision)) return;
    setRestoring(project.id);
    try {
      await api.patch(`/api/projects/${encodeURIComponent(project.id)}`, { archived: false,
        expected_entity_revision: project.entity_revision, client_operation_id: crypto.randomUUID() });
      invalidate("/api/projects");
      invalidate("/api/sessions");
      toast(t("project.restored", { name: project.name }));
    } catch (error) { toast(errorText(error)); }
    finally { setRestoring(""); }
  }

  const chip = (id: Filter, label: string, n: number, tone = "") => (
    <button type="button" className={`projects-chip ${tone} ${filter === id ? "on" : ""}`} aria-pressed={filter === id} onClick={() => setFilter(id)}>
      {label}<span className="num">{n}</span>
    </button>
  );

  return (
    <div className="projects-page">
      <header className="projects-head"><h1>{t("shell.projects")}</h1></header>
      <div className="projects-body">
        <div className="projects-col">
          <div className="projects-tools">
            <label className="projects-search">
              <Icon name="search" size={15} />
              <input type="search" value={query} placeholder={t("projects.search")} aria-label={t("projects.search")} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") setQuery(""); }} />
            </label>
            <div className="projects-chips" role="group" aria-label={t("projects.filter")}>
              {chip("all", t("agents.filter.all"), listed.length)}
              {chip("waiting", t("projects.filter.waiting"), counts.waiting, "warn")}
              {chip("working", t("status.running"), counts.working)}
            </div>
            <span className="grow" />
            <button type="button" className="btn projects-new" onClick={() => setAdding(true)}><Icon name="plus" size={16} />{t("ph.newproject")}</button>
          </div>
          <div className="projects-table" role="table" aria-label={t("shell.projects")}>
            <div className="projects-row head" role="row">
              <span role="columnheader">{t("projects.col.project")}</span>
              <span role="columnheader">{t("start.chats")}</span>
              <span role="columnheader">{t("projects.col.now")}</span>
              <span role="columnheader">{t("projects.col.where")}</span>
              <span role="columnheader">{t("projects.col.activity")}</span>
              <span role="columnheader" aria-label={t("projects.col.actions")} />
            </div>
            {rows.map((p) => (
              <ProjectRow key={p.id} project={p} folder={folders.get(p.id)} live={liveOf(p.id)} onStart={() => startIn(p)} onSettings={() => setEditing(p)}
                menu={[
                  { label: t("ph.newchat.in", { name: p.name }), icon: "compose", onSelect: () => startIn(p) },
                  ...(onPickProject ? [{ label: t("projects.lens", { name: p.name }), icon: "folder" as const, onSelect: () => { onPickProject(p.id); navigate(pathFor("agents")); } }] : []),
                  ...(!p.system ? [
                    { label: t("project.team.for", { name: p.name }), icon: "bots" as const, onSelect: () => navigate(projectPagePath(p.id, "team")) },
                    { label: t("project.board.for", { name: p.name }), icon: "board" as const, onSelect: () => navigate(projectPagePath(p.id, "board")) },
                  ] : []),
                  "-",
                  { label: t("project.settings.for", { name: p.name }), icon: "settings", onSelect: () => setEditing(p) },
                ]} />
            ))}
            {projects.data && rows.length === 0 && (
              <div className="projects-empty" role="row">{listed.length ? t("projects.none.match") : t("projects.none")}</div>
            )}
          </div>
          {archived.length > 0 && (
            <details className="projects-archived">
              <summary><span>{t("projects.archived")}</span><span className="num">{archived.length}</span><span className="grow" /><Icon name="chevron" size={14} /></summary>
              {archived.map((p) => (
                <div key={p.id} className="projects-archived-row" data-project={p.id}>
                  <span className="grow projects-name">
                    <b className="truncate">{p.name}</b>
                    <span className="mono truncate">{projectPath(p)}</span>
                  </span>
                  <button type="button" className="btn ghost small" disabled={!!restoring || offline} onClick={() => void restore(p)}>{t("project.restore")}</button>
                  <button type="button" className="iconbtn small" onClick={() => setEditing(p)} title={t("project.settings.for", { name: p.name })} aria-label={t("project.settings.for", { name: p.name })}>
                    <Icon name="settings" size={15} />
                  </button>
                </div>
              ))}
            </details>
          )}
          {elsewhere.length > 0 && (
            <div className="projects-note">
              <Icon name="compass" size={16} />
              <span className="grow">{t("projects.orchestrated", { names: elsewhere.length === 1 ? elsewhere[0].name : t("projects.orchestrated.names", { name: elsewhere[0].name, n: elsewhere.length - 1 }) })}</span>
              <a href={modeHome("orchestration")} onClick={(e) => { e.preventDefault(); navigate(modeHome("orchestration")); }}>{t("projects.orchestrated.open")}</a>
            </div>
          )}
        </div>
      </div>
      {adding && <AddProjectSheet firstProject={mine.length === 0} onClose={() => setAdding(false)} onAdded={(p) => { setAdding(false); startIn(p); }} toast={toast} />}
      {editing && <ProjectSettingsSheet project={editing} onClose={() => setEditing(null)} onRemoved={() => setEditing(null)} toast={toast} />}
    </div>
  );
}

/** One project: its tile with the live dot, the name over the path, and the columns. The row opens a
 *  new chat in the project; a new chat, the settings and the menu show on hover or focus. */
function ProjectRow({ project: p, folder, live, onStart, onSettings, menu }: { project: Project; folder?: ProjectFolder; live: Live; onStart: () => void; onSettings: () => void; menu: MenuItem[] }) {
  const chats = folder?.total ?? p.sessions.length;
  const env = p.folders[0]?.env ?? "container";
  const dot = live.waiting ? "waiting" : live.working ? "running" : "";
  const reachable = projectReachable(p);
  const state = [
    live.waiting > 0 && <span key="w" className="projects-pill warn">{plural("projects.waiting", live.waiting)}</span>,
    live.working > 0 && <span key="r" className="projects-pill info">{plural("projects.working", live.working)}</span>,
    live.loops > 0 && <span key="l" className="projects-pill">{plural("projects.loops", live.loops)}</span>,
    !reachable && <span key="m" className="projects-pill warn" title={t("project.notmounted.bot")}>{t("project.notmounted")}</span>,
  ].filter(Boolean);
  if (!state.length && p.settings.snapshots) state.push(<span key="s" className="projects-pill" title={t("project.snapshots.title")}>{t("project.snapshots.badge")}</span>);
  return (
    <div className="projects-row" role="row" data-project={p.id} tabIndex={0} onClick={onStart}
      onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter") { e.preventDefault(); onStart(); } }}>
      <span className="projects-name" role="cell">
        <span className="projects-tile"><Icon name={p.system === "voice" ? "mic" : "folder"} size={16} />{dot && <span className={`dot ${dot}`} aria-hidden />}</span>
        <span className="projects-text">
          <b className="truncate">{p.name}</b>
          <span className="mono truncate">{p.system ? t("project.system.badge") : projectPath(p)}</span>
        </span>
      </span>
      <span className="num" role="cell">{plural("ph.chats.count", chats)}</span>
      <span className="projects-state" role="cell">{state.length ? state : <span className="projects-none">—</span>}</span>
      <span role="cell"><EnvPill env={env} tiny /></span>
      <span className="num projects-when" role="cell">{folder?.last_message_at ? relTime(folder.last_message_at) : "—"}</span>
      <span className="projects-acts" role="cell" onClick={(e) => e.stopPropagation()}>
        <button type="button" className="iconbtn small" onClick={onStart} title={t("ph.newchat.in", { name: p.name })} aria-label={t("ph.newchat.in", { name: p.name })}><Icon name="compose" size={15} /></button>
        <button type="button" className="iconbtn small" onClick={onSettings} title={t("project.settings.for", { name: p.name })} aria-label={t("project.settings.for", { name: p.name })}><Icon name="settings" size={15} /></button>
        <OverflowMenu small items={menu} label={t("projects.actions", { name: p.name })} />
      </span>
    </div>
  );
}
