import { openContextMenu } from "./ui/context-menu";
// Projects: a named home for agents and their files. The switcher lives in
// the shell (the sidebar on a desktop, the Agents header on a phone) because a project is a lens
// over every list of agents, not a destination of its own.

import { useEffect, useRef, useState } from "react";
import { api, ApiError, Project, ProjectDir, ProjectEnvironments } from "./api";
import { folderName, projectPath, projectReachable, reachIsProblem, reachKey } from "./folders";
import { Sheet } from "./ui/dialogs";
import { EnvPill } from "./envpill";
import { Icon } from "./icons";
import { navigate, projectPagePath } from "./router";
import { invalidate, useOffline, useQuery } from "./store";
import { confirmAsync, errorText } from "./ui";
import { plural, t } from "./i18n";
import { ProjectExtensions } from "./project/ProjectExtensions";
import { ExecutionHosts } from "./project/ExecutionHosts";
import { LifecycleCancel } from "./project/LifecycleCancel";
import { CoordinatorAuthority } from "./project/CoordinatorAuthority";
import { ProjectKnowledge } from "./project/ProjectKnowledge";
import { ProjectWorktrees } from "./project/ProjectWorktrees";
import { DiskUsage } from "./diskusage";
import { ProjectArchive } from "./project/ProjectArchive";
import { ProjectBudget, budgetKey } from "./project/ProjectBudget";
import { ProjectResources, resourceProfileKey } from "./project/ProjectResources";
import { RevealButton } from "./reveal";
import { FolderBrowser, type FolderChoice } from "./project/FolderBrowser";
import { openNewProject } from "./project/NewProject";
import { openImport } from "./imports/ImportExplorer";

const PICKED = "daedalus.project";

/** The project the operator is looking at, remembered between visits; "" is all of them. */
export function storedProject(): string {
  try {
    return localStorage.getItem(PICKED) ?? "";
  } catch {
    return "";
  }
}

export function rememberProject(id: string): void {
  try {
    if (id) localStorage.setItem(PICKED, id);
    else localStorage.removeItem(PICKED);
  } catch {
    /* private mode */
  }
}

export function useProjects() {
  return useQuery<Project[]>("/api/projects", { staleMs: 15000 });
}

function afterChange(): void {
  invalidate("/api/projects");
  invalidate("/api/sessions");
}

type ProjectWrite = { method: "PATCH" | "POST" | "DELETE" | "PUT"; path: string; body: Record<string, unknown>; label: string };
type WriteProject = (method: ProjectWrite["method"], path: string, fields: Record<string, unknown>, label: string, onSuccess?: () => void) => Promise<boolean>;

function savedProjectWrite(projectId: string): ProjectWrite | null {
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  try {
    const value = JSON.parse(sessionStorage.getItem(`daedalus.project.write.${projectId}`) ?? "null");
    const suffix = typeof value?.path === "string" && value.path.startsWith(base) ? value.path.slice(base.length) : null;
    const pathAllowed = suffix === "" || suffix === "/folders" || suffix === "/budget" || suffix === "/resource-profile"
      || (typeof suffix === "string" && /^\/folders\/[^/]+$/.test(suffix));
    return pathAllowed && ["PATCH", "POST", "DELETE", "PUT"].includes(value.method)
      && typeof value.body?.client_operation_id === "string" && Number.isInteger(value.body?.expected_entity_revision)
      && typeof value.label === "string" ? value as ProjectWrite : null;
  } catch { return null; }
}

