// The Terminals screen. Its Overview is every terminal on the machine as a card with the last rows of
// its screen, by project, with the load bar in the header and a way to open a free one. The cards are
// read from the host's listing (`?preview=6`), never from a connection: forty cards must not hold
// forty sockets. Its Workspace (TerminalWorkspace.tsx) is the terminals the operator set up, live, in
// a grid; opening a card shows that terminal there (`/app/terminals/<id>`).
//
// Which of the two `/app/terminals` opens on is `openingView`'s rule: the Workspace when panes are
// set up, unless the operator chose the Overview last.

import { useContextActions } from "../ui/context-menu";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useEdgeFade } from "../edgefade";
import { api, Project, SessionList, TerminalEnv, TerminalEnvName, TerminalList, TerminalLoad, TerminalView as TerminalRow } from "../api";
import { Skeleton } from "../ui/index";
import { Sheet } from "../ui/dialogs";
import { primaryFolder } from "../folders";
import { t } from "../i18n";
import { Icon } from "../icons";
import { LoadBar } from "../loadbar";
import { navigate, pathFor } from "../router";
import { PageHeader, screenTitle, useMedia } from "../ui/index";
import { PhoneTerminals } from "./TerminalsPhone";
import { TerminalWorkspaceScreen } from "./TerminalWorkspace";
import { StateDot, ViewSwitch } from "../terminal/workspace";
import { paneState } from "../terminal/status";
import { useTerminalCommands } from "../terminal/rowmenu";
import { loadView, loadWorkspace, openingView, saveView } from "../terminal/workspacestate";
import { invalidate, peek, useQuery } from "../store";
import { errorText } from "../ui";
import { createTerminalConfirmed } from "../terminal/actions";
import { EnvPill } from "../envpill";
import { cardStatus, FILTERS, groupTerminals, headerCounts, matches, ownerLine, previewRows, runStyle, running, TerminalFilter } from "../terminal/preview";
import { TerminalStatusLine } from "../terminal/statusline";

/** How often the load bar asks again: the daemon measures at most every ten seconds anyway. */
const LOAD_POLL_MS = 10000;
export const LIST_KEY = "/api/terminals?preview=6";

/** The address of a terminal's full-screen view. */
export function terminalPath(id: string): string {
  return pathFor("terminals", id);
}

/** Where a new container terminal starts: the project the operator is looking at, else the folder of the conversation they used last. */
export function containerFolder(project: Project | null, sessions: SessionList["sessions"]): { path: string; projectId: string | null } | null {
  if (project) {
    const inside = project.folders.find((f) => f.env === "container" && f.reachable) ?? (primaryFolder(project)?.env === "container" ? primaryFolder(project) : undefined);
    if (inside) return { path: inside.path, projectId: project.id };
  }
  const recent = [...sessions].filter((s) => s.workspace_path).sort((a, b) => Date.parse(b.last_message_at || "") - Date.parse(a.last_message_at || ""))[0];
  return recent?.workspace_path ? { path: recent.workspace_path, projectId: recent.project_id || null } : null;
}

/** Opens a free terminal (asking at the cap) and goes to it; the shared path for this screen and the palette. */
export async function openFreeTerminal(env: TerminalEnvName, cwd: string | undefined, projectId: string | null, toast: (text: string) => void): Promise<void> {
  let row: TerminalRow | null;
  try {
    row = await createTerminalConfirmed({ env, owner_kind: "free", cwd: cwd || undefined, project_id: projectId || undefined });
  } catch (error) {
    toast(errorText(error));
    return;
  }
  if (!row) return;
  invalidate("/api/terminals");
  navigate(terminalPath(row.id));
}

export function TerminalsScreen(props: { detail: string | null; query: URLSearchParams; toast: (text: string) => void; project: string; projects: Project[] }) {
  const { detail, query, ...rest } = props;
  // A phone draws the same listing from its own pieces (TerminalsPhone.tsx): a capacity line, cards,
  // sheets; a terminal opened from it is the Workspace's one pane at a time.
  const phone = !useMedia("(min-width: 1024px)");
  const asked = query.get("view");
  const marked = (window.history.state as { termView?: string } | null)?.termView;
  const view = detail ? "workspace" : phone ? "overview" : asked === "overview" || asked === "workspace" ? asked : marked === "overview" ? "overview" : openingView(loadView(), loadWorkspace());
  if (view === "workspace") return <TerminalWorkspaceScreen detail={detail} query={query} {...rest} />;
  return phone ? <PhoneTerminals {...rest} /> : <DesktopTerminals {...rest} />;
}

