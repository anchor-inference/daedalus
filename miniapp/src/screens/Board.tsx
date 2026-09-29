import { useEffect, useState } from "react";
import { api, Project, SessionList, SessionSummary } from "../api";
import { Skeleton, copyText } from "../components";
import { OverflowMenu, Sheet } from "../dialogs";
import { absTime, relTime } from "../format";
import { Icon } from "../icons";
import { navigate, pathFor, projectPagePath } from "../router";
import { PageHeader, screenTitle } from "../shell";
import { invalidate, useQuery } from "../store";
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
  checklist: { text: string; done: boolean }[];
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
const NEXT: Record<Status, Status[]> = { todo: ["doing", "blocked", "dropped"], doing: ["review", "done", "blocked", "todo"], review: ["done", "doing"], blocked: ["todo", "doing"], done: ["todo"], dropped: ["todo"] };

export function BoardScreen({ toast, onOpen, selected, project }: { toast: (t: string) => void; onOpen: (id: string) => void; selected?: string | null; project?: Project | null }) {
  const [showDone, setShowDone] = useState(false);
  // The shell's project lens narrows this board as it narrows the list of agents.
  const key = `/api/board?include_done=${showDone ? 1 : 0}${project ? `&project=${encodeURIComponent(project.id)}` : ""}`;
  const { data: tasks, error, loading, refresh } = useQuery<Task[]>(key, { pollMs: 20000, staleMs: 5000 });
  const titles = useSessionTitles();
  const [creating, setCreating] = useState(false);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<Status | null>(null);

  const reload = () => {
    refresh();
    invalidate("/api/board");
  };
  async function move(t: Task, status: Status) {
    if (t.status === status) return;
    try {
      await api.put(`/api/board/${t.id}`, { status });
      reload();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function check(t: Task, i: number) {
    // The board refuses a checklist edit that would leave a finished task with an item open; the
    // refusal shows as a toast, not as a dead checkbox.
    try {
      await api.put(`/api/board/${t.id}`, t.checklist[i].done ? { uncheck: [i] } : { check: [i] });
      reload();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function remove(t: Task) {
    if (!(await confirmAsync(t2("board.delete.title", { title: t.title }), { body: t2("board.delete.body"), action: t2("board.delete.action") }))) return;
    try {
      await api.delete(`/api/board/${t.id}`);
      if (selected === t.id) navigate(pathFor("board"), { replace: true });
      reload();
    } catch (e) {
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
    <TaskRow key={t.id} t={t} owner={t.session_id ? titles[t.session_id] : undefined} onOpen={() => navigate(pathFor("board", t.id))} onDragStart={() => setDragging(t.id)} onDragEnd={() => { setDragging(null); setOver(null); }} dragging={dragging === t.id} />
  );
  const dropProps = (status: Status) => ({
    onDragOver: (e: React.DragEvent) => {
      if (!dragging) return;
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
      if (t) void move(t, status);
    },
  });

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
          <div className={`kanban ${showDone ? "five" : ""}`}>
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

function TaskRow({ t, owner, onOpen, onDragStart, onDragEnd, dragging }: { t: Task; owner?: string; onOpen: () => void; onDragStart: () => void; onDragEnd: () => void; dragging: boolean }) {
  const done = t.checklist.filter((c) => c.done).length;
  const next = t.checklist.find((c) => !c.done);
  const pct = t.checklist.length ? Math.round((100 * done) / t.checklist.length) : 0;
  return (
    <div className={`erow task p${Math.min(t.priority, 4)} ${dragging ? "dragging" : ""}`} role="link" tabIndex={0} draggable onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", t.id); onDragStart(); }} onDragEnd={onDragEnd} onClick={onOpen} onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } }}>
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
  const done = t.checklist.filter((c) => c.done).length;
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
            { label: t2("board.delete.menu"), icon: "trash", danger: true, onSelect: () => onRemove(t) },
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
            <button className="linkbtn" onClick={() => navigate(projectPagePath(t.project_id!, "board", { task: t.id }))}>
              {t2("board.on.project", { name: t.project_name ?? t.project_id })}
            </button>
          )}
        </div>
      )}
      {BRIEF_FIELDS.filter((field) => (t.brief?.[field] ?? "").trim()).map((field) => (
        <section key={field} className="sheet-section board-task-brief">
          <div className="sheet-section-title">{t2(`pboard.brief.${field}`)}</div>
          <div className="proposal-text">{t.brief![field]}</div>
        </section>
      ))}
      {t.acceptance && (
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
              <input type="checkbox" checked={c.done} onChange={() => onCheck(t, i)} />
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
      <section className="sheet-section">
        <div className="sheet-section-title">{t2("board.moveto")}</div>
        <div className="btnrow" style={{ marginTop: 0 }}>
          {/* "Done" is filled only once the checklist is closed: filled on every task it read as the
              state the task was already in, beside a status chip saying "Doing" and a checklist at 2/3.
              A task without a checklist gives no such sign, so its "Done" stays plain too. */}
          {NEXT[t.status].map((s) => (
            <button key={s} className={`btn small ${s === "done" && t.checklist.length > 0 && done === t.checklist.length ? "primary" : ""}`} onClick={() => onMove(t, s)}>
              {columnLabel(s)}
            </button>
          ))}
        </div>
      </section>
    </Sheet>
  );
}

function NewTaskSheet({ onClose, onCreated, toast }: { onClose: () => void; onCreated: () => void; toast: (t: string) => void }) {
  const [form, setForm] = useState({ title: "", acceptance: "", checklist: "", priority: 3, session_id: "" });
  const [busy, setBusy] = useState(false);
  const { data: listing } = useQuery<SessionList>("/api/sessions", { staleMs: 15000 });
  const sessions = listing?.sessions;
  const agents = (sessions ?? []).filter((s) => !s.title.startsWith("[sub]"));
  async function create() {
    setBusy(true);
    try {
      await api.post("/api/board", {
        title: form.title,
        acceptance: form.acceptance,
        priority: form.priority,
        checklist: form.checklist.split("\n").map((l) => l.trim()).filter(Boolean),
        session_id: form.session_id || null,
      });
      onCreated();
    } catch (e) {
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
      <div className="segmented inline" role="radiogroup">
        {[1, 2, 3, 4, 5].map((p) => (
          <button key={p} role="radio" aria-checked={form.priority === p} className={form.priority === p ? "on" : ""} onClick={() => setForm({ ...form, priority: p })}>P{p}</button>
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
        <button className="btn primary" disabled={busy || !form.title.trim()} onClick={create}>{t("common.create")}</button>
      </div>
    </Sheet>
  );
}