function useProjectWrites(projectId: string, revision: number | undefined, readFailed: boolean, refresh: () => void, toast: (text: string) => void) {
  const [pending, setPending] = useState<ProjectWrite | null>(() => savedProjectWrite(projectId));
  const [conflict, setConflict] = useState<number | null>(null);
  const [requiredRevision, setRequiredRevision] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState<ProjectWrite | null>(null);
  const completion = useRef<(() => void) | null>(null);
  const offline = useOffline();
  const readCurrent = !readFailed && Number.isInteger(revision) && (revision ?? 0) > 0;
  const ready = !offline && readCurrent && !busy && !pending && conflict === null
    && (requiredRevision === null || (revision ?? 0) >= requiredRevision);

  function remember(next: ProjectWrite | null) {
    setPending(next);
    try {
      if (next) sessionStorage.setItem(`daedalus.project.write.${projectId}`, JSON.stringify(next));
      else sessionStorage.removeItem(`daedalus.project.write.${projectId}`);
    } catch { /* this mounted sheet still retains the exact request */ }
  }

  async function run(intent: ProjectWrite, onSuccess?: () => void): Promise<boolean> {
    if (busy || offline || readFailed) return false;
    setBusy(true);
    try {
      const receipt = await api.request<{ receipt_id: string; entity_revision: number }>(intent.method, intent.path, intent.body);
      if (!receipt || typeof receipt.receipt_id !== "string" || !Number.isInteger(receipt.entity_revision))
        throw new Error(t("project.write.badReceipt"));
      remember(null);
      setConfirmed(intent);
      setConflict(null);
      setRequiredRevision(receipt.entity_revision);
      if (intent.path === budgetKey(projectId) || intent.path === resourceProfileKey(projectId)) invalidate(intent.path);
      afterChange(); refresh();
      toast(intent.label);
      (onSuccess ?? completion.current)?.();
      completion.current = null;
      return true;
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        remember(null);
        const detail = error.data.detail;
        const currentRevision = error.data.current_revision ?? (detail && typeof detail === "object" ? (detail as Record<string, unknown>).current_revision : null);
        if (Number.isInteger(currentRevision)) setConflict(Number(intent.body.expected_entity_revision));
        completion.current = null;
        if (intent.path === budgetKey(projectId) || intent.path === resourceProfileKey(projectId)) invalidate(intent.path);
        refresh();
      } else if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408 && error.status !== 429) {
        remember(null);
        completion.current = null;
      }
      toast(errorText(error));
      return false;
    } finally { setBusy(false); }
  }

  const write: WriteProject = async (method, path, fields, label, onSuccess) => {
    if (!ready || !revision) return false;
    const intent: ProjectWrite = { method, path, label,
      body: { ...fields, expected_entity_revision: revision, client_operation_id: crypto.randomUUID() } };
    completion.current = onSuccess ?? null;
    remember(intent);
    return run(intent, onSuccess);
  };

  async function reviewConflict() {
    if (offline || busy) return;
    setBusy(true);
    try {
      const projects = await api.get<Project[]>("/api/projects");
      const fresh = projects.find((project) => project.id === projectId && Number.isInteger(project.entity_revision));
      if (!fresh) return;
      setRequiredRevision(fresh.entity_revision ?? null);
      setConflict(null);
      invalidate(budgetKey(projectId));
      invalidate(resourceProfileKey(projectId));
      afterChange(); refresh();
    } catch (error) { toast(errorText(error)); }
    finally { setBusy(false); }
  }

  return { write, retry: () => pending ? run(pending) : Promise.resolve(false), reviewConflict, pending, confirmed, conflict, busy, offline, readCurrent, ready };
}

/** The control that says which project is in view and opens the list: sidebar, header or palette. */
export function ProjectChip({ projects, current, onOpen, collapsed }: { projects: Project[]; current: string; onOpen: () => void; collapsed?: boolean }) {
  const active = projects.find((p) => p.id === current);
  const label = active ? active.name : projects.length ? t("shell.projects.all") : t("shell.projects.add");
  return (
    <button className="project-chip" onClick={onOpen} title={active ? projectPath(active) : t("shell.projects")} aria-haspopup="dialog">
      <Icon name="folder" size={16} />
      {!collapsed && <span className="sidebar-text truncate">{label}</span>}
      {!collapsed && active && !projectReachable(active) && <span className="badge attn" title={t("project.notmounted.here")}>{t("project.notmounted")}</span>}
      {!collapsed && <span className="chev">›</span>}
    </button>
  );
}

