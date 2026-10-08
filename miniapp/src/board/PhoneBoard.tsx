// A project's board on a phone: chips over one list, as the design draws it. Each chip is a column
// with its count; the list under them is the columns as sections, each folded or open, with a task as
// a borderless row (who does it, how it goes, its priority and its progress). What waits for the
// operator is the first section, a request as an ordinary row with an inline Answer that opens the one
// decision sheet — the board draws no banner of its own, since the Orchestrator and Team tabs carry
// the project's one Needs-you card. A long press, the ⋮ or a right click opens the task's sheet:
// Open, Move to as chips, Refresh, the member's session, Copy id, then Stop and Archive apart.
//
// The desktop board and the board in focus mode's panel are ProjectBoard; this file is only the
// phone's tab, and it reuses ProjectBoard's task sheet for a new task and for an opened one.

import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api";
import { copyText } from "../ui/components";
import { useEvent, useStreamUp } from "../events";
import { clock, relTime } from "../format";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { navigate, projectPagePath, projectSessionPath } from "../router";
import { invalidate, useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { BottomSheet, Chip, ChipBar, EmptyState, IconButton, ListRow, SectionHeader, SegmentedControl, SheetRow, TopBar } from "../ui/phone";
import { HarnessBadge, StaffAvatar } from "../team/parts";
import { statusTone } from "../team/team";
import { DecisionSheet } from "../project/needs";
import { budgetCompact, useGoalBudget } from "../project/ProjectBudget";
import { TaskSheet, type BoardResponse } from "./ProjectBoard";
import { IssueImport } from "./IssueImport";
import { HostCapacity } from "./HostCapacity";
import { UnknownStops } from "./UnknownStops";
import { UncertainLaunches } from "./UncertainLaunches";
import type { AcceptedResultReference } from "./ResultFlow";
import { NEXT, arrange, chips, columnCount, sections, statusLine, toggleFilter, type Arranged, type Column, type NeedsYou, type ProjectTask, type TaskStatus } from "./board";

const enc = encodeURIComponent;
const boardKey = (projectId: string) => `/api/projects/${enc(projectId)}/board`;
/** A section shows this many rows before it offers the rest behind one line: a phone list of 128 open
 *  tasks is a scroll nobody finishes, and the chips already say how many each column holds. */
const SHOWN = 6;

type Titles = Record<string, { title: string; status: TaskStatus }>;

export function PhoneProjectBoard({ projectId, toast, selected, selectedResult }: { projectId: string; toast: (text: string) => void; selected: string | null; selectedResult?: URLSearchParams }) {
  const [showDone, setShowDone] = useState(false);
  const key = `${boardKey(projectId)}?include_done=${showDone ? 1 : 0}`;
  const live = useStreamUp();
  const { data, error, loading, refresh, updatedAt } = useQuery<BoardResponse>(key, { pollMs: live ? 60000 : 10000, staleMs: 3000 });
  useEvent(["task.", "ask.", "permission.", "staff.status"], (event) => {
    if (event.project_id === projectId) invalidate(boardKey(projectId));
  }, [projectId]);
  const [filter, setFilter] = useState<Column | null>(null);
  const [folded, setFolded] = useState<Set<Column>>(() => new Set());
  const [expanded, setExpanded] = useState<Set<Column>>(() => new Set());
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  const [options, setOptions] = useState(false);
  const [deciding, setDeciding] = useState(false);
  const [menu, setMenu] = useState<ProjectTask | null>(null);
  const { data: budget } = useGoalBudget(projectId);

  const reload = () => {
    invalidate(boardKey(projectId));
    invalidate("/api/board");
    refresh();
  };
  const arranged = useMemo<Arranged>(() => arrange(data ?? { tasks: [], needs_you: [] }), [data]);
  const titles = useMemo<Titles>(() => Object.fromEntries((data?.tasks ?? []).map((task) => [task.id, { title: task.title, status: task.status }])), [data]);
  const open = selected && data ? data.tasks.find((task) => task.id === selected) ?? null : null;
  const resultReference: AcceptedResultReference | null = selectedResult?.has("result") ? {
    resultId: selectedResult.get("result") ?? "", revision: Number(selectedResult.get("revision")),
    attemptId: selectedResult.get("attempt") ?? "", digest: selectedResult.get("digest") ?? "",
  } : null;
  // A link to a finished task widens the board so the task can be shown.
  useEffect(() => {
    if (selected && data && !open && !showDone) setShowDone(true);
  }, [selected, data, open, showDone]);

  const boardPath = projectPagePath(projectId, "board");
  const openTask = (task: ProjectTask) => navigate(`${boardPath}?task=${enc(task.id)}`);
  const closeTask = () => navigate(boardPath, { replace: true });
  const pick = (column: Column | null) => {
    const next = column === null ? null : toggleFilter(filter, column);
    setFilter(next);
    if (next === "done") setShowDone(true);
  };
  const toggleFold = (column: Column) => setFolded((f) => {
    const next = new Set(f);
    if (next.has(column)) next.delete(column); else next.add(column);
    return next;
  });

  const openCount = data ? data.tasks.filter((task) => task.status !== "done" && task.status !== "dropped").length : 0;
  const name = data?.project.name ?? "";
  const empty = !!data && data.tasks.length === 0 && data.needs_you.length === 0 && columnCount("done", arranged, data.counts) === 0;
  const sub = !data
    ? (error ? [name, updatedAt ? t("ph.live.confirmed", { time: clock(updatedAt) }) : ""] : [name, t("common.loading")])
    : empty ? [name, t("pbph.notasks")]
    : [name, plural("pbph.open", openCount), data.needs_you.length ? plural("pboard.count.needs", data.needs_you.length) : ""];
  // What is left of the budget closes the line: a task blocked by it says so on its row, and the board
  // is where the operator looks for why.
  const money = budgetCompact(budget);
  const all = data ? data.tasks.length + data.needs_you.length + (showDone ? 0 : columnCount("done", arranged, data.counts) - arranged.done.length) : 0;

  const taskRow = (task: ProjectTask) => <TaskRow key={task.id} task={task} titles={titles} onOpen={() => openTask(task)} onMenu={() => setMenu(task)} />;
  const needRow = (need: NeedsYou) => {
    const task = need.task_id ? data?.tasks.find((x) => x.id === need.task_id) : undefined;
    return <NeedRow key={need.id} need={need} onAnswer={() => setDeciding(true)} onOpen={() => (task ? openTask(task) : setDeciding(true))} />;
  };
  const rows = (column: Column) => {
    const items = column === "needs" ? arranged.needs.map(needRow) : arranged[column].map(taskRow);
    if (expanded.has(column) || filter === column || items.length <= SHOWN + 1) return items;
    return [...items.slice(0, SHOWN),
      <button key="more" type="button" className="ph-board-more" onClick={() => setExpanded((e) => new Set([...e, column]))}>
        {t("pbph.more", { n: items.length - SHOWN, column: t(`pbph.col.${column}`) })}
      </button>];
  };

  return (
    <div className="ph-page ph-board">
      <TopBar title={t("focus.page.board")} sub={<>{sub.filter(Boolean).join(" · ")}{data && money && <> · <span className="project-budget-chip">{money}</span></>}</>} pip={!!data && data.needs_you.length > 0}
        actions={<>
          {data && !empty && <IconButton icon="sliders" label={t("pbph.options")} onClick={() => setOptions(true)} />}
          <IconButton icon="plus" label={t("pboard.new")} onClick={() => setCreating(true)} disabled={!data} />
        </>} />
      {data && !empty && (
        <ChipBar label={t("pboard.filter")}>
          <Chip on={filter === null} count={all} onClick={() => pick(null)}>{t("pbph.all")}</Chip>
          {chips(arranged, data.counts).map(({ column, count }) => (
            <Chip key={column} on={filter === column} tone={column === "needs" ? "warn" : undefined} count={count > 99 ? "99+" : count} onClick={() => pick(column)}>{t(`pboard.col.${column}`)}</Chip>
          ))}
        </ChipBar>
      )}
      {!data && loading && !error && (
        <div className="ph-chips" aria-hidden><span className="ph-chip on"><span className="ph-chip-l">{t("pbph.all")}</span></span>{[110, 100, 80].map((w) => <span key={w} className="ph-sk ph-chip-sk" style={{ width: w }} />)}</div>
      )}
      <div className="ph-page-body list">
        <UnknownStops key={`stops-${projectId}`} projectId={projectId} />
        <UncertainLaunches key={`launches-${projectId}`} projectId={projectId} />
        {!data && loading && !error && <BoardSkeleton />}
        {!data && error && (
          <EmptyState icon="alert" tone="bad" title={t("pboard.error")} body={<>{error}<br />{t("pbph.error.sub")}</>}
            action={<button type="button" className="ph-btn primary" onClick={refresh}>{t("common.retry")}</button>} />
        )}
        {empty && (
          <EmptyState icon="board" title={t("pboard.empty")} body={t("pboard.empty.sub")}
            action={<>
              <button type="button" className="ph-btn accent" onClick={() => setCreating(true)}><Icon name="plus" size={18} />{t("pboard.new")}</button>
              <button type="button" className="ph-link" onClick={() => setImporting(true)}>{t("issues.open")}</button>
            </>} />
        )}
        {data && !empty && (
          <>
            {sections(filter, arranged).map((column) => {
              const fold = folded.has(column) && filter !== column;
              return (
                <section key={column} className={`ph-board-sec ${column}`} aria-label={t(`pboard.col.${column}`)} data-column={column}>
                  <SectionHeader tone={column === "needs" ? "warn" : undefined} count={columnCount(column, arranged, column === "done" ? data.counts : undefined)}
                    action={filter === column ? undefined : <IconButton icon={fold ? "forward" : "chevron"} label={t(fold ? "pbph.unfold" : "pbph.fold", { column: t(`pboard.col.${column}`) })} onClick={() => toggleFold(column)} expanded={!fold} />}>
                    {t(`pboard.col.${column}`)}
                  </SectionHeader>
                  {!fold && rows(column)}
                  {!fold && (column === "needs" ? arranged.needs : arranged[column]).length === 0 && <div className="ph-board-none">{t(`pboard.none.${column}`)}</div>}
                </section>
              );
            })}
            {filter === null && columnCount("done", arranged, data.counts) > 0 && (
              <SectionHeader count={columnCount("done", arranged, data.counts)}
                action={<IconButton icon="forward" label={t("pbph.unfold", { column: t("pboard.col.done") })} onClick={() => pick("done")} />}>
                {t("pboard.col.done")}
              </SectionHeader>
            )}
          </>
        )}
      </div>
      {menu && data && <TaskMenu task={menu} projectId={projectId} titles={titles} toast={toast} onOpen={() => { setMenu(null); openTask(menu); }} onClose={() => setMenu(null)} onDone={reload} />}
      {options && (
        <BottomSheet title={t("pbph.options")} onClose={() => setOptions(false)} className="ph-board-options">
          <div className="ph-gl">{t("pbph.show")}</div>
          <div className="ph-board-opt"><SegmentedControl label={t("pbph.show")} value={showDone ? "done" : "open"} onChange={(v) => setShowDone(v === "done")}
            options={[{ id: "open", label: t("board.filter.open") }, { id: "done", label: t("board.filter.done") }]} /></div>
          <div className="ph-msep" role="separator" />
          <SheetRow icon="download" label={t("issues.open")} onClick={() => { setOptions(false); setImporting(true); }} />
          <div className="ph-board-opt"><HostCapacity projectId={projectId} pending={false} /></div>
          <SheetRow icon="reload" label={t("pboard.launch.refresh")} onClick={() => { reload(); setOptions(false); }} />
        </BottomSheet>
      )}
      {deciding && <DecisionSheet projectId={projectId} toast={toast} onClose={() => setDeciding(false)} />}
      {creating && data && <TaskSheet projectId={projectId} data={data} onClose={() => setCreating(false)} onDone={reload} toast={toast} onImport={() => { setCreating(false); setImporting(true); }} />}
      {importing && <IssueImport projectId={projectId} onClose={() => setImporting(false)} onDone={reload} toast={toast} />}
      {open && data && <TaskSheet key={open.id} projectId={projectId} data={data} task={open} resultReference={resultReference} onClose={closeTask} onDone={reload} toast={toast} />}
    </div>
  );
}

/** The skeleton at the real rows' geometry: a section label, then title and meta lines with the avatar. */
function BoardSkeleton() {
  return (
    <div className="ph-board-sk" aria-busy="true" aria-label={t("common.loading")}>
      {[[44, 54, 30], [24, 61, 34, 50, 40, 38]].map((widths, s) => (
        <div key={s}>
          <span className="ph-sk ph-board-sk-sec" style={{ width: widths[0] * 2 }} />
          {widths.slice(1).map((w, i) => (
            <div key={i} className="ph-board-sk-row">
              <span className="ph-sk" style={{ height: 14, width: `${w}%` }} />
              <span className="ph-board-sk-meta"><span className="ph-sk" style={{ width: 24, height: 24, borderRadius: "50%" }} /><span className="ph-sk" style={{ height: 11, width: `${w - 20}%` }} /></span>
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

/** How a task goes, in the colour of its state: its member's status while they work on it, the task
 *  it waits for, who will take it, or how long ago it finished. */
function statusWords(task: ProjectTask, titles: Titles): { text: string; tone: string } {
  const line = statusLine(task, titles);
  if (line.kind === "working") return { text: [t(`team.status.${line.status}`), line.since ? relTime(line.since) : ""].filter(Boolean).join(" · "), tone: statusTone(line.status) };
  if (line.kind === "after") return { text: line.more ? t("pboard.after.more", { title: line.title, n: line.more }) : t("pboard.after", { title: line.title }), tone: "waiting" };
  if (line.kind === "waiting") return { text: t("pboard.waiting", { name: line.name }), tone: "" };
  if (task.status === "review") return { text: t("pbph.inreview"), tone: "" };
  if (task.status === "done" || task.status === "dropped") return { text: task.status === "dropped" ? t("board.col.dropped") : relTime(task.updated_at), tone: "" };
  return { text: t(`board.col.${task.status}`), tone: "" };
}

function TaskRow({ task, titles, onOpen, onMenu }: { task: ProjectTask; titles: Titles; onOpen: () => void; onMenu: () => void }) {
  const done = task.checklist.filter((c) => c.done).length;
  const words = statusWords(task, titles);
  const pct = task.checklist.length ? Math.round((100 * done) / task.checklist.length) : 0;
  return (
    <ListRow
      className={`ph-task p${Math.min(task.priority, 4)} ${task.status}`}
      data={{ task: task.id }}
      title={task.title}
      label={task.title}
      meta={<>
        {task.assignee && <StaffAvatar name={task.assignee.name} color={task.assignee.color} size="small" />}
        {task.assignee && <HarnessBadge harness={task.assignee.harness} />}
        <span className={`ph-ell ${words.tone}`}>{words.text}</span>
      </>}
      trail={<>
        {/* The issue a task came from stays one tap away, as on the desktop card; the tap opens the
            issue, not the task under it. */}
        {task.issue && <a className={`ph-pill ph-issue ${task.issue.state === "conflict" ? "warn" : ""}`} href={task.issue.url} target="_blank" rel="noreferrer" data-issue={task.issue.number}
          title={t("issues.card.title", { repository: task.issue.repository, n: task.issue.number })} onClick={(e) => e.stopPropagation()} onPointerDown={(e) => e.stopPropagation()}>#{task.issue.number}</a>}
        {task.priority <= 2 && <span className={`ph-pill ${task.priority === 1 ? "bad" : ""}`}>P{task.priority}</span>}
        {task.checklist.length > 0 && (
          <span className="ph-progress">
            <span className={`ph-bar ${pct === 100 ? "ok" : ""}`} style={{ ["--v" as string]: pct }}><i /></span>
            <span className="num">{done}/{task.checklist.length}</span>
          </span>
        )}
      </>}
      onOpen={onOpen}
      onMenu={onMenu}
    />
  );
}

/** A request on the board: the request itself in two lines, who asks and since when,
 *  and Answer, which opens the decision sheet rather than answering in a second set of buttons. */
function NeedRow({ need, onAnswer, onOpen }: { need: NeedsYou; onAnswer: () => void; onOpen: () => void }) {
  const who = need.staff ? t("pboard.need.from", { kind: t(`pboard.need.kind.${need.kind}`), name: need.staff.name }) : t("pboard.need.orchestrator", { kind: t(`pboard.need.kind.${need.kind}`) });
  return (
    <ListRow
      className="ph-task need"
      data={{ need: need.short_id }}
      title={need.text}
      label={need.text}
      meta={<>
        {need.staff ? <><StaffAvatar name={need.staff.name} color={need.staff.color} size="small" /><HarnessBadge harness={need.staff.harness} /></> : <Icon name="ask" size={16} />}
        <span className="ph-ell waiting">{who} · {relTime(need.created_at)}</span>
      </>}
      trail={<>
        <code className="ph-short">{need.short_id}</code>
        <button type="button" className="ph-btn sm warn" onClick={(e) => { e.stopPropagation(); onAnswer(); }} onPointerDown={(e) => e.stopPropagation()}>{t("pboard.need.answer")}</button>
      </>}
      onOpen={onOpen}
    />
  );
}

/**
 * A task's sheet, from a long press, its ⋮ or a right click: the task as its preview, Open, where it
 * can be moved as chips, Refresh, the member's session and Copy id; Stop and Archive come last, apart.
 * Moving, stopping and archiving are the task sheet's own commands, each carrying the task's revision
 * so a board read before someone else's change refuses rather than overwrites it.
 */
function TaskMenu({ task, projectId, titles, toast, onOpen, onClose, onDone }: { task: ProjectTask; projectId: string; titles: Titles; toast: (text: string) => void; onOpen: () => void; onClose: () => void; onDone: () => void }) {
  const offline = useOffline();
  const [busy, setBusy] = useState(false);
  const blocked = offline || !Number.isInteger(task.entity_revision) || busy;
  const done = task.checklist.filter((c) => c.done).length;
  const words = statusWords(task, titles);
  const meta = [task.assignee?.name, words.text, task.checklist.length ? t("pbph.parts", { done, n: task.checklist.length }) : "", t("pbph.id", { id: task.id })].filter(Boolean).join(" · ");
  async function run(work: () => Promise<unknown>, after?: string) {
    if (blocked) { toast(t("result.block.unconfirmed")); return; }
    setBusy(true);
    try {
      await work();
      if (after) toast(after);
      onDone();
      onClose();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) onDone();
      toast(errorText(error));
    } finally { setBusy(false); }
  }
  const move = (status: TaskStatus) => void run(() => api.put(`/api/board/${enc(task.id)}`, { status, expected_entity_revision: task.entity_revision, client_operation_id: crypto.randomUUID() }));
  const stop = async () => {
    if (!(await confirmAsync(t("result.stopTaskTitle", { title: task.title }), { body: t("result.stopTaskBody"), action: t("result.stopTaskAction"), danger: true }))) return;
    void run(() => api.post(`/api/board/${enc(task.id)}/stop`, { expected_entity_revision: task.entity_revision, client_operation_id: crypto.randomUUID(), reason: "operator_requested" }), t("result.stopTaskQueued"));
  };
  const archive = async () => {
    if (!(await confirmAsync(t("pboard.archive.title", { title: task.title }), { body: t("pboard.archive.body"), action: t("pboard.archive.action"), danger: true }))) return;
    void run(() => api.delete(`/api/board/${enc(task.id)}?client_operation_id=${enc(crypto.randomUUID())}&expected_entity_revision=${task.entity_revision}`), t("pboard.archive.saved"));
  };
  const canArchive = ["todo", "blocked"].includes(task.status);
  return (
    <BottomSheet onClose={onClose} className="ph-actions ph-task-menu" label={task.title}
      preview={{ title: <span className="ph-task-preview">{task.title}{task.priority <= 2 && <span className={`ph-pill ${task.priority === 1 ? "bad" : ""}`}>P{task.priority}</span>}</span>, meta }}>
      <div role="menu">
        <SheetRow icon="expand" label={t("common.open")} onClick={onOpen} />
        {NEXT[task.status].length > 0 && (
          <>
            <div className="ph-gl ph-move-label">{t("board.moveto")}</div>
            <div className="ph-move-chips" role="group" aria-label={t("board.moveto")}>
              <Chip on onClick={() => undefined}>{t(`board.col.${task.status}`)}</Chip>
              {NEXT[task.status].map((status) => <Chip key={status} onClick={() => move(status)}>{t(`board.col.${status}`)}</Chip>)}
            </div>
          </>
        )}
        <SheetRow icon="reload" label={t("pboard.launch.refresh")} onClick={() => { onDone(); onClose(); }} />
        {task.assignee?.session_id && <SheetRow icon="bots" label={t("pboard.open.staff", { name: task.assignee.name })} onClick={() => { onClose(); navigate(projectSessionPath(projectId, task.assignee!.session_id!)); }} />}
        <SheetRow icon="copy" label={t("board.copyid")} value={<code>{task.id}</code>} onClick={async () => { onClose(); toast((await copyText(task.id)) ? t("board.copied") : task.id); }} />
        <div className="ph-msep" role="separator" />
        {task.status === "doing" && <SheetRow icon="stop" danger label={t("result.stopTaskAction")} hint={t("result.stopTaskScope")} disabled={blocked} onClick={() => void stop()} />}
        <SheetRow icon="archive" label={t("pboard.archive.action")} hint={canArchive ? undefined : t("pboard.archive.unavailable")} disabled={blocked || !canArchive} onClick={() => void archive()} />
      </div>
    </BottomSheet>
  );
}
