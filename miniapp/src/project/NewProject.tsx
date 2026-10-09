// "New project": a name, a folder, and Create.
//
// Creating a project is a short action in the middle of other work, so it is a dialog the operator
// returns from, and its quick path is three lines: the name, "New folder" (chosen by default, named
// after the project rather than by a hash), and Create. An existing folder is chosen in the same
// window, through the folder browser, only when asked for. What a project can be told later — the
// snapshots, read-only, the folder's label, where new agents run — waits under "More".
//
// The goal, the rules, the first task and who takes it belong to a project an orchestrator runs, so
// they live in orchestration mode's own dialog, "New orchestration project", which keeps the one
// atomic command that creates the project, its brief and its first task, and every promise that
// command makes about a reply that was lost.
//
// Either dialog is opened from anywhere by `openNewProject()`, so a button in the sidebar, the
// projects list, the palette or a phone's page needs nothing passed down to it.

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, ApiError, type Project, type ProjectEnvironments } from "../api";
import { t } from "../i18n";
import { Icon } from "../icons";
import { navigate, ORCHESTRATION, projectPagePath } from "../router";
import { useMedia } from "../shell";
import { invalidate, useOffline, useQuery } from "../store";
import { errorText } from "../ui";
import { Sheet } from "../ui/dialogs";
import { FolderBrowser, type Env, type FolderChoice } from "./FolderBrowser";
import { folderNameProblem, freeName, slugify } from "./slug";
import "./newproject.css";

export type NewProjectKind = "agents" | "orchestration";

const OPEN_EVENT = "daedalus:new-project";

/** Open the new-project dialog from anywhere. Without a kind it is the dialog of the mode on screen:
 *  a plain project in Agents mode, an orchestrated one in orchestration mode. */
export function openNewProject(kind?: NewProjectKind): void {
  window.dispatchEvent(new CustomEvent<NewProjectKind | undefined>(OPEN_EVENT, { detail: kind }));
}

/** Where the dialogs live: mounted once by the app, listening for `openNewProject`. */
export function NewProjectHost({ mode, toast, onCreated }: { mode: NewProjectKind; toast: (text: string) => void; onCreated: (project: Project) => void }) {
  const [open, setOpen] = useState<{ kind: NewProjectKind; name?: string } | null>(null);
  useEffect(() => {
    const listen = (event: Event) => setOpen({ kind: (event as CustomEvent<NewProjectKind | undefined>).detail ?? mode });
    window.addEventListener(OPEN_EVENT, listen);
    return () => window.removeEventListener(OPEN_EVENT, listen);
  }, [mode]);
  if (!open) return null;
  const close = () => setOpen(null);
  if (open.kind === "orchestration") return <OrchestrationProjectDialog onClose={close} toast={toast} initialName={open.name} />;
  return <NewProjectDialog onClose={close} toast={toast} onCreated={(project) => { close(); onCreated(project); }}
    onOrchestration={(name) => { navigate(ORCHESTRATION); setOpen({ kind: "orchestration", name }); }} />;
}

function useEnvironments() {
  return useQuery<ProjectEnvironments>("/api/project-environments", { staleMs: 60000 });
}

function afterChange(): void {
  invalidate("/api/projects");
  invalidate("/api/sessions");
}

function readStored<T>(key: string): T | null {
  try { return JSON.parse(sessionStorage.getItem(key) ?? "null") as T | null; }
  catch { return null; }
}

function keepStored(key: string, value: unknown | null): void {
  try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, JSON.stringify(value)); }
  catch { /* the mounted form retains the draft and the exact request */ }
}

/** The workspaces folder as the operator reads it: under their home it is `~/…`. */
function shortRoot(environments?: ProjectEnvironments): string {
  const root = environments?.workspaces_root ?? "";
  const home = environments?.home ?? "";
  if (home && (root.startsWith(home + "/") || root.startsWith(home + "\\"))) return "~" + root.slice(home.length);
  return root;
}

/** The new folder's full path, spelled with the root's own separator so a native Windows path
 *  compares equal to the one the server stores. */
function joinRoot(root: string, name: string): string {
  if (!root) return name;
  const separator = /^[A-Za-z]:\\/.test(root) ? "\\" : "/";
  return `${root.replace(/[\\/]+$/, "")}${separator}${name}`;
}