/** Pick a project, add one, or open one's settings. */
export function ProjectSwitcher({ projects, archived = [], current, onPick, onClose, toast }: { projects: Project[]; archived?: Project[]; current: string; onPick: (id: string) => void; onClose: () => void; toast: (t: string) => void }) {
  const [editing, setEditing] = useState<Project | null>(null);
  const [restoring, setRestoring] = useState("");
  const offline = useOffline();
  async function restore(project: Project) {
    if (restoring || offline || !Number.isInteger(project.entity_revision)) return;
    setRestoring(project.id);
    try {
      await api.patch(`/api/projects/${encodeURIComponent(project.id)}`, { archived: false,
        expected_entity_revision: project.entity_revision, client_operation_id: crypto.randomUUID() });
      afterChange();
      toast(t("project.restored", { name: project.name }));
    } catch (error) { toast(errorText(error)); }
    finally { setRestoring(""); }
  }
  const pick = (id: string) => {
    onPick(id);
    onClose();
  };
  const add = () => { onClose(); openNewProject("agents"); };
  // With nothing to switch between, the list would be a heading over an empty space: the operator came
  // to make the first project, so the dialog that makes it opens instead.
  const empty = projects.length === 0 && archived.length === 0;
  useEffect(() => { if (empty) add(); }, []);
  if (empty) return null;
  if (editing) return <ProjectSettingsSheet project={editing} onClose={() => setEditing(null)} onRemoved={() => { setEditing(null); if (editing.id === current) onPick(""); onClose(); }} toast={toast} />;
  return (
    <Sheet title={t("shell.projects")} onClose={onClose} size="narrow">
      <div className="sub" style={{ marginBottom: 8 }}>{t("project.intro")}</div>
      <button className={`menu-item ${current ? "" : "on"}`} onClick={() => pick("")}>
        <span className="grow truncate">{t("shell.projects.all")}</span>
        {!current && <Icon name="check" size={16} />}
      </button>
      {projects.map((p) => (
        <div key={p.id} className={`project-row ${p.id === current ? "on" : ""}`} onKeyDown={(event) => {
          if (event.key !== "ContextMenu" && !(event.shiftKey && event.key === "F10")) return;
          event.preventDefault();
          const rect = event.currentTarget.getBoundingClientRect();
          event.currentTarget.dispatchEvent(new MouseEvent("contextmenu", { bubbles:true, cancelable:true, clientX:rect.left + 12, clientY:rect.bottom }));
        }} onContextMenu={(event) => {
          event.preventDefault(); event.stopPropagation();
          openContextMenu({ x:event.clientX, y:event.clientY, owner:event.currentTarget.querySelector<HTMLElement>("button")!, items:[
            { label:p.name, icon:"folder", onSelect:() => pick(p.id) },
            ...(!p.system && !p.settings.ephemeral ? [
              { label:t("project.team.for", { name:p.name }), icon:"bots" as const, onSelect:() => { onClose(); navigate(projectPagePath(p.id, "team")); } },
              { label:t("project.board.for", { name:p.name }), icon:"board" as const, onSelect:() => { onClose(); navigate(projectPagePath(p.id, "board")); } },
            ] : []),
            { label:t("project.settings.for", { name:p.name }), icon:"settings", onSelect:() => setEditing(p) },
          ] });
        }}>
          <button className="grow project-pick" onClick={() => pick(p.id)}>
            <span className="project-name truncate">
              {p.name}
              {!projectReachable(p) && <span className="badge attn" title={t("project.notmounted.bot")}>{t("project.notmounted")}</span>}
              {p.settings.snapshots && <span className="badge" title={t("project.snapshots.title")}>{t("project.snapshots.badge")}</span>}
              {/* Says why this row has the gear alone: without it the missing team and board
                  buttons looked like icons that had failed to draw. */}
              {(p.system || p.settings.ephemeral) && <span className="badge" title={t("project.system.title")}>{t("project.system.badge")}</span>}
            </span>
            <span className="sub mono truncate">{projectPath(p)}</span>
            <span className="sub">{p.sessions.length ? plural("project.agents", p.sessions.length) : t("project.noagents")}</span>
          </button>
          {!p.system && !p.settings.ephemeral && (
            <button className="iconbtn small" onClick={() => { onClose(); navigate(projectPagePath(p.id, "team")); }} title={t("project.team.for", { name: p.name })} aria-label={t("project.team.for", { name: p.name })}>
              <Icon name="bots" size={15} />
            </button>
          )}
          {!p.system && !p.settings.ephemeral && (
            <button className="iconbtn small" onClick={() => { onClose(); navigate(projectPagePath(p.id, "board")); }} title={t("project.board.for", { name: p.name })} aria-label={t("project.board.for", { name: p.name })}>
              <Icon name="board" size={15} />
            </button>
          )}
          <button className="iconbtn small" onClick={() => setEditing(p)} title={t("project.settings.for", { name: p.name })} aria-label={t("project.settings.for", { name: p.name })}>
            <Icon name="settings" size={15} />
          </button>
        </div>
      ))}
      {archived.length > 0 && (
        <details className="sheet-section project-archived">
          <summary>{t("project.archivedFold", { n: archived.length })}</summary>
          {archived.map((p) => (
            <div key={p.id} className="project-row archived" data-project={p.id}>
              <span className="grow project-pick">
                <span className="project-name truncate">{p.name}</span>
                <span className="sub mono truncate">{projectPath(p)}</span>
              </span>
              <button className="btn ghost small" disabled={!!restoring || offline} onClick={() => void restore(p)}>{t("project.restore")}</button>
              <button className="iconbtn small" onClick={() => setEditing(p)} title={t("project.settings.for", { name: p.name })} aria-label={t("project.settings.for", { name: p.name })}>
                <Icon name="settings" size={15} />
              </button>
            </div>
          ))}
        </details>
      )}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.close")}</button>
        <button className="btn" onClick={() => { onClose(); openImport(); }} data-import-entry><Icon name="download" size={15} /> {t("imp.entry")}</button>
        <button className="btn primary" onClick={add}><Icon name="plus" size={15} /> {t("shell.projects.add")}</button>
      </div>
    </Sheet>
  );
}