function DesktopTerminals({ toast, project, projects }: { toast: (text: string) => void; project: string; projects: Project[] }) {
  // The Overview marks its history entry: Back from a terminal opened here must come back to the
  // cards, not to whatever `/app/terminals` would choose by then (the Workspace, once a pane is set
  // up). In the entry's state rather than its address, so the address stays the one every link uses.
  useEffect(() => {
    try {
      window.history.replaceState({ ...(window.history.state ?? {}), termView: "overview" }, "");
    } catch {
      /* the choice falls back to the remembered one */
    }
  }, []);
  const toWorkspace = () => {
    saveView("workspace");
    navigate(pathFor("terminals", null, { view: "workspace" }));
  };
  const [filter, setFilter] = useState<TerminalFilter>("all");
  const [creating, setCreating] = useState(false);
  // The host says how often a preview is worth refreshing; the environments come with the listing.
  const [pollMs, setPollMs] = useState(3000);
  const list = useQuery<TerminalList>(LIST_KEY, { pollMs, staleMs: 1000 });
  const load = useQuery<TerminalLoad>("/api/terminals/load", { pollMs: LOAD_POLL_MS, staleMs: 3000 });
  const envs = list.data?.envs ?? [];
  const wanted = Math.max(1000, envs.find((e) => e.available)?.preview_poll_ms ?? 3000);
  if (wanted !== pollMs) setPollMs(wanted);

  const rows = useMemo(() => list.data?.terminals ?? [], [list.data]);
  const counts = headerCounts(rows);
  const shown = rows.filter((r) => matches(r, filter));
  const groups = groupTerminals(shown, projects);
  const names = useMemo(() => new Map(projects.map((p) => [p.id, p.name])), [projects]);
  const homes = useMemo(() => new Map((list.data?.envs ?? []).map((e) => [e.env, e.home])), [list.data?.envs]);

  const refresh = useCallback(() => {
    invalidate("/api/terminals");
  }, []);

  const remove = async (row: TerminalRow) => {
    try {
      await api.removeTerminal(row.id);
    } catch (error) {
      toast(errorText(error));
    }
    refresh();
  };

  // On a phone the chips run past the edge; the row fades there so "Finished" reads as a swipe away.
  const filterRow = useRef<HTMLDivElement>(null);
  useEdgeFade(filterRow, rows.length);
  const subtitle = list.data ? t("term.screen.counts", { open: counts.open, host: counts.host, staff: counts.staff }) : undefined;
  const lens = projects.find((p) => p.id === project) ?? null;

  return (
    <div className="tp tp-overview">
      <PageHeader
        title={screenTitle("terminals")}
        subtitle={subtitle}
        actions={
          <>
          <ViewSwitch view="overview" onChange={(v) => { if (v === "workspace") toWorkspace(); }} />
          <button className="btn primary small term-new-button" onClick={() => setCreating(true)} disabled={list.data ? !envs.some((e) => e.available) : false}>
            <Icon name="plus" size={16} />
            <span>{t("term.new")}</span>
          </button>
          </>
        }
      >
        {load.data && <LoadBar load={load.data} compact />}
        <div ref={filterRow} className="chips term-filters" role="group" aria-label={t("term.filter.label")}>
          {FILTERS.map((f) => {
            const n = rows.filter((r) => matches(r, f)).length;
            return (
              <button key={f} className="chip select" aria-pressed={filter === f} data-filter={f} onClick={() => setFilter(f)}>
                {t(`term.filter.${f}`)}
                {n > 0 && <span className="num"> · {n}</span>}
              </button>
            );
          })}
        </div>
      </PageHeader>
      <div className="screen wide term-screen-list">
        {list.loading && !list.error && <Skeleton rows={3} />}
        {list.error && !list.data && (
          <div className="empty">
            <b>{t("term.screen.error")}</b>
            <div>{list.error}</div>
            <button className="btn primary" onClick={() => void list.refresh()}>{t("common.retry")}</button>
          </div>
        )}
        {list.data && rows.length === 0 && (
          <div className="empty">
            <b>{t("term.screen.empty")}</b>
            <div>{t("term.screen.empty.sub")}</div>
          </div>
        )}
        {list.data && rows.length > 0 && shown.length === 0 && <div className="empty"><b>{t("term.screen.none")}</b></div>}
        {groups.map((group) => (
          <section key={group.key || "none"} className="term-group" data-group={group.key || "none"}>
            <div className="section-title">
              <span>{group.name ?? t("term.screen.noproject")}</span>
              <span className="n">{group.rows.length}</span>
            </div>
            <div className="term-cards">
              {group.rows.map((row) => (
                <TerminalCard key={row.id} row={row} projectName={row.project_id ? names.get(row.project_id) ?? null : null} home={homes.get(row.env) ?? null} onRemove={() => void remove(row)} toast={toast} />
              ))}
            </div>
          </section>
        ))}
      </div>
      {creating && <NewTerminalSheet envs={envs} lens={lens} projects={projects} toast={toast} onClose={() => setCreating(false)} />}
    </div>
  );
}