/** The new folder's name: proposed from the project's name until the operator edits it, then theirs.
 *  A proposed name already taken in the workspaces moves on to the next free one; an edited name that
 *  is taken is said out loud instead, because it was chosen on purpose. */
function useNewFolder(name: string, environments: ProjectEnvironments | undefined, initial: { value: string; edited: boolean } | null) {
  const [value, setValue] = useState(initial?.value ?? "");
  const [edited, setEdited] = useState(initial?.edited ?? false);
  const [taken, setTaken] = useState(false);
  const proposed = slugify(name);
  const folder = edited ? value : proposed;
  const root = environments?.workspaces_root ?? "";
  // The free name is for the name it was worked out from: until the check of a newly typed name
  // answers, the plain proposal stands, never the free name of what was typed before.
  const [found, setFound] = useState({ base: folder, free: folder });
  const free = found.base === folder ? found.free : folder;
  const setFree = (value: string) => setFound({ base: folder, free: value });
  useEffect(() => {
    if (!root || folderNameProblem(folder)) { setFree(folder); setTaken(false); return; }
    let stale = false;
    const timer = window.setTimeout(async () => {
      const exists = async (candidate: string) => {
        try {
          const checked = await api.get<{ exists: boolean }>(`/api/folders/check?env=${environments?.local ?? ""}&path=${encodeURIComponent(joinRoot(root, candidate))}`);
          return checked.exists;
        } catch { return false; }
      };
      if (edited) {
        const there = await exists(folder);
        if (!stale) { setTaken(there); setFree(folder); }
        return;
      }
      const known = new Map<string, boolean>();
      for (let n = 1; n < 6; n++) {
        const candidate = n === 1 ? folder : `${folder}-${n}`;
        known.set(candidate, await exists(candidate));
        if (!known.get(candidate)) break;
      }
      if (!stale) { setTaken(false); setFree(freeName(folder, (candidate) => known.get(candidate) ?? false)); }
    }, 300);
    return () => { stale = true; window.clearTimeout(timer); };
  }, [folder, edited, root, environments?.local]);
  return { folder: edited ? value : free, edited, taken, problem: folderNameProblem(edited ? value : free),
    edit: (next: string) => { setEdited(true); setValue(next); }, value, setValue };
}

type Draft = { name: string; mode: "new" | "existing"; folder: string; folderEdited: boolean; choice: FolderChoice | null;
  snapshots: boolean | null; readonly: boolean; label: string; defaultEnv: Env | "" };
type Body = { name: string; folder_name?: string; folders?: { path: string; env?: Env; label?: string; readonly?: boolean }[]; snapshots: boolean; default_env?: Env };

const DRAFT = "daedalus.project.new.draft";
const SEND_TIMEOUT_MS = 15000;

function withTimeout<T>(promise: Promise<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new TimeoutError()), SEND_TIMEOUT_MS);
    promise.then((value) => { window.clearTimeout(timer); resolve(value); }, (error) => { window.clearTimeout(timer); reject(error); });
  });
}

class TimeoutError extends Error {}

/** Whether a failure leaves the request's fate unknown, so it may be sent again: no answer, or the
 *  server's own failure. A refusal (4xx) is an answer, and sending it again would be refused again. */
function uncertain(error: unknown): boolean {
  if (error instanceof TimeoutError) return true;
  if (error instanceof ApiError) return error.status >= 500 || error.status === 408 || error.status === 429;
  return true;
}