type Env = "container" | "host";

type SettingsDraft = { name: string; snapshots: boolean; defaultEnv: Env; setupCommand?: string };

function savedSettingsDraft(projectId: string): SettingsDraft | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(`daedalus.project.draft.${projectId}`) ?? "null");
    return typeof value?.name === "string" && typeof value.snapshots === "boolean"
      && (value.defaultEnv === "container" || value.defaultEnv === "host") ? value as SettingsDraft : null;
  } catch { return null; }
}

/** Where a folder may live, asked once and kept: it changes only when the host terminal bridge is installed. */
export function useEnvironments() {
  return useQuery<ProjectEnvironments>("/api/project-environments", { staleMs: 60000 });
}

/** One folder of a project: what it is, who can reach it, the read-only switch and the way to forget it. */
function FolderRow({ project, folder, environments, write, canWrite }: { project: Project; folder: ProjectDir; environments?: ProjectEnvironments; write: WriteProject; canWrite: boolean }) {
  const [busy, setBusy] = useState(false);
  const primary = folder.position === 0;
  const only = project.folders.length === 1;
  const key = reachKey(folder, environments);
  const base = `/api/projects/${encodeURIComponent(project.id)}/folders/${encodeURIComponent(folder.id)}`;
  async function lock(readonly: boolean) {
    if (!canWrite || busy) return;
    setBusy(true);
    await write("PATCH", base, { readonly }, t(readonly ? "folder.locked" : "folder.unlocked", { name: folderName(folder) }));
    setBusy(false);
  }
  async function remove() {
    if (!(await confirmAsync(t("folder.remove.title", { name: folderName(folder) }), { body: t("folder.remove.body", { path: folder.path }), action: t("common.remove") }))) return;
    if (!canWrite || busy) return;
    setBusy(true);
    await write("DELETE", base, {}, t("folder.removed", { name: folderName(folder) }));
    setBusy(false);
  }
  return (
    <div className="dir-row" data-folder={folder.id}>
      <div className="dir-head">
        <Icon name="folder" size={15} />
        <span className="dir-name truncate">{folderName(folder)}</span>
        {primary && <span className="badge" title={t("folder.primary.title")}>{t("folder.primary")}</span>}
        <EnvPill env={folder.env} tiny />
        {folder.is_git && <span className="badge">{t("comp.name.git")}</span>}
        {folder.reachable !== false && <RevealButton target={{ project_id: project.id, folder_id: folder.id }} />}
        <button className="iconbtn small" onClick={remove} disabled={busy || only || !canWrite} title={only ? t("folder.remove.last") : t("folder.remove", { name: folderName(folder) })} aria-label={t("folder.remove", { name: folderName(folder) })}>
          <Icon name="trash" size={14} />
        </button>
      </div>
      <div className="sub mono dir-path">{folder.path}</div>
      <label className="toggle-row dir-lock">
        <input type="checkbox" checked={folder.readonly} disabled={busy || !canWrite} onChange={(e) => void lock(e.target.checked)} />
        <span>{t("folder.readonly")}</span>
      </label>
      <div className={`sub dir-reach ${reachIsProblem(key) ? "attn" : ""}`}>{t(key)}</div>
    </div>
  );
}

