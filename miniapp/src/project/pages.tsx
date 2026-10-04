// The pages of a project that are not the team and the board: the brief, the journal, the wake-ups,
// the folders, the terminals, and the empty state that switches an orchestrator on. Each page is a
// component that works both as the centre of focus mode and as a tab of the right panel (`compact`),
// so there is one brief editor and not two that drift apart.

import { useEffect, useState } from "react";
import { api, type BriefSection, type JournalEntry, type Project } from "../api";
import { Skeleton } from "../ui/components";
import { Sheet } from "../ui/dialogs";
import { absTime, relTime } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { ProjectSettingsSheet } from "../projects";
import { folderName, reachIsProblem, reachKey } from "../folders";
import { navigate, pathFor, projectPagePath } from "../router";
import { go, PageHeader } from "../shell";
import { invalidate, useQuery } from "../store";
import { EnvPill } from "../envpill";
import { TerminalView } from "../terminal/view";
import { TerminalRowMenu } from "../terminal/rowmenu";
import type { Team } from "../team/team";
import { errorText } from "../ui";
import { briefKey, journalKey, staffKey, terminalsKey, useFocus, useProject, useUsage } from "./data";
import { spendLine, totalsLine } from "./usage";
import { AUTONOMIES, type Autonomy } from "./focus";

/** The six sections of a brief, in the host's order (daedalus/stores/projects.py). */
export const BRIEF_SECTIONS = ["goals", "constraints", "preferences", "done_when", "allowed_without_operator", "notes"] as const;

// ── the brief ────────────────────────────────────────────────────────────────────────────────