export function NewProjectDialog({ onClose, onCreated, onOrchestration, toast }: { onClose: () => void; onCreated: (project: Project) => void;
  onOrchestration: (name: string) => void; toast: (text: string) => void }) {
  const environments = useEnvironments().data;
  const wide = useMedia("(min-width: 1024px)");
  const offline = useOffline();
  const [initial] = useState(() => readStored<Draft>(DRAFT));
  const [name, setName] = useState(initial?.name ?? "");
  const [mode, setMode] = useState<"new" | "existing">(initial?.mode ?? "new");
  const [choice, setChoice] = useState<FolderChoice | null>(initial?.choice ?? null);
  const [extras, setExtras] = useState(false);
  const [snapshots, setSnapshots] = useState<boolean | null>(initial?.snapshots ?? null);
  const [readonly, setReadonly] = useState(initial?.readonly ?? false);
  const [label, setLabel] = useState(initial?.label ?? "");
  const [defaultEnv, setDefaultEnv] = useState<Env | "">(initial?.defaultEnv ?? "");
  const [renaming, setRenaming] = useState(false);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<{ body: Body; message: string } | null>(null);
  const nameInput = useRef<HTMLInputElement>(null);
  const folder = useNewFolder(name, environments, initial ? { value: initial.folder, edited: initial.folderEdited } : null);
  const launcher = !!window.daedalus?.window;
  const local: Env = environments?.local ?? "container";
  const snapshotsOn = snapshots ?? mode === "new";

  useEffect(() => keepStored(DRAFT, { name, mode, folder: folder.value || folder.folder, folderEdited: folder.edited, choice, snapshots, readonly, label, defaultEnv }),
    [name, mode, folder.value, folder.folder, folder.edited, choice, snapshots, readonly, label, defaultEnv]);

  const nameProblem = touched && !name.trim();
  const folderProblem = mode === "new" ? folder.problem || folder.taken : !choice || !!choice.problem;
  const blocked = busy || offline || nameProblem || folderProblem;

  function body(): Body {
    const base: Body = { name: name.trim(), snapshots: snapshotsOn, ...(defaultEnv ? { default_env: defaultEnv } : {}) };
    if (mode === "new") return { ...base, folder_name: folder.folder };
    const picked = choice!;
    return { ...base, folders: [{ path: picked.path, ...(picked.env !== local ? { env: picked.env } : {}),
      ...(label.trim() ? { label: label.trim() } : {}), ...(readonly ? { readonly: true } : {}) }] };
  }

  /** A project the request may have made although its answer was lost: one with the same name and
   *  the same folder. Looking first is what lets "Try again" never make two. */
  async function alreadyMade(sent: Body): Promise<Project | null> {
    const projects = await api.get<Project[]>("/api/projects");
    const wanted = sent.folder_name ? joinRoot(environments?.workspaces_root ?? "", sent.folder_name) : sent.folders?.[0]?.path;
    return projects.find((project) => project.name === sent.name && project.folders.some((f) => f.path === wanted)) ?? null;
  }

  async function send(sent: Body, again: boolean) {
    setBusy(true);
    try {
      const made = again ? await alreadyMade(sent) : null;
      const created = made ?? await withTimeout(api.post<Project>("/api/projects", sent));
      keepStored(DRAFT, null);
      setFailed(null);
      afterChange();
      const unmounted = sent.folders && choice?.unmounted;
      toast(t(unmounted ? "project.added.unmounted" : "project.added", { name: created.name }));
      onCreated(created);
    } catch (error) {
      if (uncertain(error)) setFailed({ body: sent, message: error instanceof TimeoutError ? t("np.failed.timeout") : t("np.failed.server", { reason: errorText(error) }) });
      else toast(errorText(error));
    } finally { setBusy(false); }
  }

  function create() {
    setTouched(true);
    if (!name.trim()) { nameInput.current?.focus(); return; }
    if (blocked) return;
    void send(body(), false);
  }

  function keys(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); create(); }
  }

  const envs = environments?.available ?? [];
  const where = (mode === "existing" ? choice?.env : undefined) ?? local;
  const root = shortRoot(environments);
  const body_ = (
    <div className="np" onKeyDown={keys}>
      <p className="np-intro sub">{t("np.intro")}</p>
      <div className="np-field">
        <label className="np-label" htmlFor="project-name">{t("common.name")}</label>
        <input id="project-name" ref={nameInput} className={`field np-input ${nameProblem ? "bad" : ""}`} autoFocus value={name} maxLength={80}
          aria-invalid={nameProblem} aria-describedby={nameProblem ? "project-name-error" : undefined}
          onChange={(event) => setName(event.target.value)} onBlur={() => name && setTouched(true)} placeholder={t("np.name.placeholder")} />
        {nameProblem && <div id="project-name-error" className="np-error" role="alert"><Icon name="alert" size={13} />{t("np.name.missing")}</div>}
      </div>
      <div className="np-field">
        <div className="np-label">{t("np.folder")}</div>
        <div className="np-choice" role="radiogroup" aria-label={t("np.folder")}>
          <ChoiceCard on={mode === "new"} title={t("np.folder.new")} sub={t("np.folder.new.sub")} onPick={() => setMode("new")} />
          <ChoiceCard on={mode === "existing"} title={t("np.folder.existing")} sub={t(envs.length > 1 || environments?.host_configured ? "np.folder.existing.sub" : "np.folder.existing.sub.one")} onPick={() => setMode("existing")} />
        </div>
      </div>
      {mode === "new" ? (
        <div className={`np-newpath ${folder.problem || folder.taken ? "bad" : ""}`}>
          <Icon name="folder" size={15} />
          {renaming ? (
            <span className="np-newpath-edit mono">
              <span className="sub">{root}/</span>
              <input className="np-slug-input mono" autoFocus value={folder.edited ? folder.value : folder.folder} aria-label={t("np.folder.rename")} spellCheck={false}
                onChange={(event) => folder.edit(event.target.value)}
                onKeyDown={(event) => { if (event.key === "Enter" && !(event.ctrlKey || event.metaKey)) { event.preventDefault(); setRenaming(false); } if (event.key === "Escape") { event.stopPropagation(); setRenaming(false); } }}
                onBlur={() => setRenaming(false)} />
            </span>
          ) : (
            <span className="mono truncate" data-new-folder={folder.folder}>{root}/<span className="np-slug">{folder.folder}</span></span>
          )}
          <span className="grow" />
          {environments?.docker && <span className="sub np-where">{t(`np.where.${local}`)}</span>}
          {!renaming && <button type="button" className="linkbtn" onClick={() => setRenaming(true)}>{t("np.folder.rename")}</button>}
        </div>
      ) : (
        <FolderBrowser environments={environments} choice={choice} onChoice={setChoice} toast={toast} launcher={launcher}
          onOpenProject={(id) => { onClose(); navigate(projectPagePath(id, "team")); }} />
      )}
      {mode === "new" && folder.taken && <div className="np-error" role="alert"><Icon name="alert" size={13} />{t("np.folder.taken")}</div>}
      {mode === "new" && folder.problem && name.trim() && <div className="np-error" role="alert"><Icon name="alert" size={13} />{t("np.folder.bad")}</div>}
      <div className="np-extras">
        <button type="button" className="np-extras-h" aria-expanded={extras} onClick={() => setExtras((open) => !open)}>
          <span className={`chev ${extras ? "down" : ""}`}>›</span>{t("np.more")}
          {!extras && <span className="sub">{t(mode === "new" ? "np.more.sub.new" : "np.more.sub")}</span>}
        </button>
        {extras && (
          <div className="np-extras-body">
            <Toggle id="np-snapshots" on={snapshotsOn} onChange={setSnapshots} title={t("np.snapshots")} sub={t("np.snapshots.sub")} />
            {mode === "existing" && <Toggle id="np-readonly" on={readonly} onChange={setReadonly} title={t("np.readonly")} sub={t("np.readonly.sub")} />}
            {mode === "existing" && (
              <div className="np-field">
                <label className="np-label" htmlFor="np-label">{t("np.label")} <span className="sub">{t("np.optional")}</span></label>
                <input id="np-label" className="field np-input" value={label} maxLength={60} onChange={(event) => setLabel(event.target.value)} placeholder={t("np.label.placeholder")} />
              </div>
            )}
            {envs.length > 1 && (
              <div className="np-field">
                <div className="np-label">{t("np.defaultenv")}</div>
                <Segmented value={defaultEnv || where} options={envs.map((env) => ({ id: env, label: t(`fb.${env}`) }))} onChange={setDefaultEnv} label={t("np.defaultenv")} />
              </div>
            )}
          </div>
        )}
      </div>
      {failed && (
        <div className="np-failed" role="alert">
          <Icon name="alert" size={15} />
          <span className="grow">{failed.message}</span>
          <button type="button" className="btn small" disabled={busy || offline} onClick={() => void send(failed.body, true)}>{t("common.retry")}</button>
        </div>
      )}
      {offline && <p className="sub attn" role="status">{t("result.block.unconfirmed")}</p>}
    </div>
  );
  const foot = (
    <div className={`sheet-foot np-foot ${wide ? "" : "ph-sheet-foot"}`}>
      <button type="button" className="np-hint" onClick={() => onOrchestration(name)}>
        <Icon name="compass" size={13} /><span>{t("np.orch.hint")} <span className="np-hint-link">{t("np.orch.link")}</span></span>
      </button>
      <button type="button" className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
      <button type="button" className="btn primary np-create" onClick={create} disabled={blocked}>
        {t("np.create")}{wide && <kbd aria-hidden>Ctrl ↵</kbd>}
      </button>
    </div>
  );
  return (
    <Sheet title={t("np.title")} onClose={onClose} size={wide ? undefined : "full"} className={`np-sheet ${wide ? "" : "ph-sheet"}`}>
      {body_}
      {foot}
    </Sheet>
  );
}