/** Another folder for a project: chosen in the folder browser, with a label and whether agents may write in it. */
function AddFolder({ project, environments, onDone, toast, write, canWrite }: { project: Project; environments?: ProjectEnvironments; onDone: () => void; toast: (t: string) => void; write: WriteProject; canWrite: boolean }) {
  const [choice, setChoice] = useState<FolderChoice | null>(null);
  const [label, setLabel] = useState("");
  const [readonly, setReadonly] = useState(false);
  const [busy, setBusy] = useState(false);
  // A folder some project already owns is refused here as in the new-project dialog: two records of
  // one folder are two sets of rules for the same files.
  const usable = !!choice && !choice.problem;
  async function add() {
    if (!usable || busy || !canWrite || !choice) return;
    setBusy(true);
    await write("POST", `/api/projects/${encodeURIComponent(project.id)}/folders`,
      { path: choice.path, label: label.trim(), env: choice.env, readonly }, t("folder.added", { name: label.trim() || choice.path }), onDone);
    setBusy(false);
  }
  return (
    <div className="dir-form">
      <FolderBrowser environments={environments} choice={choice} onChoice={setChoice} toast={toast} launcher={!!window.daedalus?.window} />
      <label className="field" htmlFor="folder-label">{t("folder.label")}</label>
      <input id="folder-label" className="field" value={label} onChange={(e) => setLabel(e.target.value)} placeholder={t("folder.label.placeholder")} />
      <label className="toggle-row">
        <input type="checkbox" checked={readonly} onChange={(e) => setReadonly(e.target.checked)} />
        <span>{t("folder.readonly")}</span>
        <span className="sub">{t("folder.readonly.hint")}</span>
      </label>
      <div className="dir-form-foot">
        <button className="btn ghost" onClick={onDone}>{t("common.cancel")}</button>
        <button className="btn primary" onClick={add} disabled={busy || !canWrite || !usable}>{t("folder.add.action")}</button>
      </div>
    </div>
  );
}

/** Rename it, manage its folders, choose where its agents run, keep a chat's project, or remove it.
 *
 * The project is read live from the list rather than from the copy it was opened with: a folder
 * added or locked here changes the list, and the sheet has to show the change it just made. */
