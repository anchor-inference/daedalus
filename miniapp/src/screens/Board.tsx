import { useEffect, useRef, useState } from "react";
import { api, ApiError, Project, SessionList, SessionSummary } from "../api";
import { Skeleton, copyText } from "../ui/components";
import { OverflowMenu, Sheet } from "../ui/dialogs";
import { absTime, relTime } from "../format";
import { useEdgeFade } from "../edgefade";
import { Icon } from "../icons";
import { navigate, pathFor, projectPagePath } from "../router";
import { PageHeader, screenTitle } from "../ui/index";
import { invalidate, useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
// `t` is also the name every task on this screen goes by, so the translator is imported twice: the
// plain name where there is no task in scope, and `t2` inside the functions that take one.
import { plural, t, t as t2 } from "../i18n";
import { useSessionTitles } from "./Sessions";

type Status = "todo" | "doing" | "review" | "done" | "blocked" | "dropped";
type Task = {
  id: string;
  title: string;
  status: Status;
  priority: number;
  acceptance: string;
  checklist: { id?: string; text: string; done: boolean }[];
  entity_revision?: number;
  depends_on: string[];
  session_id: string | null;
  origin_session_id: string | null;
  notes: string;
  created_at: string;
  updated_at: string;
  // A project's task: its four-part brief, and the names the host adds so the card can say whose
  // project it is and who does it. A task outside every project has none of them.
  brief?: Partial<Record<BriefField, string>>;
  project_id?: string | null;
  project_name?: string | null;
  assignee?: { id: string; name: string; color: string } | null;
};
type BriefField = "objective" | "deliverable" | "boundaries" | "done_when";
const BRIEF_FIELDS: BriefField[] = ["objective", "deliverable", "boundaries", "done_when"];

const COLUMNS: Status[] = ["todo", "doing", "review", "blocked"];
const FINISHED: Status[] = ["done", "dropped"];
const columnLabel = (s: Status) => t(`board.col.${s}`);
const NEXT: Record<Status, Status[]> = { todo: ["blocked", "dropped"], doing: [], review: [], blocked: ["todo", "dropped"], done: [], dropped: ["todo"] };

export function BoardScreen({ toast, onOpen, selected, project }: { toast: (t: string) => void; onOpen: (id: string) => void; selected?: string | null; project?: Project | null }) {
  const offline = useOffline();
  const [showDone, setShowDone] = useState(false);
  // The shell's project lens narrows this board as it narrows the list of agents.
  const key = `/api/board?include_done=${showDone ? 1 : 0}${project ? `&project=${encodeURIComponent(project.id)}` : ""}`;
  const { data: tasks, error, loading, refresh } = useQuery<Task[]>(key, { pollMs: 20000, staleMs: 5000 });
  const titles = useSessionTitles();
  const [creating, setCreating] = useState(false);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<Status | null>(null);
  const pending = useRef<{ fingerprint: string; id: string } | null>(null);
  const operationId = (fingerprint: string) => {
    if (pending.current?.fingerprint !== fingerprint) pending.current = { fingerprint, id: crypto.randomUUID() };
    return pending.current.id;
  };

  const reload = () => {
    refresh();
    invalidate("/api/board");
  };
  async function move(t: Task, status: Status) {
    if (t.status === status || !NEXT[t.status].includes(status)) return;
    if (offline || !Number.isInteger(t.entity_revision)) { toast(t2("result.block.unconfirmed")); return; }
    const body = { status, expected_entity_revision: t.entity_revision };
    try {
      await api.put(`/api/board/${encodeURIComponent(t.id)}`, { ...body, client_operation_id: operationId(`${t.id}:${JSON.stringify(body)}`) });
      pending.current = null;
      reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { pending.current = null; reload(); }
      toast(errorText(e));
    }
  }
  async function check(t: Task, i: number) {
    if (offline || !Number.isInteger(t.entity_revision)) { toast(t2("result.block.unconfirmed")); return; }
    try {
      const contract = await api.get<{ checklist: { id: string; text: string }[] }>(`/api/board/${encodeURIComponent(t.id)}/contract`);
      const criterion = contract.checklist[i];
      if (!criterion || criterion.text !== t.checklist[i].text || (t.checklist[i].id && t.checklist[i].id !== criterion.id)) {
        toast(t2("board.check.stale"));
        reload();
        return;
      }
      const body = { [t.checklist[i].done ? "uncheck_ids" : "check_ids"]: [criterion.id], expected_entity_revision: t.entity_revision };
      await api.put(`/api/board/${encodeURIComponent(t.id)}`, { ...body, client_operation_id: operationId(`${t.id}:${JSON.stringify(body)}`) });
      pending.current = null;
      reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { pending.current = null; reload(); }
      toast(errorText(e));
    }
  }
  async function remove(t: Task) {
    if (offline || !Number.isInteger(t.entity_revision)) { toast(t2("result.block.unconfirmed")); return; }
    if (!(await confirmAsync(t2("pboard.archive.title", { title: t.title }), { body: t2("pboard.archive.body"), action: t2("pboard.archive.action"), danger: true }))) return;
    const id = operationId(`${t.id}:archive:${t.entity_revision}`);
    try {
      await api.delete(`/api/board/${encodeURIComponent(t.id)}?client_operation_id=${encodeURIComponent(id)}&expected_entity_revision=${t.entity_revision}`);
      pending.current = null;
      if (selected === t.id) navigate(pathFor("board"), { replace: true });
      reload();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { pending.current = null; reload(); }
      toast(errorText(e));
    }
  }

  const all = tasks ?? [];
  const finished = all.filter((t) => FINISHED.includes(t.status));
  const openCount = all.filter((t) => !FINISHED.includes(t.status)).length;
  const open = selected ? all.find((t) => t.id === selected) ?? null : null;
  // A link to a finished task widens the filter so the task can be shown.
  useEffect(() => {
    if (selected && tasks && !open && !showDone) setShowDone(true);
  }, [selected, tasks, open, showDone]);
  const column = (status: Status) => all.filter((t) => t.status === status).sort((a, b) => a.priority - b.priority || Date.parse(b.updated_at) - Date.parse(a.updated_at));
  const card = (t: Task) => (
    <TaskRow key={t.id} t={t} owner={t.session_id ? titles[t.session_id] : undefined} onOpen={() => navigate(pathFor("board", t.id))} onDragStart={() => setDragging(t.id)} onDragEnd={() => { setDragging(null); setOver(null); }} dragging={dragging === t.id} canDrag={!offline && Number.isInteger(t.entity_revision) && NEXT[t.status].length > 0} />
  );
  const dropProps = (status: Status) => ({
    onDragOver: (e: React.DragEvent) => {
      if (!dragging) return;
      const source = all.find((task) => task.id === dragging);
      if (!source || !NEXT[source.status].includes(status)) return;
      e.preventDefault();
      if (over !== status) setOver(status);
    },
    onDragLeave: (e: React.DragEvent) => {
      if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
      if (over === status) setOver(null);
    },
    onDrop: (e: React.DragEvent) => {
      e.preventDefault();
      const id = dragging ?? e.dataTransfer.getData("text/plain");
      const t = all.find((x) => x.id === id);
      setDragging(null);
      setOver(null);
      if (t && NEXT[t.status].includes(status)) void move(t, status);
    },
  });

  const kanban = useRef<HTMLDivElement>(null);
  useEdgeFade(kanban, `${showDone}:${all.length}`);
  return (
    <>
      <PageHeader
        title={screenTitle("board")}
        subtitle={tasks ? `${plural("board.open.count", openCount)}${finished.length && showDone ? t("board.finished.count", { n: finished.length }) : ""}` : undefined}
        actions={<button className="iconbtn primary" onClick={() => setCreating(true)} title={t("board.new")} aria-label={t("board.new")}><Icon name="plus" /></button>}
      >
        <div className="chips">
          {project && <span className="chip accent">{t("board.lens", { name: project.name })}</span>}
          <button className="chip select" aria-pressed={!showDone} onClick={() => setShowDone(false)}>{t("board.filter.open")}</button>
          <button className="chip select" aria-pressed={showDone} onClick={() => setShowDone(true)}>{t("board.filter.done")}</button>
          {project && !project.system && !project.settings.ephemeral && (
            <button className="chip select" onClick={() => navigate(projectPagePath(project.id, "board"))}>{t("board.lens.project")}</button>
          )}
        </div>
      </PageHeader>
      <div className="screen wide board">
        {loading && !error && <Skeleton rows={4} />}
        {error && !tasks && <div className="empty"><b>{t("board.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {tasks && tasks.length === 0 && (
          <div className="empty">
            <b>{t("board.empty")}</b>
            <div>{t("board.empty.sub")}</div>
            <button className="btn primary" onClick={() => setCreating(true)}><Icon name="plus" size={15} /> {t("board.new")}</button>
          </div>
        )}
        {tasks && tasks.length > 0 && (
          <div ref={kanban} className={`kanban ${showDone ? "five" : ""}`} style={{ gridTemplateColumns: kanbanColumns([...COLUMNS.map((col) => column(col).length), ...(showDone ? [finished.length] : [])]) }}>
            {COLUMNS.map((col) => {
              const items = column(col);
              return (
                <section key={col} className={`kanban-col ${over === col ? "over" : ""} ${items.length === 0 ? "is-empty" : ""}`} {...dropProps(col)}>
                  <div className="section-title">
                    {columnLabel(col)} <span className="n">{items.length}</span>
                  </div>
                  {items.map(card)}
                  {items.length === 0 && <div className="kanban-empty">—</div>}
                </section>
              );
            })}
            {showDone && (
              <section className={`kanban-col ${over === "done" ? "over" : ""}`} {...dropProps("done")}>
                <div className="section-title">
                  {t("board.finished")} <span className="n">{finished.length}</span>
                </div>
                {finished.map(card)}
              </section>
            )}
          </div>
        )}
      </div>
      {creating && <NewTaskSheet onClose={() => setCreating(false)} onCreated={() => { setCreating(false); reload(); }} toast={toast} />}
      {open && <TaskSheet t={open} owner={open.session_id ? titles[open.session_id] : undefined} board={open.origin_session_id ? titles[open.origin_session_id] : undefined} onClose={() => navigate(pathFor("board"), { replace: true })} onMove={move} onCheck={check} onRemove={remove} onOpenSession={onOpen} toast={toast} />}
    </>
  );
}

function TaskRow({ t, owner, onOpen, onDragStart, onDragEnd, dragging, canDrag }: { t: Task; owner?: string; onOpen: () => void; onDragStart: () => void; onDragEnd: () => void; dragging: boolean; canDrag: boolean }) {
  const done = t.checklist.filter((c) => c.done).length;
  const next = t.checklist.find((c) => !c.done);
  const pct = t.checklist.length ? Math.round((100 * done) / t.checklist.length) : 0;
  return (
    <div className={`erow task p${Math.min(t.priority, 4)} ${dragging ? "dragging" : ""}`} role="link" tabIndex={0} draggable={canDrag} onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", t.id); onDragStart(); }} onDragEnd={onDragEnd} onClick={onOpen} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } }}>
      <div className="erow-main">
        <div className="erow-head">
          <span className="erow-title clamp-3">{t.title}</span>
          <span className="erow-time num" title={absTime(t.updated_at)}>{relTime(t.updated_at)}</span>
        </div>
        {t.checklist.length > 0 && (
          <div className="task-check">
            <div className={`bar ${pct === 100 ? "ok" : ""}`} style={{ ["--v" as string]: pct }}><i /></div>
            <span className="num sub">{done}/{t.checklist.length}</span>
          </div>
        )}
        {next && <div className="erow-meta"><span className="faint">{t2("board.next")}</span><span>{next.text}</span></div>}
        <div className="erow-meta">
          {t.priority <= 2 && <span className={`chip ${t.priority === 1 ? "bad" : "attn"}`}>P{t.priority}</span>}
          {t.assignee && <span>{t.assignee.name}</span>}
          {t.assignee && owner && <span className="sep">·</span>}
          {owner && <span>{owner}</span>}
          {t.depends_on.length > 0 && (owner || t.assignee) && <span className="sep">·</span>}
          {t.depends_on.length > 0 && <span>{plural("board.after", t.depends_on.length)}</span>}
        </div>
      </div>
    </div>
  );
}

function TaskSheet({ t, owner, board, onClose, onMove, onCheck, onRemove, onOpenSession, toast }: { t: Task; owner?: string; board?: string; onClose: () => void; onMove: (t: Task, s: Status) => void; onCheck: (t: Task, i: number) => void; onRemove: (t: Task) => void; onOpenSession: (id: string) => void; toast: (t: string) => void }) {
  const offline = useOffline();
  const writeBlocked = offline || !Number.isInteger(t.entity_revision);
  const done = t.checklist.filter((c) => c.done).length;
  // Acceptance that only restates the brief's "Done when" is one section, not two: the pair, one under
  // the other and a clause apart, left nobody able to say what set them apart. The longer words stay.
  const merged = Boolean(t.acceptance) && restates(t.acceptance!, t.brief?.done_when);
  const briefText = (field: BriefField) => (field === "done_when" && merged && t.acceptance!.length > (t.brief?.done_when ?? "").length ? t.acceptance! : t.brief?.[field] ?? "");
  return (
    <Sheet
      title={t.title}
      onClose={onClose}
      head={
        <OverflowMenu
          small
          label={t2("board.actions")}
          items={[
            ...(t.session_id ? [{ label: owner ? t2("board.open.owner", { name: owner }) : t2("board.open.session"), icon: "bots" as const, onSelect: () => onOpenSession(t.session_id!) }] : []),
            { label: t2("board.copyid"), icon: "copy", onSelect: async () => toast((await copyText(t.id)) ? t2("board.copied") : t.id) },
            "-",
            { label: t2("pboard.archive.action"), icon: "trash", danger: true, disabled: writeBlocked || !["todo", "blocked"].includes(t.status), hint: t2("pboard.archive.unavailable"), onSelect: () => onRemove(t) },
          ]}
        />
      }
    >
      <div className="erow-meta" style={{ marginBottom: 10 }}>
        <span className="chip">{columnLabel(t.status)}</span>
        <span className={`chip ${t.priority === 1 ? "bad" : t.priority === 2 ? "attn" : ""}`}>P{t.priority}</span>
        {owner && <span className="sep">·</span>}
        {owner && <button className="linkbtn" onClick={() => onOpenSession(t.session_id!)}>{owner}</button>}
        <span className="sep">·</span>
        <span title={absTime(t.updated_at)}>{t2("board.updated", { t: relTime(t.updated_at) })}</span>
      </div>
      <div className="sub" style={{ marginBottom: 10 }}>
        {t.origin_session_id ? <>{t2("board.of")} <button className="linkbtn" onClick={() => onOpenSession(t.origin_session_id!)}>{board ?? t2("board.of.gone")}</button></> : t2("board.everyone")}
      </div>
      {(t.project_id || t.assignee) && (
        <div className="erow-meta board-task-where" style={{ marginBottom: 10 }}>
          {t.assignee && <span>{t2("board.assignee", { name: t.assignee.name })}</span>}
          {t.assignee && t.project_id && <span className="sep">·</span>}
          {t.project_id && (
            // A link that looks like one, and the project named only when the line above has not
            // just named it: "Board of Bakery site" over "Open on the board of Bakery site" read as
            // the same words twice, with nothing to say the second was a way somewhere.
            <button className="linkbtn accent board-task-open" onClick={() => navigate(projectPagePath(t.project_id!, "board", { task: t.id }))}>
              {(t.project_name ?? t.project_id) === board ? t2("board.on.project.same") : t2("board.on.project", { name: t.project_name ?? t.project_id })}
              <Icon name="forward" size={12} />
            </button>
          )}
        </div>
      )}
      {BRIEF_FIELDS.filter((field) => (t.brief?.[field] ?? "").trim()).map((field) => (
        <section key={field} className="sheet-section board-task-brief">
          <div className="sheet-section-title">{t2(`pboard.brief.${field}`)}</div>
          <div className="proposal-text">{briefText(field)}</div>
        </section>
      ))}
      {t.acceptance && !merged && (
        <section className="sheet-section">
          <div className="sheet-section-title">{t2("board.acceptance")}</div>
          <div className="proposal-text">{t.acceptance}</div>
        </section>
      )}
      {t.checklist.length > 0 && (
        <section className="sheet-section">
          <div className="sheet-section-title">{t2("board.checklist")} <span className="sub">{done}/{t.checklist.length}</span></div>
          {t.checklist.map((c, i) => (
            <label key={i} className="toggle-row check-row">
              <input type="checkbox" checked={c.done} disabled={writeBlocked || t.status === "done"} onChange={() => onCheck(t, i)} />
              <span className={c.done ? "done" : ""}>{c.text}</span>
            </label>
          ))}
        </section>
      )}
      {t.depends_on.length > 0 && (
        <section className="sheet-section">
          <div className="sheet-section-title">{t2("board.depends")}</div>
          <div className="sub mono">{t.depends_on.join(", ")}</div>
        </section>
      )}
      {t.notes && (
        <section className="sheet-section">
          <div className="sheet-section-title">{t2(t.project_id ? "board.history" : "board.notes")}</div>
          <pre className="inbox-text">{t.notes}</pre>
        </section>
      )}
      {NEXT[t.status].length > 0 && <section className="sheet-section">
        <div className="sheet-section-title">{t2("board.moveto")}</div>
        <div className="btnrow" style={{ marginTop: 0 }}>
          {NEXT[t.status].map((s) => (
            <button key={s} className="btn small" disabled={writeBlocked} onClick={() => onMove(t, s)}>
              {columnLabel(s)}
            </button>
          ))}
        </div>
      </section>}
      {writeBlocked && <div className="result-warning" role="status">{t2("result.block.unconfirmed")}</div>}
    </Sheet>
  );
}

function NewTaskSheet({ onClose, onCreated, toast }: { onClose: () => void; onCreated: () => void; toast: (t: string) => void }) {
  const offline = useOffline();
  const operation = useRef<{ fingerprint: string; id: string } | null>(null);
  const collectionRevision = useRef<number | null>(null);
  const [form, setForm] = useState({ title: "", acceptance: "", checklist: "", priority: 3, session_id: "" });
  const [busy, setBusy] = useState(false);
  const { data: listing } = useQuery<SessionList>("/api/sessions", { staleMs: 15000 });
  const sessions = listing?.sessions;
  const agents = (sessions ?? []).filter((s) => !s.title.startsWith("[sub]"));
  async function create() {
    if (offline) { toast(t("result.block.unconfirmed")); return; }
    setBusy(true);
    try {
      if (collectionRevision.current === null) {
        const revisions = await api.get<{ collection_revision: number }>("/api/control/revisions");
        if (!Number.isInteger(revisions.collection_revision)) { toast(t("result.block.unconfirmed")); return; }
        collectionRevision.current = revisions.collection_revision;
      }
      const body = {
        title: form.title,
        acceptance: form.acceptance,
        priority: form.priority,
        checklist: form.checklist.split("\n").map((l) => l.trim()).filter(Boolean),
        session_id: form.session_id || null,
        expected_collection_revision: collectionRevision.current,
      };
      const fingerprint = JSON.stringify(body);
      if (operation.current?.fingerprint !== fingerprint) operation.current = { fingerprint, id: crypto.randomUUID() };
      await api.post("/api/board", { ...body, client_operation_id: operation.current.id });
      operation.current = null;
      collectionRevision.current = null;
      onCreated();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { operation.current = null; collectionRevision.current = null; }
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("board.new")} onClose={onClose}>
      <label className="field">{t("board.title")}</label>
      <input className="field" autoFocus value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} />
      <label className="field">{t("board.acceptance.label")}</label>
      <textarea className="field" rows={2} value={form.acceptance} onChange={(e) => setForm({ ...form, acceptance: e.target.value })} />
      <label className="field">{t("board.checklist.label")}</label>
      <textarea className="field" rows={3} value={form.checklist} onChange={(e) => setForm({ ...form, checklist: e.target.value })} />
      <label className="field">{t("board.priority")}</label>
      {/* P1 and P2 wear the red and amber of the pills on the cards, so the choice previews the mark
          the task will carry; the rest stay neutral, as their pills do. */}
      <div className="segmented inline priority-pick" role="radiogroup">
        {[1, 2, 3, 4, 5].map((p) => (
          <button key={p} role="radio" aria-checked={form.priority === p} className={`${form.priority === p ? "on" : ""} ${p === 1 ? "bad" : p === 2 ? "attn" : ""}`} onClick={() => setForm({ ...form, priority: p })}>P{p}</button>
        ))}
      </div>
      <div className="sub" style={{ marginTop: 4 }}>{t("board.priority.hint")}</div>
      <label className="field">{t("board.board")}</label>
      <select className="field" value={form.session_id} onChange={(e) => setForm({ ...form, session_id: e.target.value })}>
        <option value="">{t("board.board.any")}</option>
        {agents.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}
      </select>
      <div className="sub" style={{ marginTop: 4 }}>{t("board.board.hint")}</div>
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || offline || !form.title.trim()} onClick={create}>{t("common.create")}</button>
      </div>
      {offline && <div className="result-warning" role="status">{t("result.block.unconfirmed")}</div>}
    </Sheet>
  );
}

/** Whether `text` says no more than `other`: one contains the other once case, spaces and the final
 *  full stop are set aside. */
export function restates(text: string, other: string | undefined): boolean {
  const norm = (s: string) => s.toLowerCase().replace(/\s+/g, " ").trim().replace(/[.;:!]+$/, "");
  const a = norm(text);
  const b = norm(other ?? "");
  return Boolean(a && b) && (a.includes(b) || b.includes(a));
}

/**
 * The board's columns on a wide screen: an empty column narrow, a column with cards wide enough for a
 * title on one line, and the board scrolling sideways (its edge faded) when they do not all fit.
 * Equal shares left two empty columns holding half of a desktop around a lone dash, and squeezed five
 * columns on a tablet until every title broke over two lines.
 */
export function kanbanColumns(counts: number[]): string {
  return counts.map((n) => (n === 0 ? "minmax(120px, 0.5fr)" : "minmax(240px, 1fr)")).join(" ");
}
