// A project's board: what waits on the operator, the work in progress, what is in review, the queue and
// what is finished. On a wide window the columns stand side by side; narrower, they become chips over one
// list, because five columns on a phone are five slivers nobody can read. The component takes the
// project by id and nothing else, so the project's focus panel and its phone tab can mount it as it is.

import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Skeleton, copyText } from "../components";
import { OverflowMenu, Sheet } from "../dialogs";
import { useEvent, useStreamUp } from "../events";
import { absTime, relTime } from "../format";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { navigate, pathFor, projectHome, projectPagePath, projectSessionPath } from "../router";
import { PageHeader, useMedia } from "../shell";
import { invalidate, useQuery } from "../store";
import { HarnessBadge, StaffAvatar } from "../team/parts";
import { ReviewPanel } from "./ReviewPanel";
import { Harness, statusTone } from "../team/team";
import { confirmAsync, errorText } from "../ui";
import {
  Arranged,
  BRIEF_FIELDS,
  Brief,
  Column,
  NEXT,
  NeedsYou,
  ProjectBoardData,
  ProjectTask,
  TaskStatus,
  arrange,
  briefChanges,
  chips,
  columnCount,
  emptyBrief,
  mergesOnAccept,
  missingBrief,
  sections,
  statusLine,
  toggleFilter,
} from "./board";

type BoardResponse = ProjectBoardData & { project: { id: string; name: string } };
type Launch = { state: string; position?: number; detail?: string } | null;