export function ProjectSettingsSheet({ project: opened, onClose, onRemoved, toast }: { project: Project; onClose: () => void; onRemoved: () => void; toast: (t: string) => void }) {
  const projects = useProjects();
  const environments = useEnvironments().data;
  const project = projects.data?.find((p) => p.id === opened.id) ?? opened;
  const draftKey = `daedalus.project.draft.${project.id}`;
  const startingDraft = useRef(savedSettingsDraft(opened.id));
  const [name, setName] = useState(startingDraft.current?.name ?? project.name);
  const [snapshots, setSnapshots] = useState(startingDraft.current?.snapshots ?? project.settings.snapshots);
  const savedEnv: Env = project.settings.default_env ?? environments?.local ?? "container";
  const [defaultEnv, setDefaultEnv] = useState<Env>(startingDraft.current?.defaultEnv ?? savedEnv);
  const savedSetup = project.settings.setup_command ?? "";
  const [setupCommand, setSetupCommand] = useState(startingDraft.current?.setupCommand ?? savedSetup);
  const previousSavedEnv = useRef(savedEnv);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const path = `/api/projects/${encodeURIComponent(project.id)}`;
  const writes = useProjectWrites(project.id, project.entity_revision, !!projects.error, projects.refresh, toast);
  useEffect(() => {
    const draft = savedSettingsDraft(opened.id);
    setName(draft?.name ?? opened.name);
    setSnapshots(draft?.snapshots ?? opened.settings.snapshots);
    setDefaultEnv(draft?.defaultEnv ?? savedEnv);
    setSetupCommand(draft?.setupCommand ?? opened.settings.setup_command ?? "");
  }, [opened.id]);
  useEffect(() => {
    setDefaultEnv((value) => value === previousSavedEnv.current ? savedEnv : value);
    previousSavedEnv.current = savedEnv;
  }, [savedEnv]);
  const setupChanged = setupCommand.trim() !== savedSetup;
  const dirty = name.trim() !== project.name || snapshots !== project.settings.snapshots || defaultEnv !== savedEnv || setupChanged;
  useEffect(() => {
    try {
      if (dirty) sessionStorage.setItem(draftKey, JSON.stringify({ name, snapshots, defaultEnv, setupCommand }));
      else sessionStorage.removeItem(draftKey);
    } catch { /* the mounted form still retains its draft */ }
  }, [draftKey, name, snapshots, defaultEnv, setupCommand, dirty]);
  async function save() {
    if (!dirty || !name.trim() || busy || !writes.ready) return;
    setBusy(true);
    await writes.write("PATCH", path, { name: name.trim(), snapshots, ...(defaultEnv !== savedEnv ? { default_env: defaultEnv } : {}), ...(setupChanged ? { setup_command: setupCommand.trim() } : {}) }, t("common.saved"), () => {
      try { sessionStorage.removeItem(draftKey); } catch { /* local state has the confirmed value */ }
      onClose();
    });
    setBusy(false);
  }
  async function keep() {
    if (!writes.ready || busy) return;
    setBusy(true);
    await writes.write("PATCH", path, { keep: true }, t("project.kept", { name: project.name }));
    setBusy(false);
  }
  const archivable = !project.system && !project.settings.system && !project.settings.ephemeral;
  async function archive(archived: boolean) {
    if (!writes.ready || busy) return;
    if (archived && !(await confirmAsync(t("project.archive.title", { name: project.name }), { body: t("project.archive.body"), action: t("project.archive"), danger: false }))) return;
    setBusy(true);
    // Archiving closes the sheet: the project leaves the list the sheet was opened from.
    await writes.write("PATCH", path, { archived }, t(archived ? "project.archived" : "project.restored", { name: project.name }),
      archived ? () => { if (project.id === storedProject()) rememberProject(""); onClose(); } : undefined);
    setBusy(false);
  }
  async function remove() {
    if (writes.offline || !writes.readCurrent || writes.pending || writes.busy) return;
    const agents = project.sessions.length;
    const running = project.sessions.filter((s) => s.running);
    const body = running.length
      ? plural("project.remove.running", running.length, { names: running.map((s) => s.title).join(", ") })
      : agents
        ? plural("project.remove.agents", agents)
        : t("project.remove.empty");
    if (!(await confirmAsync(t("project.remove.title", { name: project.name }), { body, action: t("common.remove") }))) return;
    try {
      await api.delete(`/api/projects/${encodeURIComponent(project.id)}`);
      afterChange();
      toast(t("project.removed", { name: project.name }));
      onRemoved();
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <Sheet title={project.name} ariaLabel={t("project.settings.for", { name: project.name })} onClose={onClose} size="narrow">
      {project.settings.archived && <p className="sub attn project-archived-note" role="status">{t("project.archived.note")}</p>}
      {(writes.offline || !writes.readCurrent) && <div className="result-warning" role="status">{t("project.write.unverified")} <button type="button" className="linkbtn" disabled={writes.offline} onClick={projects.refresh}>{t("common.retry")}</button></div>}
      {writes.pending && <div className="result-warning" role="status">{t("project.write.pending", { action: writes.pending.label })} <button type="button" className="linkbtn" disabled={writes.offline || !!projects.error || writes.busy} onClick={() => void writes.retry()}>{t("project.write.retry")}</button></div>}
      {writes.conflict !== null && <div className="result-warning" role="status">{t("project.write.conflict")} <button type="button" className="linkbtn" disabled={writes.offline || writes.busy} onClick={() => void writes.reviewConflict()}>{t("project.write.review")}</button></div>}
      <label className="field" htmlFor="project-rename">{t("common.name")}</label>
      <input id="project-rename" className="field" value={name} onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()} />
      {project.settings.ephemeral && (
        <div className="project-ephemeral">
          <span className="sub">{t("project.ephemeral")}</span>
          <button className="btn ghost" onClick={keep} disabled={busy || !writes.ready}>{t("project.keep")}</button>
        </div>
      )}
      <label className="field">{t("project.folders")}</label>
      <div className="dir-list">
        {project.folders.map((folder) => <FolderRow key={folder.id} project={project} folder={folder} environments={environments} write={writes.write} canWrite={writes.ready} />)}
      </div>
      {adding
        ? <AddFolder project={project} environments={environments} onDone={() => setAdding(false)} toast={toast} write={writes.write} canWrite={writes.ready} />
        : <button className="btn ghost dir-add-open" onClick={() => setAdding(true)}><Icon name="plus" size={15} /> {t("folder.add")}</button>}
      {environments && environments.available.length > 1 && (
        <>
          <label className="field" htmlFor="project-default-env">{t("project.defaultenv")}</label>
          <select id="project-default-env" className="field" value={defaultEnv} onChange={(e) => setDefaultEnv(e.target.value as Env)}>
            {environments.available.map((env) => <option key={env} value={env}>{t(`folder.env.${env}.long`)}</option>)}
          </select>
          <div className="sub">{t("project.defaultenv.hint")}</div>
        </>
      )}
      <label className="toggle-row">
        <input type="checkbox" checked={snapshots} onChange={(e) => setSnapshots(e.target.checked)} />
        <span>{t("project.snapshots")}</span>
        <span className="sub">{t("project.snapshots.hint.edit")}</span>
      </label>
      <label className="field" htmlFor="project-setup-command">{t("project.setup")}</label>
      <input id="project-setup-command" className="field mono" value={setupCommand} maxLength={2000}
        spellCheck={false} autoCapitalize="off" autoCorrect="off" onChange={(e) => setSetupCommand(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()} />
      <div className="sub">{t("project.setup.hint")}</div>
      <ProjectBudget projectId={project.id} write={writes.write} canWrite={writes.ready} confirmed={writes.confirmed} />
      <ProjectResources projectId={project.id} write={writes.write} canWrite={writes.ready} />
      <ProjectExtensions projectId={project.id} toast={toast} />
      {/* Restoring a project opens its team page and nothing more. It used to write the project lens
          into storage too, behind the shell's back, so the column narrowed only at the next load,
          with nothing to connect it to the restore. */}
      <ProjectArchive project={project} toast={toast} onChanged={afterChange} readFailed={!!projects.error}
        onOpenRestored={(id) => { onClose(); navigate(projectPagePath(id, "team")); }} />
      {/* Under normal or full autonomy the coordinator holds a standing project grant; the per-task
          permissions only exist for a project that asks first. */}
      {project.settings.orchestrator?.enabled && project.settings.orchestrator.autonomy !== "ask"
        ? <p className="sub">{t("authority.standing", { level: t(`focus.autonomy.${project.settings.orchestrator.autonomy}`) })}</p>
        : <CoordinatorAuthority projectId={project.id} toast={toast} onChanged={() => { afterChange(); projects.refresh(); }} />}
      <ProjectKnowledge projectId={project.id} toast={toast} />
      <ProjectWorktrees projectId={project.id} toast={toast} />
      <details className="sheet-section project-disk">
        <summary>{t("disk.title")}</summary>
        <DiskUsage kind="project" id={project.id} toast={toast} />
      </details>
      <ExecutionHosts toast={toast} />
      <LifecycleCancel kind="project_goal" id={project.id} projectId={project.id} onDone={afterChange} toast={toast} />
      {project.sessions.length > 0 && (
        <>
          <label className="field">{t("project.agents.in")}</label>
          <div className="sub">{project.sessions.map((s) => s.title).join(" · ")}</div>
        </>
      )}
      <div className="sheet-foot">
        <button className="btn danger" onClick={remove} disabled={writes.offline || !writes.readCurrent || !!writes.pending || writes.busy}><Icon name="trash" size={15} /> {t("common.remove")}</button>
        {archivable && <button className="btn ghost" onClick={() => void archive(!project.settings.archived)} disabled={busy || !writes.ready}>{t(project.settings.archived ? "project.restore" : "project.archive")}</button>}
        <button className="btn primary" onClick={save} disabled={!dirty || !name.trim() || busy || !writes.ready}>{t("common.save")}</button>
      </div>
    </Sheet>
  );
}

/** Move one agent into a project or between two projects.
 *
 * Nothing on disk moves. A private choice creates a child in the destination project; the shared
 * choice uses its root. Files in the old directory stay where they were.
 * The installation's own folders are not offered as a destination — the concierge's Projects tool
 * hides the Voice project for the same reason — unless the session is already in one.
 */
export function MoveSessionSheet({ sessionId, current, currentOwn, onClose, onMoved, toast }: { sessionId: string; current: string; currentOwn: boolean; onClose: () => void; onMoved: () => void; toast: (t: string) => void }) {
  const projects = useProjects();
  const [target, setTarget] = useState(current);
  const [ownDirectory, setOwnDirectory] = useState(currentOwn);
  const [busy, setBusy] = useState(false);
  const chosen = (projects.data ?? []).find((p) => p.id === target);
  async function move() {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`/api/sessions/${encodeURIComponent(sessionId)}/project`, { project_id: target, own_directory: ownDirectory });
      afterChange();
      invalidate(`/api/sessions/${sessionId}`);
      toast(t("move.done", { name: chosen?.name ?? "" }));
      onMoved();
      onClose();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("move.title")} onClose={onClose} size="narrow">
      <label className="field" htmlFor="move-project">{t("move.where")}</label>
      <select id="move-project" className="field" value={target} onChange={(e) => { setTarget(e.target.value); setOwnDirectory(false); }}>
        <option value="" disabled>{t("move.choose")}</option>
        {(projects.data ?? []).filter((p) => !p.system || p.id === current).map((p) => (
          <option key={p.id} value={p.id}>{p.name} · {projectPath(p)}</option>
        ))}
      </select>
      {chosen && (
        <>
          <label className="toggle-row">
            <input type="checkbox" checked={ownDirectory} onChange={(e) => setOwnDirectory(e.target.checked)} disabled={!projectReachable(chosen)} />
            <span>{t("move.owndirectory")}</span>
            <span className="sub">{t("move.owndirectory.hint", { root: projectPath(chosen) })}</span>
          </label>
          <div className="sub attn">{ownDirectory ? t("move.warn.separate", { root: projectPath(chosen) }) : t("move.warn.shared", { root: projectPath(chosen) })}</div>
        </>
      )}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" onClick={move} disabled={busy || !target || (target === current && ownDirectory === currentOwn)}>{t("move.action")}</button>
      </div>
    </Sheet>
  );
}