export function BriefPage({ projectId, back, compact, toast }: { projectId: string; back?: string | null; compact?: boolean; toast: (text: string) => void }) {
  const { data, error, refresh } = useQuery<{ sections: BriefSection[] }>(briefKey(projectId), { staleMs: 5000 });
  const { project } = useProject(projectId);
  const sections = BRIEF_SECTIONS.map((name) => data?.sections.find((s) => s.section === name) ?? { section: name, body: "", updated_at: null, updated_by: null });
  const body = (
    <div className={`brief ${compact ? "compact" : ""}`}>
      {!data && !error && <Skeleton rows={3} />}
      {error && !data && <div className="empty"><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
      {data && sections.map((s) => <BriefCard key={s.section} projectId={projectId} section={s} toast={toast} />)}
    </div>
  );
  if (compact) return body;
  return (
    <>
      <PageHeader title={project ? `${t("focus.brief.title")} · ${project.name}` : t("focus.brief.title")} back={back ?? undefined} />
      <div className="screen narrow">{body}</div>
    </>
  );
}

function BriefCard({ projectId, section, toast }: { projectId: string; section: BriefSection; toast: (text: string) => void }) {
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const name = t(`focus.brief.section.${section.section}`);
  const onlyYou = section.section === "allowed_without_operator";
  async function save() {
    if (draft === null) return;
    setBusy(true);
    try {
      await api.put(briefKey(projectId), { section: section.section, body: draft });
      toast(t("focus.brief.saved", { section: name }));
      setDraft(null);
      invalidate(briefKey(projectId));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className={`brief-card ${section.section}`} aria-label={name}>
      <div className="brief-head">
        <span className="brief-name">{name}</span>
        {onlyYou && <span className="chip tiny attn">{t("focus.brief.onlyyou")}</span>}
        {section.updated_by === "orchestrator" && <span className="chip tiny accent" title={absTime(section.updated_at)}>{t("focus.brief.byorch")}</span>}
        <span className="grow" />
        {draft === null && <button className="btn small ghost" onClick={() => setDraft(section.body)}>{t("common.edit")}</button>}
      </div>
      {onlyYou && draft !== null && <div className="brief-hint sub">{t("focus.brief.hint.allowed_without_operator")}</div>}
      {draft === null ? (
        section.body ? <div className="brief-body">{section.body}</div> : <div className="brief-body empty-line">{t("focus.brief.empty")}</div>
      ) : (
        <>
          <textarea className="field brief-edit" value={draft} rows={Math.min(12, Math.max(3, draft.split("\n").length + 1))} maxLength={20000} autoFocus onChange={(e) => setDraft(e.target.value)} aria-label={name} />
          <div className="btnrow">
            <button className="btn small primary" disabled={busy} onClick={() => void save()}>{t("common.save")}</button>
            <button className="btn small ghost" disabled={busy} onClick={() => setDraft(null)}>{t("common.cancel")}</button>
          </div>
        </>
      )}
    </section>
  );
}

// ── the journal ──────────────────────────────────────────────────────────────────────────────

const JOURNAL_PAGE = 30;
export const JOURNAL_KINDS = ["note", "decision", "plan", "answer", "reassignment", "report", "grant", "hire", "dismiss", "folder", "escalation", "replacement", "brief", "watch", "rule", "rule_lifted"];
/** The longest rule the host keeps (``RULE_TEXT_MAX``): a rule is an instruction, not a document. */
const RULE_MAX = 600;
type JournalPage = { entries: JournalEntry[]; next_before: number | null; rules?: JournalEntry[] };

export function JournalPage({ projectId, back, toast }: { projectId: string; back?: string | null; toast: (text: string) => void }) {
  const { project } = useProject(projectId);
  const { team, board } = useFocus(projectId);
  const usage = useUsage(projectId);
  const orchestratorSpend = spendLine(usage?.orchestrator);
  const first = `${journalKey(projectId)}?limit=${JOURNAL_PAGE}`;
  const { data, error, refresh } = useQuery<JournalPage>(first, { pollMs: 30000, staleMs: 5000 });
  const [older, setOlder] = useState<JournalEntry[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [note, setNote] = useState("");
  const [asRule, setAsRule] = useState(false);
  const [busy, setBusy] = useState(false);
  const [lifting, setLifting] = useState<number | null>(null);
  useEffect(() => {
    setOlder([]);
    setNext(data?.next_before ?? null);
  }, [data]);
  async function more() {
    if (next === null) return;
    setLoadingMore(true);
    try {
      const page = await api.get<JournalPage>(`${journalKey(projectId)}?limit=${JOURNAL_PAGE}&before=${next}`);
      setOlder((held) => [...held, ...page.entries]);
      setNext(page.next_before);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setLoadingMore(false);
    }
  }
  async function add() {
    const text = note.trim();
    if (!text) return;
    setBusy(true);
    try {
      await api.post(journalKey(projectId), asRule ? { text, kind: "rule" } : { text });
      setNote("");
      setAsRule(false);
      toast(t(asRule ? "focus.rules.added" : "focus.journal.added"));
      invalidate(journalKey(projectId));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function lift(rule: JournalEntry) {
    setLifting(rule.id);
    try {
      await api.post(`${journalKey(projectId)}/${rule.id}/lift`, {});
      toast(t("focus.rules.lifted"));
      invalidate(journalKey(projectId));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setLifting(null);
    }
  }
  const rules = data?.rules ?? [];
  const entries = [...(data?.entries ?? []), ...older];
  const names = new Map((team?.staff ?? []).map((m) => [m.id, m.name]));
  const titles = new Map((board?.tasks ?? []).map((task) => [task.id, task.title]));
  return (
    <>
      <PageHeader title={project ? t("focus.journal.title", { name: project.name }) : t("focus.page.journal")} back={back ?? undefined} />
      <div className="screen narrow journal">
        {usage && (
          <div className="journal-usage" aria-label={t("pusage.title")}>
            <span className="num">{totalsLine(usage)}</span>
            {orchestratorSpend && <span className="sub">{t("pusage.orchestrator", { line: orchestratorSpend })}</span>}
          </div>
        )}
        {rules.length > 0 && (
          <section className="journal-rules" aria-label={t("focus.rules.title")}>
            <div className="journal-rules-head">
              <span className="journal-rules-title">{t("focus.rules.title")}</span>
              <span className="sub">{t("focus.rules.hint")}</span>
            </div>
            <ol className="journal-list">
              {rules.map((rule) => (
                <li key={rule.id} className="journal-rule">
                  <div className="journal-meta">
                    <span className={`chip tiny author ${rule.author}`}>{t(`focus.author.${rule.author}`)}</span>
                    <span className="journal-kind num">#{rule.id}</span>
                    <span className="grow" />
                    <time className="journal-time num" dateTime={rule.at} title={absTime(rule.at)}>{relTime(rule.at)}</time>
                    <button className="btn small ghost" disabled={lifting === rule.id} onClick={() => void lift(rule)}>{t("focus.rules.lift")}</button>
                  </div>
                  <div className="journal-text">{rule.text}</div>
                </li>
              ))}
            </ol>
          </section>
        )}
        <form className="journal-note" onSubmit={(e) => { e.preventDefault(); void add(); }}>
          <textarea className="field" rows={2} value={note} maxLength={asRule ? RULE_MAX : 4000} placeholder={t(asRule ? "focus.rules.placeholder" : "focus.journal.placeholder")} aria-label={t(asRule ? "focus.rules.placeholder" : "focus.journal.placeholder")} onChange={(e) => setNote(e.target.value)} />
          <button className="btn small primary" type="submit" disabled={busy || !note.trim()}>{t(asRule ? "focus.rules.add" : "focus.journal.add")}</button>
          <label className="toggle-row journal-as-rule">
            <input type="checkbox" checked={asRule} onChange={(e) => setAsRule(e.target.checked)} />
            <span>{t("focus.rules.as")}</span>
          </label>
        </form>
        {!data && !error && <Skeleton rows={4} />}
        {error && !data && <div className="empty"><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {data && entries.length === 0 && <div className="empty"><b>{t("focus.journal.empty")}</b></div>}
        <ol className="journal-list">
          {entries.map((entry) => (
            <li key={entry.id} className={`journal-entry ${entry.author}`}>
              <div className="journal-meta">
                <span className={`chip tiny author ${entry.author}`}>{t(`focus.author.${entry.author}`)}</span>
                <span className="journal-kind">{JOURNAL_KINDS.includes(entry.kind) ? t(`focus.kind.${entry.kind}`) : entry.kind}</span>
                {entry.lifted && <span className="chip tiny">{t("focus.rules.liftedChip")}</span>}
                <span className="grow" />
                <time className="journal-time num" dateTime={entry.at} title={absTime(entry.at)}>{relTime(entry.at)}</time>
              </div>
              <div className="journal-text">{entry.text}</div>
              <JournalRefs projectId={projectId} refs={entry.refs} names={names} titles={titles} />
            </li>
          ))}
        </ol>
        {next !== null && (
          <button className="btn ghost load-more" disabled={loadingMore} onClick={() => void more()}>{t(loadingMore ? "common.loading" : "focus.journal.older")}</button>
        )}
      </div>
    </>
  );
}

function JournalRefs({ projectId, refs, names, titles }: { projectId: string; refs: Record<string, string>; names: Map<string, string>; titles: Map<string, string> }) {
  const links: { key: string; label: string; href: string }[] = [];
  if (refs?.staff_id) links.push({ key: "s", label: names.get(refs.staff_id) ?? refs.staff_id, href: projectPagePath(projectId, "team") });
  if (refs?.task_id) links.push({ key: "t", label: `${t("focus.ref.task")} ${titles.get(refs.task_id) ?? refs.task_id}`, href: projectPagePath(projectId, "board", { task: refs.task_id }) });
  if (refs?.ask_id) links.push({ key: "a", label: t("focus.ref.ask", { id: refs.ask_id }), href: projectPagePath(projectId, "board") });
  if (links.length === 0) return null;
  return (
    <div className="journal-refs">
      {links.map((link) => (
        <a key={link.key} className="chip tiny link" href={link.href} onClick={(e) => { e.preventDefault(); navigate(link.href); }}>{link.label}</a>
      ))}
    </div>
  );
}

// ── the wake-ups ─────────────────────────────────────────────────────────────────────────────

export { WakeupsPage } from "./wakeups";

// ── the folders ──────────────────────────────────────────────────────────────────────────────

export function FoldersPage({ projectId, back, compact, toast }: { projectId: string; back?: string | null; compact?: boolean; toast: (text: string) => void }) {
  const { project } = useProject(projectId);
  const [managing, setManaging] = useState(false);
  if (!project) return <Skeleton rows={2} />;
  const body = (
    <div className="focus-folders">
      {project.folders.map((folder, i) => (
        <div key={folder.id} className="focus-folder">
          <Icon name="folder" size={16} />
          <div className="focus-folder-main">
            <div className="focus-folder-name truncate">
              {folderName(folder)}
              {i === 0 && <span className="chip tiny">{t("folder.primary")}</span>}
              {folder.readonly && <span className="chip tiny">{t("project.readonly.short")}</span>}
            </div>
            <FolderPath path={folder.path} />
            {reachIsProblem(reachKey(folder)) && <div className="focus-folder-warn"><Icon name="alert" size={12} /><span>{t(reachKey(folder))}</span></div>}
          </div>
          <EnvPill env={folder.env} />
        </div>
      ))}
      <button className="btn small" onClick={() => setManaging(true)}><Icon name="settings" size={14} /> {t("focus.folders.manage")}</button>
      {managing && <ProjectSettingsSheet project={project} onClose={() => setManaging(false)} onRemoved={() => setManaging(false)} toast={toast} />}
    </div>
  );
  if (compact) return body;
  return (
    <>
      <PageHeader title={`${t("focus.folders.title")} · ${project.name}`} back={back ?? undefined} />
      <div className="screen narrow">{body}</div>
    </>
  );
}

// ── the terminals ────────────────────────────────────────────────────────────────────────────

/** The project's terminals, one of them on screen. The session dock holds a session's own; this is
 *  every terminal of the project, whoever opened it, in one place. */
export function TerminalsPage({ projectId, selected, back, toast }: { projectId: string; selected: string | null; back?: string | null; toast: (text: string) => void }) {
  const { project } = useProject(projectId);
  const { data } = useQuery<{ terminals: import("../api").TerminalView[] }>(terminalsKey(projectId), { pollMs: 10000, staleMs: 3000 });
  const terminals = data?.terminals ?? [];
  const current = terminals.find((term) => term.id === selected) ?? null;
  return (
    <>
      <PageHeader
        title={project ? t("focus.terminals.title", { name: project.name }) : t("focus.page.terminals")}
        back={back ?? undefined}
        actions={
          // The rows stay here, inside the project's column; the Terminals screen's own view (a grid,
          // End, the owner) is one step away rather than the place a row leads, which would leave focus mode.
          current ? (
            <a className="iconbtn" href={pathFor("terminals", current.id)} onClick={(e) => go(e, pathFor("terminals", current.id))} aria-label={t("term.maximize")} title={t("term.maximize")}>
              <Icon name="expand" />
            </a>
          ) : undefined
        }
      />
      <div className="focus-terminals">
        <div className="focus-terminal-list">
          {data && terminals.length === 0 && <div className="empty calm">{t("focus.terminals.empty")}</div>}
          {terminals.map((term) => (
            <div key={term.id} className={`focus-term-row ${term.id === selected ? "current" : ""}`} data-terminal={term.id}>
              <button className={`focus-row ${term.id === selected ? "current" : ""} ${term.status === "running" ? "" : "ended"}`} onClick={() => navigate(projectPagePath(projectId, "terminals", { t: term.id }), { replace: !!selected })}>
                <Icon name="terminal" size={16} />
                <span className="focus-row-label truncate">{term.title || t("term.untitled")}</span>
                <EnvPill env={term.env} />
                {term.status !== "running" && term.exit_code !== null && <span className="focus-row-meta">{t("focus.terminal.code", { code: term.exit_code })}</span>}
              </button>
              {/* A removed terminal that was on screen leaves the page on the list, not on a view of nothing. */}
              <TerminalRowMenu row={term} toast={toast} onRemoved={term.id === selected ? () => navigate(projectPagePath(projectId, "terminals"), { replace: true }) : undefined} />
            </div>
          ))}
        </div>
        <div className="focus-terminal-view">
          {current ? <TerminalView key={current.id} id={current.id} visible env={current.env} /> : terminals.length > 0 && <div className="empty calm">{t("focus.terminals.pick")}</div>}
        </div>
      </div>
    </>
  );
}

// ── switching the orchestrator on ────────────────────────────────────────────────────────────

/** The centre of a project without an orchestrator: what one is, and the way to switch it on. */
export function EnableOrchestrator({ project, toast, back }: { project: Project; toast: (text: string) => void; back?: string | null }) {
  const [open, setOpen] = useState(false);
  // The header every other page of the project carries: without it a tablet with its column folded,
  // or a phone, showed the prompt alone, with no project name and no way back.
  const head = <PageHeader title={project.name} back={back ?? undefined} />;
  if (project.settings.ephemeral) {
    return <>{head}<div className="empty"><b>{t("focus.enable.closed", { name: project.name })}</b></div></>;
  }
  return (
    <>
    {head}
    <div className="screen narrow focus-enable">
      <div className="empty">
        <Icon name="conductor" size={28} />
        <b>{t("focus.enable.title", { name: project.name })}</b>
        <div>{t("focus.enable.body")}</div>
        <button className="btn primary" onClick={() => setOpen(true)}>{t("focus.enable.action")}</button>
      </div>
      {open && <EnableSheet project={project} onClose={() => setOpen(false)} toast={toast} />}
    </div>
    </>
  );
}

function EnableSheet({ project, onClose, toast }: { project: Project; onClose: () => void; toast: (text: string) => void }) {
  const { data: team } = useQuery<Team>(staffKey(project.id), { staleMs: 5000 });
  const settings = project.settings.orchestrator;
  const [model, setModel] = useState(settings?.model ?? "");
  const [autonomy, setAutonomy] = useState<Autonomy>(settings?.autonomy ?? "normal");
  const [cap, setCap] = useState(settings?.concurrency_cap ?? 10);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const modelRefused = /^coordinator model .+ cannot run with spending limits:/i.test(error);
  const presets = team?.choices.presets ?? [];
  const fallback = presets.find((p) => p.id === team?.choices.coordinator_default_preset)?.label ?? "";
  async function go() {
    setBusy(true);
    setError("");
    try {
      await api.post(`/api/projects/${encodeURIComponent(project.id)}/orchestrator`, { model, autonomy, concurrency_cap: cap });
      toast(t("focus.enable.done"));
      invalidate("/api/projects");
      invalidate("/api/sessions");
      onClose();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("focus.enable.sheet", { name: project.name })} onClose={onClose} size="narrow" className="enable-sheet">
      <p className="focus-enable-intro">{t("focus.enable.first")}</p>
      <p className="focus-cost">{t("focus.enable.cost")}</p>
      {error && <p className="form-hint" role="alert">{modelRefused ? t("focus.enable.model.refused") : error}</p>}
      {modelRefused && <p className="sub form-hint">{error}</p>}
      <details className="focus-enable-advanced" open={error ? true : undefined}>
      <summary>{t("focus.enable.advanced")}</summary>
      <label className="field" htmlFor="orch-model">{t("focus.enable.model")}</label>
      <select id="orch-model" className="field" value={model} onChange={(e) => { setModel(e.target.value); setError(""); }}>
        <option value="">{fallback ? t("focus.enable.model.default", { name: fallback }) : t("team.model.default.short")}</option>
        {presets.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
      </select>
      <div className="sub form-hint">{t("focus.enable.model.hint")}</div>
      <fieldset className="autonomy-choice">
        <legend className="field">{t("focus.enable.autonomy")}</legend>
        {AUTONOMIES.map((level) => (
          <label key={level} className={`autonomy-option ${autonomy === level ? "on" : ""}`}>
            <input type="radio" name="autonomy" value={level} checked={autonomy === level} onChange={() => setAutonomy(level)} />
            <span className="autonomy-text">
              <b>{t(`focus.autonomy.${level}`)}</b>
              <span className="sub">{t(`focus.autonomy.${level}.hint`)}</span>
            </span>
          </label>
        ))}
      </fieldset>
      <label className="field" htmlFor="orch-cap">{t("focus.enable.cap")}</label>
      <input id="orch-cap" className="field" type="number" min={1} max={32} value={cap} onChange={(e) => setCap(Math.max(1, Math.min(32, Number(e.target.value) || 1)))} />
      <div className="sub form-hint">{t("focus.enable.cap.hint")} {plural("team.count.staff", cap)}</div>
      </details>
      <div className="btnrow">
        <button className="btn primary" disabled={busy} onClick={() => void go()}>{t("focus.enable.go")}</button>
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
      </div>
    </Sheet>
  );
}

/**
 * A folder's path that gives way in the middle: the start ellipsizes and the last segment always
 * shows. A plain end ellipsis on a phone cut "/home/operator/documents/courier" to ".../cou…", losing
 * the one part that says which folder it is.
 */
function FolderPath({ path }: { path: string }) {
  const cut = path.replace(/\/+$/, "").lastIndexOf("/");
  const head = cut > 0 ? path.slice(0, cut + 1) : "";
  const tail = cut > 0 ? path.slice(cut + 1) : path;
  return (
    <div className="focus-folder-path mono" title={path}>
      {head && <span className="focus-folder-path-head">{head}</span>}
      <span className="focus-folder-path-tail">{tail}</span>
    </div>
  );
}