function TerminalCard({ row, projectName, home, onRemove, toast }: { row: TerminalRow; projectName: string | null; home: string | null; onRemove: () => void; toast: (text: string) => void }) {
  const status = cardStatus(row);
  const live = running(row);
  const open = () => navigate(terminalPath(row.id));
  const action = live && row.activity?.action;
  const card = useRef<HTMLElement>(null);
  // Rename (and End) are the list row's own commands, with the same sheet and the same questions.
  const commands = useTerminalCommands(row, toast);
  const rename = commands.items[0];
  useContextActions(card, [
    { label:t("term.card.open"), icon:"terminal", onSelect:open },
    ...(action ? [{ label:action.kind === "answer" ? t("term.card.answer") : action.label, onSelect:() => navigate(action.path) }] : []),
    ...(rename !== "-" ? [rename] : []),
    ...(!live ? [{ label:t("term.remove"), icon:"trash" as const, onSelect:onRemove }] : []),
  ]);
  return (
    <article ref={card} className={`term-card ${row.env === "host" ? "host" : ""} ${live ? "" : "finished"}`} data-terminal={row.id}>
      <button className="term-card-open" onClick={open} aria-label={t("term.open", { title: row.title })}>
        <div className="term-card-head">
          <span className="term-card-title truncate">{row.title || t("term.untitled")}</span>
          <EnvPill env={row.env} />
        </div>
        <div className="term-card-owner sub truncate">{ownerLine(row, projectName)}</div>
        <pre className="term-card-preview" aria-hidden="true">
          {previewRows(row.preview).map((line, y) => (
            <div key={y} className="term-card-line">
              {line.map((run, x) => (
                <span key={x} style={runStyle(run)}>{run.t}</span>
              ))}
            </div>
          ))}
        </pre>
        {/* The pane's status line, shortened: the agent, the place, the meter, the cost. */}
        {live && <TerminalStatusLine row={row} home={home} variant="card" />}
      </button>
      <div className="term-card-foot">
        <StateDot status={paneState(row)} className="term-card-dot" />
        <span className="term-card-status sub truncate" data-level={status.level}>{status.text}</span>
        {!live ? (
          <button className="btn small ghost" onClick={onRemove}>{t("term.remove")}</button>
        ) : action ? (
          <button className={`btn small ${row.activity?.level === "warn" || row.activity?.level === "bad" ? "warn" : ""}`} onClick={() => navigate(action.path)}>{action.kind === "answer" ? t("term.card.answer") : action.label}</button>
        ) : (
          <button className="btn small" onClick={open}>{t("term.card.open")}</button>
        )}
      </div>
      {commands.layer}
    </article>
  );
}

/**
 * The three ways to open a free terminal: in the container at the folder the operator is working in,
 * on the host at their home, or anywhere they choose. The host choice is marked amber and carries no
 * extra question — the operator decided that a marker is enough.
 */