function ChoiceCard({ on, title, sub, onPick }: { on: boolean; title: string; sub: string; onPick: () => void }) {
  return (
    <button type="button" role="radio" aria-checked={on} className={`np-card ${on ? "on" : ""}`} onClick={onPick}>
      <span className="np-radio" aria-hidden />
      <span className="np-card-text"><b>{title}</b><span className="sub">{sub}</span></span>
    </button>
  );
}

function Toggle({ id, on, onChange, title, sub }: { id: string; on: boolean; onChange: (on: boolean) => void; title: string; sub: string }) {
  return (
    <label className="np-toggle" htmlFor={id}>
      <input id={id} type="checkbox" role="switch" checked={on} onChange={(event) => onChange(event.target.checked)} />
      <span className="np-switch" aria-hidden />
      <span className="np-card-text"><b>{title}</b><span className="sub">{sub}</span></span>
    </label>
  );
}

function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: { id: T; label: ReactNode }[]; onChange: (value: T) => void; label: string }) {
  return (
    <div className="np-seg" role="radiogroup" aria-label={label}>
      {options.map((option) => (
        <button key={option.id} type="button" role="radio" aria-checked={value === option.id} className={value === option.id ? "on" : ""} onClick={() => onChange(option.id)}>{option.label}</button>
      ))}
    </div>
  );
}