const boardKey = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/board`;

/** What the staff runtime did with a newly assigned task, in words. */
function launchText(launch: Launch): string | null {
  if (!launch) return null;
  if (launch.state === "started") return t("pboard.launch.started");
  if (launch.state === "queued") return launch.position ? t("pboard.launch.queued.at", { n: launch.position }) : t("pboard.launch.queued");
  return launch.detail ? t("pboard.launch.refused", { detail: launch.detail }) : t("pboard.launch.other", { state: launch.state });
}

/**
 * `embedded` is the board without a page header of its own and in the list layout whatever the window:
 * a tab of focus mode's right panel, or a phone's board under the project's own header. The open task
 * lives in the address whenever the caller passes `selected`, and in the board otherwise: focus mode's
 * panel passes nothing, because its address belongs to the conversation beside it, while a phone's
 * board is the whole page and a link to one of its tasks (a notification, the review's `?task=`) has to
 * open the task there. Keying this on `embedded` alone once left a phone deaf to that link.
 * `back` is the page header's way back (null for none). `bannered` is the request a "Needs you" banner
 * above the board already shows with its answers: the board leaves it out rather than showing the same
 * question twice, in two different sets of controls, across the top half of a phone.
 */
export function ProjectBoard({ projectId, toast, selected, layout = "auto", embedded = false, back, bannered = null }: { projectId: string; toast: (text: string) => void; selected?: string | null; layout?: "auto" | "list"; embedded?: boolean; back?: string | null; bannered?: string | null }) {
  const [showDone, setShowDone] = useState(false);
  const key = `${boardKey(projectId)}?include_done=${showDone ? 1 : 0}`;
  // While the event stream is up the board is read again when its project changes; the poll is only
  // the fallback for a window without the stream.
  const live = useStreamUp();
  const { data, error, loading, refresh } = useQuery<BoardResponse>(key, { pollMs: live ? 60000 : 10000, staleMs: 3000 });
  useEvent(["task.", "ask.", "permission.", "staff.status"], (event) => {
    if (event.project_id === projectId) invalidate(boardKey(projectId));
  }, [projectId]);
  const wideWindow = useMedia("(min-width: 1024px)");
  const wide = layout === "auto" && !embedded && wideWindow;
  const [picked, setPicked] = useState<string | null>(null);
  const [filter, setFilter] = useState<Column | null>(null);
  const [creating, setCreating] = useState(false);

  const reload = () => {
    invalidate(boardKey(projectId));
    invalidate("/api/board");
    refresh();
  };
  const arranged = useMemo<Arranged>(() => {
    const all = arrange(data ?? { tasks: [], needs_you: [] });
    return bannered ? { ...all, needs: all.needs.filter((need) => need.id !== bannered) } : all;
  }, [data, bannered]);
  const titles = useMemo(() => Object.fromEntries((data?.tasks ?? []).map((task) => [task.id, { title: task.title, status: task.status }])), [data]);
  const inAddress = selected !== undefined;
  const chosen = inAddress ? selected : picked;
  const open = chosen ? data?.tasks.find((task) => task.id === chosen) ?? null : null;
  // A link to a finished task widens the board so the task can be shown.
  useEffect(() => {
    if (chosen && data && !open && !showDone) setShowDone(true);
  }, [chosen, data, open, showDone]);

  const boardPath = projectPagePath(projectId, "board");
  const openTask = (task: ProjectTask) => (inAddress ? navigate(`${boardPath}?task=${encodeURIComponent(task.id)}`) : setPicked(task.id));
  const closeTask = () => (inAddress ? navigate(boardPath, { replace: true }) : setPicked(null));

  async function accept(task: ProjectTask) {
    try {
      await api.post(`/api/board/${encodeURIComponent(task.id)}/accept`);
      toast(t("pboard.accepted", { title: task.title }));
      reload();
    } catch (e) {
      toast(errorText(e));
    }
  }
  function pickChip(column: Column) {
    const next = toggleFilter(filter, column);
    setFilter(next);
    if (next === "done") setShowDone(true);
  }

  const openCount = data ? data.tasks.filter((task) => task.status !== "done" && task.status !== "dropped").length : 0;
  const subtitle = data ? [plural("pboard.count.open", openCount), data.needs_you.length ? plural("pboard.count.needs", data.needs_you.length) : ""].filter(Boolean).join(" · ") : undefined;
  const empty = data && data.tasks.length === 0 && data.needs_you.length === 0 && columnCount("done", arranged, data.counts) === 0;
  const phoneChips = data ? chips(arranged, data.counts) : [];

  const card = (task: ProjectTask) => <TaskCard key={task.id} task={task} titles={titles} onOpen={() => openTask(task)} onAccept={() => accept(task)} />;
  const needCard = (need: NeedsYou) => <NeedCard key={need.id} need={need} projectId={projectId} />;
  const items = (column: Column) => (column === "needs" ? arranged.needs.map(needCard) : arranged[column].map(card));

  const listChips = !wide && phoneChips.length > 0 && (
    <div className="chips pboard-chips" role="group" aria-label={t("pboard.filter")}>
      {phoneChips.map(({ column, count }) => (
        <button key={column} className={`chip select ${column === "needs" ? "need" : ""}`} aria-pressed={filter === column} onClick={() => pickChip(column)}>
          {t(`pboard.col.${column}`)} · {count}
        </button>
      ))}
    </div>
  );
  return (
    <>
      {embedded ? (
        <div className="pboard-bar">
          <span className="sub grow truncate">{subtitle}</span>
          <button className="iconbtn small" onClick={() => setCreating(true)} title={t("pboard.new")} aria-label={t("pboard.new")}><Icon name="plus" size={16} /></button>
        </div>
      ) : (
      <PageHeader
        title={data ? t("pboard.title.of", { name: data.project.name }) : t("pboard.title")}
        subtitle={subtitle}
        back={back === undefined ? pathFor("agents") : back ?? undefined}
        actions={
          <>
            <button className="iconbtn" onClick={() => navigate(projectPagePath(projectId, "team"))} title={t("pboard.team")} aria-label={t("pboard.team")}><Icon name="bots" /></button>
            <button className="iconbtn primary" onClick={() => setCreating(true)} title={t("pboard.new")} aria-label={t("pboard.new")}><Icon name="plus" /></button>
          </>
        }
      >
        {listChips}
      </PageHeader>
      )}
      {embedded && listChips}
      <div className={`screen wide pboard ${wide ? "is-wide" : "is-list"} ${embedded ? "embedded" : ""}`}>
        {loading && !data && !error && <Skeleton rows={4} />}
        {error && !data && (
          <div className="empty">
            <b>{t("pboard.error")}</b>
            <div>{error}</div>
            <button className="btn" onClick={refresh}>{t("common.retry")}</button>
          </div>
        )}
        {empty && (
          <div className="empty">
            <b>{t("pboard.empty")}</b>
            <div>{t("pboard.empty.sub")}</div>
            <button className="btn primary" onClick={() => setCreating(true)}><Icon name="plus" size={15} /> {t("pboard.new")}</button>
          </div>
        )}
        {data && !empty && wide && (
          <div className="pboard-cols">
            {(["needs", "doing", "review", "queue"] as Column[]).map((column) => (
              <section key={column} className={`pboard-col ${column}`} aria-label={t(`pboard.col.${column}`)}>
                <div className="section-title">{t(`pboard.col.${column}`)} <span className="n">{columnCount(column, arranged)}</span></div>
                {items(column)}
                {arranged[column].length === 0 && <div className="pboard-none">{t(`pboard.none.${column}`)}</div>}
              </section>
            ))}
            <section className="pboard-col done" aria-label={t("pboard.col.done")}>
              <button className="section-title pboard-fold" aria-expanded={showDone} onClick={() => setShowDone(!showDone)}>
                <Icon name="chevron" size={14} /> {t("pboard.col.done")} <span className="n">{columnCount("done", arranged, data.counts)}</span>
              </button>
              {showDone && items("done")}
            </section>
          </div>
        )}
        {data && !empty && !wide && (
          <div className="pboard-list">
            {sections(filter, arranged).map((column) => (
              <section key={column} className={`pboard-section ${column}`} aria-label={t(`pboard.col.${column}`)}>
                <div className="section-title">{t(`pboard.col.${column}`)} <span className="n">{columnCount(column, arranged, column === "done" ? data.counts : undefined)}</span></div>
                {items(column)}
                {arranged[column].length === 0 && <div className="pboard-none">{t(`pboard.none.${column}`)}</div>}
              </section>
            ))}
            {filter === null && columnCount("done", arranged, data.counts) > 0 && (
              <button className="section-title pboard-fold" onClick={() => pickChip("done")}>
                <Icon name="chevron" size={14} /> {t("pboard.col.done")} <span className="n">{columnCount("done", arranged, data.counts)}</span>
              </button>
            )}
          </div>
        )}
      </div>
      {creating && data && <TaskSheet projectId={projectId} data={data} onClose={() => setCreating(false)} onDone={reload} toast={toast} />}
      {open && data && <TaskSheet projectId={projectId} data={data} task={open} onClose={closeTask} onDone={reload} onAccept={() => accept(open)} toast={toast} />}
    </>
  );
}

function Who({ name, color, harness }: { name: string; color: string; harness?: Harness }) {
  return (
    <span className="pcard-who">
      <StaffAvatar name={name} color={color} size="small" />
      {harness && <HarnessBadge harness={harness} />}
    </span>
  );
}

function NeedCard({ need, projectId }: { need: NeedsYou; projectId: string }) {
  const who = need.staff ? t("pboard.need.from", { kind: t(`pboard.need.kind.${need.kind}`), name: need.staff.name }) : t("pboard.need.orchestrator", { kind: t(`pboard.need.kind.${need.kind}`) });
  return (
    <div className="pcard need">
      <div className="pcard-title clamp-3">{need.text}</div>
      {need.task_title && <div className="pcard-line faint truncate">{need.task_title}</div>}
      <div className="pcard-meta">
        {need.staff ? <Who name={need.staff.name} color={need.staff.color} /> : <Icon name="question" size={14} />}
        <span className="truncate">{who} · <span title={absTime(need.created_at)}>{relTime(need.created_at)}</span></span>
      </div>
      {need.suggestion && <div className="pcard-line sub">{t("pboard.need.suggestion", { text: need.suggestion })}</div>}
      {/* The short id and the answer get a row of their own, so a narrow column never squeezes who asked to nothing. */}
      <div className="pcard-meta">
        <code className="pcard-short" title={t("pboard.need.short")}>{need.short_id}</code>
        <span className="grow" />
        {need.session_id && (
          <button className="btn small warn" onClick={() => navigate(answerPath(projectId, need))}>{t("pboard.need.answer")}</button>
        )}
      </div>
    </div>
  );
}

/** Where a request is answered: inside the project's focus mode, in the orchestrator's chat for its own
 *  questions (they are cards there) and in the staff member's session for theirs. */
function answerPath(projectId: string, need: NeedsYou): string {
  if (need.origin === "orchestrator") return projectHome(projectId);
  return projectSessionPath(projectId, need.session_id!);
}

function StatusText({ task, titles }: { task: ProjectTask; titles: Record<string, { title: string; status: TaskStatus }> }) {
  const line = statusLine(task, titles);
  if (line.kind === "working") {
    return (
      // The words sit in a span of their own: `.status` is a flex row, and an ellipsis never applies to
      // a flex container's text, so a narrow tablet column cut "needs permission · 4d" to "needs permissic".
      <span className={`status ${statusTone(line.status)} pcard-status`} title={line.waiting || undefined}>
        <span className="dot" aria-hidden />
        <span className="truncate">
          {t(`team.status.${line.status}`)}
          {line.since && <> · {relTime(line.since)}</>}
        </span>
      </span>
    );
  }
  if (line.kind === "after") return <span className="truncate">{line.more ? t("pboard.after.more", { title: line.title, n: line.more }) : t("pboard.after", { title: line.title })}</span>;
  if (line.kind === "waiting") return <span className="truncate">{t("pboard.waiting", { name: line.name })}</span>;
  return null;
}

function TaskCard({ task, titles, onOpen, onAccept }: { task: ProjectTask; titles: Record<string, { title: string; status: TaskStatus }>; onOpen: () => void; onAccept: () => void }) {
  const done = task.checklist.filter((c) => c.done).length;
  return (
    <div
      className={`pcard p${Math.min(task.priority, 4)} ${task.status}`}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onOpen();
        }
      }}
    >
      <div className="pcard-head">
        <span className="pcard-title clamp-3">{task.title}</span>
        {task.priority <= 2 && <span className={`chip tiny ${task.priority === 1 ? "bad" : "attn"}`}>P{task.priority}</span>}
      </div>
      {task.checklist.length > 0 && (
        <div className="task-check">
          <div className={`bar ${done === task.checklist.length ? "ok" : ""}`} style={{ ["--v" as string]: Math.round((100 * done) / task.checklist.length) }}><i /></div>
          <span className="num sub">{done}/{task.checklist.length}</span>
        </div>
      )}
      {task.status === "review" && task.branch && (
        <div className="pcard-review">
          <code className="pcard-branch truncate">{task.branch}</code>
          {task.merge_state === "conflict" && <span className="chip tiny bad">{t("pboard.review.state.conflict")}</span>}
        </div>
      )}
      <div className="pcard-meta">
        {task.assignee && <Who name={task.assignee.name} color={task.assignee.color} harness={task.assignee.harness} />}
        <StatusText task={task} titles={titles} />
        <span className="grow" />
        {task.status === "done" || task.status === "dropped" ? (
          <span className="faint" title={absTime(task.updated_at)}>{task.status === "dropped" ? t("board.col.dropped") : relTime(task.updated_at)}</span>
        ) : null}
        {task.status === "review" && (
          <button className="btn small primary" onClick={(e) => { e.stopPropagation(); onAccept(); }}>{mergesOnAccept(task) ? t("pboard.review.merge") : t("pboard.accept")}</button>
        )}
      </div>
    </div>
  );
}

/** Creating a task and changing one: the same sheet, because a task is its title, its brief, who does it and what it waits for. */
function TaskSheet({ projectId, data, task, onClose, onDone, onAccept, toast }: { projectId: string; data: BoardResponse; task?: ProjectTask; onClose: () => void; onDone: () => void; onAccept?: () => void; toast: (text: string) => void }) {
  const [title, setTitle] = useState(task?.title ?? "");
  const [brief, setBrief] = useState<Brief>(task?.brief ?? emptyBrief());
  const [assignee, setAssignee] = useState(task?.assignee_staff_id ?? "");
  const [deps, setDeps] = useState<string[]>(task?.depends_on ?? []);
  const [priority, setPriority] = useState(task?.priority ?? 3);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  // Only tasks still open can be waited for; a dependency already listed stays offered so it can be removed.
  const candidates = data.tasks.filter((other) => other.id !== task?.id && ((other.status !== "done" && other.status !== "dropped") || deps.includes(other.id)));
  const team = data.staff;
  const gone = task?.assignee && !team.some((m) => m.id === task.assignee!.id) ? task.assignee : null;
  const missing = missingBrief(brief);
  const changed = task
    ? title.trim() !== task.title ||
      Object.keys(briefChanges(task.brief, brief)).length > 0 ||
      assignee !== (task.assignee_staff_id ?? "") ||
      deps.join(",") !== task.depends_on.join(",") ||
      priority !== task.priority ||
      note.trim() !== ""
    : title.trim() !== "";

  function report(result: { launch?: Launch }) {
    const text = launchText(result.launch ?? null);
    if (text) toast(text);
  }

  async function save() {
    setBusy(true);
    try {
      if (!task) {
        const made = await api.post<{ launch?: Launch }>(`${boardKey(projectId)}`, { title: title.trim(), brief, assignee_staff_id: assignee || null, depends_on: deps, priority });
        report(made);
      } else {
        const body: Record<string, unknown> = {};
        if (title.trim() !== task.title) body.title = title.trim();
        const briefDiff = briefChanges(task.brief, brief);
        if (Object.keys(briefDiff).length) body.brief = briefDiff;
        if (assignee !== (task.assignee_staff_id ?? "")) body.assignee_staff_id = assignee;
        if (deps.join(",") !== task.depends_on.join(",")) body.depends_on = deps;
        if (priority !== task.priority) body.priority = priority;
        if (note.trim()) body.note = note.trim();
        report(await api.put<{ launch?: Launch }>(`/api/board/${encodeURIComponent(task.id)}`, body));
      }
      onDone();
      onClose();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function move(status: TaskStatus) {
    if (!task) return;
    try {
      await api.put(`/api/board/${encodeURIComponent(task.id)}`, { status });
      onDone();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function remove() {
    if (!task) return;
    if (!(await confirmAsync(t("board.delete.title", { title: task.title }), { body: t("board.delete.body"), action: t("board.delete.action") }))) return;
    try {
      await api.delete(`/api/board/${encodeURIComponent(task.id)}`);
      onDone();
      onClose();
    } catch (e) {
      toast(errorText(e));
    }
  }
  const toggleDep = (id: string) => setDeps(deps.includes(id) ? deps.filter((d) => d !== id) : [...deps, id]);

  return (
    <Sheet
      title={task ? task.title : t("pboard.new")}
      onClose={onClose}
      className="pboard-sheet"
      head={
        task && (
          <OverflowMenu
            small
            label={t("board.actions")}
            items={[
              ...(task.assignee?.session_id ? [{ label: t("pboard.open.staff", { name: task.assignee.name }), icon: "bots" as const, onSelect: () => navigate(projectSessionPath(projectId, task.assignee!.session_id!)) }] : []),
              { label: t("board.copyid"), icon: "copy", onSelect: async () => toast((await copyText(task.id)) ? t("board.copied") : task.id) },
              "-",
              { label: t("board.delete.menu"), icon: "trash", danger: true, onSelect: remove },
            ]}
          />
        )
      }
    >
      {task && (
        <div className="erow-meta pboard-sheet-status">
          <span className="chip">{t(`board.col.${task.status}`)}</span>
          {task.branch && <code className="pcard-branch">{task.branch}</code>}
          <span className="sep">·</span>
          <span title={absTime(task.updated_at)}>{t("board.updated", { t: relTime(task.updated_at) })}</span>
        </div>
      )}
      {task && task.status === "review" && task.branch && (
        <ReviewPanel taskId={task.id} toast={toast} onMerged={() => { onDone(); onClose(); }} onRejected={onDone} />
      )}
      {task && (NEXT[task.status].length > 0 || task.status === "review") && (
        <div className="btnrow pboard-moves" role="group" aria-label={t("board.moveto")}>
          {task.status === "review" && onAccept && !mergesOnAccept(task) && <button className="btn small primary" onClick={onAccept}><Icon name="check" size={14} /> {t("pboard.accept")}</button>}
          {NEXT[task.status].length > 0 && <span className="sub">{t("board.moveto")}</span>}
          {NEXT[task.status].map((status) => (
            <button key={status} className="btn small" onClick={() => move(status)}>{t(`board.col.${status}`)}</button>
          ))}
        </div>
      )}

      <label className="field" htmlFor="ptask-title">{t("board.title")}</label>
      <input id="ptask-title" className="field" autoFocus={!task} value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />

      <fieldset className="pboard-brief">
        <legend>{t("pboard.brief")}</legend>
        {BRIEF_FIELDS.map((field) => (
          <div key={field}>
            <label className="field" htmlFor={`ptask-${field}`}>{t(`pboard.brief.${field}`)}</label>
            <textarea id={`ptask-${field}`} className="field" rows={2} maxLength={4000} value={brief[field]} placeholder={t(`pboard.brief.${field}.placeholder`)} onChange={(e) => setBrief({ ...brief, [field]: e.target.value })} />
          </div>
        ))}
      </fieldset>

      <label className="field" htmlFor="ptask-assignee">{t("pboard.assignee")}</label>
      {team.length === 0 && !gone ? (
        <div className="sub">
          {t("pboard.assignee.none")} <button className="linkbtn" onClick={() => navigate(projectPagePath(projectId, "team"))}>{t("pboard.team.hire")}</button>
        </div>
      ) : (
        <select id="ptask-assignee" className="field" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
          <option value="">{t("pboard.assignee.nobody")}</option>
          {team.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          {gone && <option value={gone.id}>{t("pboard.assignee.gone", { name: gone.name })}</option>}
        </select>
      )}
      {assignee && missing.length > 0 && (
        <div className="sub attn">{t("pboard.brief.missing", { parts: missing.map((field) => t(`pboard.brief.${field}`)).join(", ") })}</div>
      )}

      <label className="field">{t("pboard.depends")}</label>
      {candidates.length === 0 ? (
        <div className="sub">{t("pboard.depends.none")}</div>
      ) : (
        <div className="pboard-deps" role="group" aria-label={t("pboard.depends")}>
          {candidates.map((other) => (
            <button key={other.id} type="button" className="chip select" aria-pressed={deps.includes(other.id)} onClick={() => toggleDep(other.id)} title={other.title}>
              <span className="truncate">{other.title}</span>
            </button>
          ))}
        </div>
      )}

      <label className="field">{t("board.priority")}</label>
      <div className="segmented inline" role="radiogroup" aria-label={t("board.priority")}>
        {[1, 2, 3, 4, 5].map((p) => (
          <button key={p} type="button" role="radio" aria-checked={priority === p} className={priority === p ? "on" : ""} onClick={() => setPriority(p)}>P{p}</button>
        ))}
      </div>

      {task && (
        <>
          <label className="field" htmlFor="ptask-note">{t("pboard.note")}</label>
          <textarea id="ptask-note" className="field" rows={2} value={note} onChange={(e) => setNote(e.target.value)} placeholder={t("pboard.note.placeholder")} />
          {task.notes && <pre className="inbox-text pboard-notes">{task.notes}</pre>}
        </>
      )}

      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || !title.trim() || !changed} onClick={save}>{task ? t("common.save") : t("common.create")}</button>
      </div>
    </Sheet>
  );
}