export function NewTerminalSheet({ envs, lens, projects, toast, onClose }: { envs: TerminalEnv[]; lens: Project | null; projects: Project[]; toast: (text: string) => void; onClose: () => void }) {
  const env = (name: TerminalEnvName) => envs.find((e) => e.env === name);
  const container = env("container");
  const host = env("host");
  const sessions = peek<SessionList>("/api/sessions")?.sessions ?? [];
  const here = containerFolder(lens, sessions);
  const [choosing, setChoosing] = useState(false);
  const [where, setWhere] = useState<TerminalEnvName>(container?.available ? "container" : "host");
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState(false);
  const pick = window.daedalus?.pickFolder;

  const go = async (name: TerminalEnvName, cwd: string | undefined, projectId: string | null) => {
    setBusy(true);
    await openFreeTerminal(name, cwd, projectId, toast);
    setBusy(false);
    onClose();
  };

  // Suggestions are the projects' folders in the chosen environment: a container path means nothing
  // on the host and the other way round.
  const suggestions = projects.flatMap((p) => p.folders.filter((f) => f.env === where).map((f) => ({ path: f.path, project: p })));
  // A folder may belong to several projects: the one in view wins, then the first that has it.
  const matching = suggestions.filter((s) => s.path === path.trim());
  const chosenProject = (matching.find((s) => s.project.id === lens?.id) ?? matching[0])?.project.id ?? null;
  const browse = async () => {
    const picked = await pick?.();
    if (picked) setPath(picked);
  };

  return (
    <Sheet title={t("term.new")} onClose={onClose} size="narrow" className="term-new-sheet">
      {!choosing ? (
        <div className="term-new-options">
          <button className="term-new-row" disabled={!container?.available || busy} data-choice="container" onClick={() => void go("container", here?.path, here?.projectId ?? null)}>
            <Icon name="terminal" />
            <span className="term-new-text">
              <span>{t("term.new.in.container")}</span>
              <span className="sub truncate">{container?.available ? here?.path ?? t("term.new.container.home") : t("term.unavailable.container")}</span>
            </span>
          </button>
          <button className="term-new-row host" disabled={!host?.available || busy} data-choice="host" onClick={() => void go("host", host?.home || undefined, null)}>
            <Icon name="lock" />
            <span className="term-new-text">
              <span>{t("term.new.on.host")}</span>
              <span className="sub truncate">{host?.available ? host.home || t("term.new.host.home") : t("term.unavailable.host")}</span>
            </span>
          </button>
          <button className="term-new-row" disabled={busy || !envs.some((e) => e.available)} data-choice="folder" onClick={() => setChoosing(true)}>
            <Icon name="folder" />
            <span className="term-new-text">
              <span>{t("term.new.choose")}</span>
            </span>
          </button>
        </div>
      ) : (
        <form className="term-new-folder" onSubmit={(e) => { e.preventDefault(); if (path.trim()) void go(where, path.trim(), chosenProject); }}>
          <div className="segmented inline" role="radiogroup" aria-label={t("term.new.where")}>
            {(["container", "host"] as TerminalEnvName[]).map((name) => (
              <button key={name} type="button" role="radio" aria-checked={where === name} className={where === name ? "on" : ""} disabled={!env(name)?.available} onClick={() => setWhere(name)}>
                {t(`term.env.${name}`)}
              </button>
            ))}
          </div>
          <div className="term-new-path">
            <input className="field" autoFocus value={path} onChange={(e) => setPath(e.target.value)} placeholder={t("term.new.path")} aria-label={t("term.new.path")} list="term-new-suggestions" />
            {pick && <button type="button" className="btn small" onClick={() => void browse()}>{t("term.new.browse")}</button>}
          </div>
          <datalist id="term-new-suggestions">
            {suggestions.map((s) => <option key={`${s.project.id}:${s.path}`} value={s.path}>{s.project.name}</option>)}
          </datalist>
          {suggestions.length > 0 && (
            <div className="term-new-suggest">
              {suggestions.slice(0, 6).map((s) => (
                <button key={`${s.project.id}:${s.path}`} type="button" className="chip select" aria-pressed={path === s.path} onClick={() => setPath(s.path)} title={s.path}>{s.project.name}</button>
              ))}
            </div>
          )}
          <div className="term-new-actions">
            <button type="button" className="btn ghost" onClick={() => setChoosing(false)}>{t("shell.back")}</button>
            <button type="submit" className="btn primary" disabled={!path.trim() || busy}>{t("term.new.open")}</button>
          </div>
        </form>
      )}
    </Sheet>
  );
}