// ── orchestration mode ──────────────────────────────────────────────────────────────────────────

type StartDraft = { name: string; folderMode: "new" | "existing"; folder: string; folderEdited: boolean; choice: FolderChoice | null;
  goal: string; constraints: string; taskTitle: string; checks: string; owner: "manual" | "later" };
type StartIntent = { client_operation_id: string; expected_collection_revision: number; name: string;
  goal: string; constraints: string; task_title: string; checks: string[]; owner_intent: "manual" | "later";
  folder?: { path: string; env?: Env }; folder_name?: string };

const START_DRAFT = "daedalus.project.start.draft";
const START_PENDING = "daedalus.project.start.pending";

/** "New orchestration project": the project, its goal and rules, and a first task with how to check
 *  it, in one command. The command is idempotent by its operation id and the collection revision it
 *  read: a lost reply keeps the exact request to send again ("Try again"), and a list that changed
 *  meanwhile is shown for review before a new attempt, never rebased silently. */
export function OrchestrationProjectDialog({ onClose, toast, initialName }: { onClose: () => void; toast: (text: string) => void; initialName?: string }) {
  const environments = useEnvironments().data;
  const wide = useMedia("(min-width: 1024px)");
  const offline = useOffline();
  const [initial] = useState(() => readStored<StartDraft>(START_DRAFT));
  const [name, setName] = useState(initial?.name || initialName || "");
  const [folderMode, setFolderMode] = useState<"new" | "existing">(initial?.folderMode ?? "new");
  const [choice, setChoice] = useState<FolderChoice | null>(initial?.choice ?? null);
  const [goal, setGoal] = useState(initial?.goal ?? "");
  const [constraints, setConstraints] = useState(initial?.constraints ?? "");
  const [taskTitle, setTaskTitle] = useState(initial?.taskTitle ?? "");
  const [checks, setChecks] = useState(initial?.checks ?? "");
  const [owner, setOwner] = useState<"manual" | "later">(initial?.owner ?? "manual");
  const [pending, setPending] = useState<StartIntent | null>(() => readStored<StartIntent>(START_PENDING));
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const folder = useNewFolder(name, environments, initial ? { value: initial.folder, edited: initial.folderEdited } : null);
  const local: Env = environments?.local ?? "container";
  const criteria = useMemo(() => checks.split("\n").map((line) => line.trim()).filter(Boolean), [checks]);
  const tooMany = criteria.length > 12;
  const locked = !!pending || busy;
  useEffect(() => keepStored(START_DRAFT, { name, folderMode, folder: folder.value || folder.folder, folderEdited: folder.edited, choice, goal, constraints, taskTitle, checks, owner }),
    [name, folderMode, folder.value, folder.folder, folder.edited, choice, goal, constraints, taskTitle, checks, owner]);

  const folderProblem = folderMode === "new" ? folder.problem || folder.taken : !choice || !!choice.problem;
  const incomplete = !name.trim() || !goal.trim() || !constraints.trim() || !taskTitle.trim() || !criteria.length || tooMany;
  const disabled = busy || offline || !!pending || conflict || incomplete || folderProblem;

  async function sendStart(intent: StartIntent) {
    if (busy || offline) return;
    setBusy(true);
    try {
      const receipt = await api.post<{ project_id: string; task_id: string; receipt_id: string }>("/api/project-start", intent);
      if (!receipt?.receipt_id || !receipt?.project_id || !receipt?.task_id) throw new Error(t("project.start.badReceipt"));
      const projects = await api.get<Project[]>("/api/projects");
      const created = projects.find((item) => item.id === receipt.project_id);
      if (!created) throw new Error(t("project.start.projectMissing"));
      keepStored(START_PENDING, null);
      keepStored(START_DRAFT, null);
      setPending(null);
      afterChange();
      toast(t("project.start.created"));
      onClose();
      navigate(projectPagePath(created.id, "board", { task: receipt.task_id }));
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409 &&
          !failure.message.includes("command identity was reused with a different request")) {
        keepStored(START_PENDING, null);
        setPending(null);
        setConflict(true);
      }
      toast(errorText(failure));
    } finally { setBusy(false); }
  }

  async function reviewConflict() {
    if (busy || offline) return;
    setBusy(true);
    try {
      await api.get<Project[]>("/api/projects");
      await api.get<{ collection_revision: number }>("/api/control/revisions");
      setConflict(false);
    } catch (failure) { toast(errorText(failure)); }
    finally { setBusy(false); }
  }

  async function create() {
    if (disabled) return;
    setBusy(true);
    try {
      const revisions = await api.get<{ collection_revision: number }>("/api/control/revisions");
      if (!Number.isInteger(revisions.collection_revision)) throw new Error(t("project.start.unavailable"));
      const picked = folderMode === "existing" ? choice : null;
      const intent: StartIntent = { client_operation_id: crypto.randomUUID(),
        expected_collection_revision: revisions.collection_revision, name: name.trim(), goal: goal.trim(),
        constraints: constraints.trim(), task_title: taskTitle.trim(), checks: criteria, owner_intent: owner,
        ...(picked ? { folder: { path: picked.path, ...(picked.env !== local ? { env: picked.env } : {}) } } : { folder_name: folder.folder }) };
      keepStored(START_PENDING, intent);
      setPending(intent);
      setBusy(false);
      await sendStart(intent);
    } catch (failure) { setBusy(false); toast(errorText(failure)); }
  }

  function keys(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void create(); }
  }

  const root = shortRoot(environments);
  return (
    <Sheet title={t("np.orch.title")} onClose={onClose} size={wide ? undefined : "full"} className={`np-sheet np-orch ${wide ? "" : "ph-sheet"}`}>
      <div className="np" onKeyDown={keys}>
        <p className="np-intro sub">{t("np.orch.intro")}</p>
        <div className="np-field">
          <label className="np-label" htmlFor="project-name">{t("common.name")}</label>
          <input id="project-name" className="field np-input" autoFocus value={name} maxLength={80} disabled={locked} onChange={(event) => setName(event.target.value)} placeholder={t("np.name.placeholder")} />
        </div>
        {folderMode === "new" ? (
          <div className={`np-newpath ${folder.problem || folder.taken ? "bad" : ""}`}>
            <Icon name="folder" size={15} />
            {renaming ? (
              <span className="np-newpath-edit mono">
                <span className="sub">{root}/</span>
                <input className="np-slug-input mono" autoFocus value={folder.edited ? folder.value : folder.folder} aria-label={t("np.folder.rename")} spellCheck={false}
                  onChange={(event) => folder.edit(event.target.value)} onBlur={() => setRenaming(false)}
                  onKeyDown={(event) => { if (event.key === "Enter" && !(event.ctrlKey || event.metaKey)) { event.preventDefault(); setRenaming(false); } if (event.key === "Escape") { event.stopPropagation(); setRenaming(false); } }} />
              </span>
            ) : <span className="mono truncate" data-new-folder={folder.folder}>{root}/<span className="np-slug">{folder.folder}</span></span>}
            <span className="grow" />
            {!renaming && !locked && <button type="button" className="linkbtn" onClick={() => setRenaming(true)}>{t("np.folder.rename")}</button>}
            {!locked && <button type="button" className="linkbtn" onClick={() => setFolderMode("existing")}>{t("np.folder.pick")}</button>}
          </div>
        ) : (
          <>
            <FolderBrowser environments={environments} choice={choice} onChoice={setChoice} toast={toast} launcher={!!window.daedalus?.window}
              onOpenProject={(id) => { onClose(); navigate(projectPagePath(id, "team")); }} />
            {!locked && <button type="button" className="linkbtn np-back-new" onClick={() => setFolderMode("new")}>{t("np.folder.backnew")}</button>}
          </>
        )}
        <div className="np-two">
          <div className="np-field">
            <label className="np-label" htmlFor="project-start-goal">{t("project.start.goal")}</label>
            <textarea id="project-start-goal" className="field np-input" rows={2} maxLength={4000} value={goal} disabled={locked} onChange={(event) => setGoal(event.target.value)} />
          </div>
          <div className="np-field">
            <label className="np-label" htmlFor="project-start-constraints">{t("project.start.constraints")}</label>
            <textarea id="project-start-constraints" className="field np-input" rows={2} maxLength={4000} value={constraints} disabled={locked} onChange={(event) => setConstraints(event.target.value)} />
          </div>
        </div>
        <div className="np-two">
          <div className="np-field">
            <label className="np-label" htmlFor="project-start-task">{t("project.start.task")}</label>
            <input id="project-start-task" className="field np-input" maxLength={200} value={taskTitle} disabled={locked} onChange={(event) => setTaskTitle(event.target.value)} />
          </div>
          <div className="np-field">
            <div className="np-label" id="project-start-owner">{t("project.start.owner")}</div>
            <div className="np-seg" role="radiogroup" aria-labelledby="project-start-owner">
              {(["manual", "later"] as const).map((id) => (
                <button key={id} type="button" role="radio" aria-checked={owner === id} disabled={locked} className={owner === id ? "on" : ""} onClick={() => setOwner(id)}>
                  {t(id === "manual" ? "project.start.ownerManual" : "project.start.ownerLater")}
                </button>
              ))}
            </div>
          </div>
        </div>
        <div className="np-field">
          <label className="np-label" htmlFor="project-start-checks">{t("project.start.checks")} <span className="sub">{t("project.start.checksHint")}</span></label>
          <textarea id="project-start-checks" className="field np-input" rows={2} value={checks} disabled={locked} onChange={(event) => setChecks(event.target.value)} />
        </div>
        {tooMany && <p className="sub attn" role="status">{t("project.start.tooMany")}</p>}
        <p className="np-note sub"><Icon name="alert" size={12} /> {t("np.orch.cost")}</p>
        {pending && <div className="np-failed" role="status"><Icon name="alert" size={15} /><span className="grow">{t("project.start.pending")}</span><button type="button" className="btn small" disabled={busy || offline} onClick={() => void sendStart(pending)}>{t("common.retry")}</button></div>}
        {conflict && <div className="np-failed" role="status"><Icon name="alert" size={15} /><span className="grow">{t("project.start.conflict")}</span><button type="button" className="btn small" disabled={busy || offline} onClick={() => void reviewConflict()}>{t("project.start.reviewChange")}</button></div>}
        {offline && <p className="sub attn" role="status">{t("result.block.unconfirmed")}</p>}
      </div>
      <div className={`sheet-foot np-foot ${wide ? "" : "ph-sheet-foot"}`}>
        <span className="np-hint static"><Icon name="compass" size={13} /><span>{t("np.orch.only")}</span></span>
        <button type="button" className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button type="button" className="btn primary np-create" onClick={() => void create()} disabled={disabled}>
          {t("project.start.create")}{wide && <kbd aria-hidden>Ctrl ↵</kbd>}
        </button>
      </div>
    </Sheet>
  );
}
