// A project's board: what waits on the operator, the work in progress, what is in review, the queue and
// what is finished. On a wide window the columns stand side by side; narrower, they become chips over one
// list, because five columns on a phone are five slivers nobody can read. The component takes the
// project by id and nothing else, so the project's focus panel and its phone tab can mount it as it is.

import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type Project } from "../api";
import { Skeleton, copyText } from "../ui/components";
import { OverflowMenu, Sheet, type MenuItem } from "../ui/dialogs";
import { useEvent, useStreamUp } from "../events";
import { absTime, relTime } from "../format";
import { Icon, type IconName } from "../icons";
import { plural, t } from "../i18n";
import { navigate, pathFor, projectHome, projectPagePath, projectSessionPath, useRoute } from "../router";
import { PageHeader, useMedia } from "../shell";
import { invalidate, useOffline, useQuery } from "../store";
import { HarnessBadge, StaffAvatar } from "../team/parts";
import { LifecycleCancel } from "../project/LifecycleCancel";
import { CoordinatorAuthority } from "../project/CoordinatorAuthority";
import { waitKey } from "../project/focus";
import { ReviewPanel } from "./ReviewPanel";
import { TaskDiagnostics } from "./TaskDiagnostics";
import { AcceptedResultDetail, ResultFlow, type AcceptedResultReference } from "./ResultFlow";
import { TaskWorkflow } from "./TaskWorkflow";
import { TaskComparison } from "./TaskComparison";
import { TaskExplorations } from "./TaskExplorations";
import { RuntimeHandoff } from "./RuntimeHandoff";
import { HostCapacity } from "./HostCapacity";
import { UnknownStops } from "./UnknownStops";
import { UncertainLaunches } from "./UncertainLaunches";
import { TaskContext } from "./TaskContext";
import { ManualResult } from "./ManualResult";
import { ManualReopenRecovery } from "./ManualReopen";
import { ChatFinish } from "./ChatFinish";
import { IssueImport } from "./IssueImport";
import { PhoneTaskPage } from "./TaskPage";
import { AutoTextarea, BriefBlock, Prop, ToolPanel } from "./TaskDialogParts";
import "./taskdialog.css";
import { Harness, statusTone } from "../team/team";
import { confirmAsync, errorText } from "../ui";
import { closeItems } from "./operatorClose";
import {
  Arranged,
  BRIEF_FIELDS,
  Brief,
  BriefField,
  Column,
  NEXT,
  NeedsYou,
  ProjectBoardData,
  ProjectTask,
  LaunchEffect,
  Requirement,
  TaskStatus,
  acceptanceChip,
  acceptanceTone,
  arrange,
  briefChanges,
  chips,
  columnCount,
  deliveryState,
  emptyBrief,
  hasAcceptance,
  launchFeedback,
  missingBrief,
  requirementSource,
  sections,
  statusLine,
  toggleFilter,
} from "./board";

export type BoardResponse = ProjectBoardData & { project: { id: string; name: string } };
type TaskCommand = { task: { id: string; entity_revision: number } };
type LaunchReceipt = { effect_id: string; state: "queued"; entity_revision: number };
type LaunchIntent = { id: string; body: { staff_id: string; resume_from: string | null; expected_entity_revision: number } };
type ResumeSession = { id: string; started_at: string; owner_name: string; task_title: string; can_resume: boolean; resume_reason: string };

export function acceptedResultCost(amount: number | null | undefined): string {
  if (typeof amount !== "number" || !Number.isSafeInteger(amount) || amount < 0) return t("pboard.cost.unknown");
  if (amount % 10_000 === 0) return `$${(amount / 1_000_000).toFixed(2)}`;
  return `$${(amount / 1_000_000).toFixed(6).replace(/0+$/, "")}`;
}

const boardKey = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/board`;
const launchIntentKey = (taskId: string) => `task-launch-intent:${taskId}`;
const launchEffectKey = (taskId: string) => `task-launch-effect:${taskId}`;

function storedLaunchIntent(taskId: string): LaunchIntent | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(launchIntentKey(taskId)) ?? "null");
    return value && typeof value.id === "string" && typeof value.body?.staff_id === "string" && Number.isInteger(value.body.expected_entity_revision) ? value as LaunchIntent : null;
  } catch { return null; }
}

function storedLaunchEffect(taskId: string): string | null {
  try { return sessionStorage.getItem(launchEffectKey(taskId)); }
  catch { return null; }
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
export function ProjectBoard({ projectId, toast, selected, selectedResult, layout = "auto", embedded = false, back, bannered = null }: { projectId: string; toast: (text: string) => void; selected?: string | null; selectedResult?: URLSearchParams; layout?: "auto" | "list"; embedded?: boolean; back?: string | null; bannered?: string | null }) {
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
  const [importing, setImporting] = useState(false);

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
  const resultReference: AcceptedResultReference | null = selectedResult?.has("result") ? {
    resultId: selectedResult.get("result") ?? "", revision: Number(selectedResult.get("revision")),
    attemptId: selectedResult.get("attempt") ?? "", digest: selectedResult.get("digest") ?? "",
  } : null;
  // A link to a finished task widens the board so the task can be shown.
  useEffect(() => {
    if (chosen && data && !open && !showDone) setShowDone(true);
  }, [chosen, data, open, showDone]);

  const boardPath = projectPagePath(projectId, "board");
  const openTask = (task: ProjectTask) => (inAddress ? navigate(`${boardPath}?task=${encodeURIComponent(task.id)}`) : setPicked(task.id));
  const closeTask = () => (inAddress ? navigate(boardPath, { replace: true }) : setPicked(null));

  function pickChip(column: Column) {
    const next = toggleFilter(filter, column);
    setFilter(next);
    if (next === "done") setShowDone(true);
  }

  const openCount = data ? data.tasks.filter((task) => task.status !== "done" && task.status !== "dropped").length : 0;
  const subtitle = data ? [plural("pboard.count.open", openCount), data.needs_you.length ? plural("pboard.count.needs", data.needs_you.length) : ""].filter(Boolean).join(" · ") : undefined;
  const empty = data && data.tasks.length === 0 && data.needs_you.length === 0 && columnCount("done", arranged, data.counts) === 0;
  const phoneChips = data ? chips(arranged, data.counts) : [];

  const card = (task: ProjectTask) => <TaskCard key={task.id} task={task} titles={titles} onOpen={() => openTask(task)} />;
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
        <UnknownStops key={projectId} projectId={projectId} />
        <UncertainLaunches key={projectId} projectId={projectId} />
        {loading && !data && !error && <Skeleton rows={4} />}
        {error && !data && (
          <div className="empty">
            <b>{t("pboard.error")}</b>
            <div>{error}</div>
            <button className="btn primary" onClick={refresh}>{t("common.retry")}</button>
          </div>
        )}
        {resultReference && data && !open && showDone && <div className="result-warning" role="alert">{t("goal.resultStale")}</div>}
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
      {creating && data && <TaskSheet projectId={projectId} data={data} onClose={() => setCreating(false)} onDone={reload} toast={toast} onImport={() => { setCreating(false); setImporting(true); }} />}
      {importing && <IssueImport projectId={projectId} onClose={() => setImporting(false)} onDone={reload} toast={toast} />}
      {open && data && <TaskSheet key={open.id} projectId={projectId} data={data} task={open} resultReference={resultReference} onClose={closeTask} onDone={reload} toast={toast} />}
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

function TaskCard({ task, titles, onOpen }: { task: ProjectTask; titles: Record<string, { title: string; status: TaskStatus }>; onOpen: () => void }) {
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
        {task.issue && <IssueChip issue={task.issue} />}
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
        <AcceptanceChip task={task} />
        {task.status === "done" || task.status === "dropped" ? (
          <span className="faint" title={absTime(task.updated_at)}>{task.status === "dropped" ? t("board.col.dropped") : relTime(task.updated_at)}</span>
        ) : null}
        {task.status === "review" && <span className="sub">{t("result.openReview")}</span>}
      </div>
    </div>
  );
}

/** The issue a card came from, as its number linking to GitHub. The click stops at the link, so it
 *  opens the issue rather than the card underneath it. */
function IssueChip({ issue }: { issue: NonNullable<ProjectTask["issue"]> }) {
  return (
    <a className={`chip tiny pcard-issue ${issue.state === "conflict" ? "attn" : ""}`} href={issue.url} target="_blank" rel="noreferrer"
      data-issue={issue.number} title={t("issues.card.title", { repository: issue.repository, n: issue.number })}
      onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
      #{issue.number}
    </a>
  );
}

/** How far a handed-in card was accepted, in a word. It sits in the card's last row rather than a row
 *  of its own, so a finished card is no taller for it. */
function AcceptanceChip({ task }: { task: ProjectTask }) {
  const chip = acceptanceChip(task);
  if (!chip) return null;
  return (
    <span className={`chip tiny pcard-accept ${chip.tone}`} data-acceptance={chip.state} title={t(`pboard.acceptance.${chip.state}.title`)}>
      {chip.state === "operator_approved" && <Icon name="check" size={11} />}
      {t(`pboard.acceptance.${chip.state}`)}
    </span>
  );
}

/** The checks of a handed-in card: the member's word on each (how it was checked, what came of it) and
 *  the orchestrator's mark, so the operator reads the evidence rather than a count of ticks. */
function AcceptanceSection({ task }: { task: ProjectTask }) {
  const state = task.acceptance_state;
  return (
    <section className="pboard-contract pboard-acceptance" aria-label={t("pboard.acceptance")}>
      <div className="pboard-contract-head">
        <span className="pboard-contract-title">{t("pboard.acceptance")}</span>
        {state && (
          <span className={`chip tiny ${acceptanceTone(state)}`} data-acceptance={state}>
            {state === "operator_approved" && <Icon name="check" size={11} />}
            {t(`pboard.acceptance.${state}.title`)}
          </span>
        )}
      </div>
      {task.checklist.length > 0 && (
        <ol className="pboard-items">
          {task.checklist.map((item, i) => (
            <li key={i} className={`pboard-item ${item.mark ? (item.mark.ok ? "ok" : "bad") : ""}`} data-check={`C${i + 1}`}>
              <code className="pboard-label">C{i + 1}</code>
              <div className="pboard-item-line">
                <span className="pboard-item-text">{item.text}</span>
                {item.mark && <MarkChip ok={item.mark.ok} />}
              </div>
              {item.evidence && (item.evidence.how || item.evidence.result) && (
                <div className="sub pboard-evidence">{[item.evidence.how, item.evidence.result].filter(Boolean).join(" → ")}</div>
              )}
              {!item.evidence && state === "handed_in" && <div className="sub faint">{t("pboard.check.noword")}</div>}
              {item.mark?.note && <div className="sub pboard-mark-note">{item.mark.note}</div>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function MarkChip({ ok }: { ok: boolean }) {
  return (
    <span className={`chip tiny ${ok ? "ok" : "bad"} pboard-mark`} data-mark={ok ? "met" : "unmet"}>
      <Icon name={ok ? "check" : "close"} size={11} />
      {t(ok ? "pboard.check.met" : "pboard.check.unmet")}
    </span>
  );
}

/** The card's requirements: what each asks, of what kind, whose it is, and for each member it was
 *  given to whether it got there. One replaced or withdrawn stays in the list, struck through, so the
 *  operator can see what changed rather than wonder where a requirement went. */
function RequirementsSection({ requirements }: { requirements: Requirement[] }) {
  return (
    <section className="pboard-contract pboard-requirements" aria-label={t("pboard.requirements")}>
      <div className="pboard-contract-head">
        <span className="pboard-contract-title">{t("pboard.requirements")}</span>
      </div>
      <ol className="pboard-items">
        {requirements.map((r) => {
          const source = requirementSource(r);
          return (
            <li key={r.id} className={`pboard-item req ${r.state}`} data-requirement={r.label}>
              <code className="pboard-label">{r.label}</code>
              <div className="pboard-item-line">
                <span className="pboard-item-text">{r.text}</span>
                {r.mark && r.state === "active" && <MarkChip ok={r.mark.ok} />}
              </div>
              <div className="sub pboard-req-meta">
                {t(`pboard.req.kind.${r.kind}`)} · {t(`pboard.req.from.${source.key}`, { ref: source.ref })}
                {r.state !== "active" && <> · <span className="pboard-req-state">{t(`pboard.req.state.${r.state}`)}</span></>}
              </div>
              {r.kind === "input" && r.file_name && (
                <div className="sub pboard-req-file"><Icon name="file" size={12} /> <span className="truncate" title={r.file_name}>{r.file_name}</span></div>
              )}
              {r.mark?.note && r.state === "active" && <div className="sub pboard-mark-note">{r.mark.note}</div>}
              {r.state === "active" && r.deliveries.length > 0 && (
                <div className="pboard-deliveries">
                  {r.deliveries.map((d) => {
                    const state = deliveryState(d, r.kind);
                    return (
                      <span
                        key={`${d.staff_id}-${d.delivered_at}`}
                        className={`chip tiny pboard-delivery ${state === "sent" ? "attn" : state === "words" ? "" : "ok"}`}
                        data-delivery={state}
                        title={t(`pboard.req.via.${d.via}`, { t: absTime(d.delivered_at) })}
                      >
                        {t(`pboard.req.delivery.${state}`, { name: d.staff_name })}
                      </span>
                    );
                  })}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

/** The rare tools of a task, each opened from its menu in the place of the task's text. */
type Tool = "lifecycle" | "comparison" | "handoff" | "workflow" | "context" | "manual" | "diagnostics";

/** The chip tone of a task's column, so review and a block stand out from the work flowing past them. */
const STATUS_TONE: Record<TaskStatus, string> = { todo: "", doing: "accent", review: "attn", done: "ok", blocked: "bad", dropped: "quiet" };

/**
 * Creating a task and changing one. A new task is a form, since everything in it is still to be
 * written. An existing task is a view to track it in: its state across the top; its brief, its
 * requirements, its result and its notes as text in a wide main column; its properties and the one
 * thing to do next in a narrow side column. A field turns into an editor only when its Edit button is
 * pressed, Save and Cancel appear only while something is changed, and the rare tools wait behind the
 * menu. It used to be one narrow column of every control at once, with folds inside folds and the
 * brief cut off in two-line boxes, and the operator could not find the task in it.
 */
export function TaskSheet({ projectId, data, task, resultReference, onClose, onDone, toast, onImport }: { projectId: string; data: BoardResponse; task?: ProjectTask; resultReference?: AcceptedResultReference | null; onClose: () => void; onDone: () => void; toast: (text: string) => void; onImport?: () => void }) {
  const route = useRoute();
  const offline = useOffline();
  const phone = useMedia("(max-width: 1023px)");
  const operation = useRef<{ fingerprint: string; id: string } | null>(null);
  const launchOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const stopOperation = useRef<string | null>(null);
  const createRevision = useRef<number | null>(null);
  const resultAnchor = useRef<HTMLDivElement>(null);
  const intentId = (body: Record<string, unknown>) => {
    const fingerprint = JSON.stringify(body);
    if (operation.current?.fingerprint !== fingerprint) operation.current = { fingerprint, id: crypto.randomUUID() };
    return operation.current.id;
  };
  const [title, setTitle] = useState(task?.title ?? "");
  const [brief, setBrief] = useState<Brief>(task?.brief ?? emptyBrief());
  const [assignee, setAssignee] = useState(task?.assignee_staff_id ?? "");
  const [folderId, setFolderId] = useState(task?.folder_id ?? "");
  // A new task shows its folder choice outright; an existing one shows its folder and a Change button,
  // because the choice there has to ask the host first whether a comparison holds the folder.
  const [folderOpen, setFolderOpen] = useState(false);
  const projects = useQuery<Project[]>("/api/projects", { staleMs: 15000 });
  const folders = projects.data?.find((project) => project.id === projectId)?.folders ?? [];
  // Autonomy normal or full already lets the coordinator assign and run a task; only ``ask`` needs
  // the per-task permission, so only then is the way to give it shown.
  const asksFirst = projects.data?.find((project) => project.id === projectId)?.settings.orchestrator?.autonomy === "ask";
  const comparisons = useQuery<{ groups: { state: string }[] }>(task && folderOpen ? `/api/board/${encodeURIComponent(task.id)}/comparisons?limit=20` : null, { staleMs: 0 });
  const folderBlocked = !!task && (task.status !== "todo" || !comparisons.data || !!comparisons.error ||
    comparisons.data.groups.some((group) => ["planned", "active", "ready", "unknown"].includes(group.state)));
  const [deps, setDeps] = useState<string[]>(task?.depends_on ?? []);
  const [depsOpen, setDepsOpen] = useState(!task);
  const [priority, setPriority] = useState(task?.priority ?? 3);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  // Which parts of an existing task are open for editing: "title" and the brief's fields.
  const [editing, setEditing] = useState<ReadonlySet<string>>(() => new Set());
  const [tool, setTool] = useState<Tool | null>(null);
  // Saved, and the board not yet read again: the drafts already hold what was saved, and the footer
  // would otherwise flash Save back for the moment the old task is still the one in hand.
  const [syncing, setSyncing] = useState(false);
  const [resumeFrom, setResumeFrom] = useState("");
  const [resumeOpen, setResumeOpen] = useState(!task);
  const [resumeSessions, setResumeSessions] = useState<ResumeSession[]>([]);
  const [resumeBefore, setResumeBefore] = useState("");
  const [resumeMore, setResumeMore] = useState(false);
  const [resumeError, setResumeError] = useState("");
  const [stopEffectId, setStopEffectId] = useState<string | null>(null);
  const stopEffect = useQuery<{ state: string }>(stopEffectId ? `/api/control/effects/${encodeURIComponent(stopEffectId)}` : null, { pollMs: 5000, staleMs: 0 });
  const [launchEffectId, setLaunchEffectId] = useState<string | null>(() => task ? storedLaunchEffect(task.id) : null);
  const launchEffect = useQuery<LaunchEffect>(launchEffectId ? `/api/control/effects/${encodeURIComponent(launchEffectId)}` : null, { pollMs: 5000, staleMs: 0 });
  useEffect(() => { setLaunchEffectId(task ? storedLaunchEffect(task.id) : null); }, [task?.id]);
  const writeBlocked = offline || (!!task && !Number.isInteger(task.entity_revision));
  const launchStatus = launchFeedback(launchEffect.data, !!launchEffect.error);
  const launchPending = !!launchEffectId && launchStatus.blocked;

  // Only tasks still open can be waited for; a dependency already listed stays offered so it can be removed.
  const candidates = data.tasks.filter((other) => other.id !== task?.id && ((other.status !== "done" && other.status !== "dropped") || deps.includes(other.id)));
  const team = data.staff;
  const chosenMember = team.find((member) => member.id === assignee);
  useEffect(() => {
    setResumeFrom("");
    setResumeSessions([]);
    setResumeBefore("");
    setResumeMore(false);
    setResumeError("");
    if (!chosenMember || chosenMember.harness === "daedalus" || (!task && chosenMember.isolation === "worktree")) return;
    let current = true;
    const taskQuery = task ? `task_id=${encodeURIComponent(task.id)}&` : "";
    const path = `/api/staff/${encodeURIComponent(chosenMember.id)}/resume-sessions?${taskQuery}limit=50`;
    api.get<ResumeSession[]>(path).then((rows) => {
      if (!current) return;
      setResumeSessions(rows);
      setResumeBefore(rows.at(-1)?.id ?? "");
      setResumeMore(rows.length === 50);
    }).catch((error) => { if (current) setResumeError(errorText(error)); });
    return () => { current = false; };
  }, [task?.id, chosenMember?.id]);
  async function olderSessions() {
    if (!chosenMember || !resumeBefore) return;
    try {
      const taskQuery = task ? `task_id=${encodeURIComponent(task.id)}&` : "";
      const path = `/api/staff/${encodeURIComponent(chosenMember.id)}/resume-sessions?${taskQuery}before=${encodeURIComponent(resumeBefore)}&limit=50`;
      const rows = await api.get<ResumeSession[]>(path);
      setResumeSessions((previous) => [...previous, ...rows]);
      setResumeBefore(rows.at(-1)?.id ?? "");
      setResumeMore(rows.length === 50);
    } catch (error) { setResumeError(errorText(error)); }
  }
  const gone = task?.assignee && !team.some((m) => m.id === task.assignee!.id) ? task.assignee : null;
  const grantBundle = route.query.get("grant");
  const granting = !!task && (grantBundle === "assignment_execution" || grantBundle === "execution");
  const taskPath = task ? projectPagePath(projectId, "board", { task: task.id }) : "";
  const missing = missingBrief(brief);
  const editorChanged = task
    ? title.trim() !== task.title ||
      Object.keys(briefChanges(task.brief, brief)).length > 0 ||
      assignee !== (task.assignee_staff_id ?? "") ||
      folderId !== (task.folder_id ?? "") ||
      deps.join(",") !== task.depends_on.join(",") ||
      priority !== task.priority ||
      note.trim() !== ""
    : title.trim() !== "";
  const changed = editorChanged || !!resumeFrom;
  const canStart = !!task && (task.status === "todo" || task.status === "blocked");
  const offersResume = !!chosenMember && chosenMember.harness !== "daedalus" && (!!task || chosenMember.isolation !== "worktree");

  /** Every draft back to the task as it stands: Cancel, and the board read again after a save. */
  function reset(from: ProjectTask) {
    setTitle(from.title);
    setBrief(from.brief);
    setAssignee(from.assignee_staff_id ?? "");
    setFolderId(from.folder_id ?? "");
    setDeps(from.depends_on);
    setPriority(from.priority);
    setNote("");
    setResumeFrom("");
    setEditing(new Set());
    setDepsOpen(false);
    setFolderOpen(false);
    setResumeOpen(false);
  }
  // A newer revision of the task replaces the drafts only when nothing is being edited or the change
  // is our own save coming back: the event stream reads the board again whenever anything in the
  // project moves, and an edit in progress must survive a worker's status changing elsewhere.
  const seenRevision = useRef(task?.entity_revision);
  const dirty = useRef(false);
  dirty.current = changed;
  useEffect(() => {
    if (!task || task.entity_revision === seenRevision.current) return;
    seenRevision.current = task.entity_revision;
    if (dirty.current && !syncing) return;
    reset(task);
    setSyncing(false);
  }, [task?.entity_revision]);
  useEffect(() => {
    if (!syncing) return;
    const timer = window.setTimeout(() => setSyncing(false), 5000);
    return () => window.clearTimeout(timer);
  }, [syncing]);
  const edit = (part: string) => setEditing((open) => new Set(open).add(part));
  // A task that moved shows where it moved to: a submitted result put the task in review behind the
  // very tool that submitted it, and the result to judge stayed out of sight.
  useEffect(() => { setTool(null); }, [task?.status]);

  async function launch(taskId: string, revision: number) {
    const chosen = { staff_id: assignee, resume_from: resumeFrom || null };
    const previous = storedLaunchIntent(taskId);
    if (previous && (previous.body.staff_id !== chosen.staff_id || previous.body.resume_from !== chosen.resume_from)) {
      throw new Error(t("pboard.launch.reconcileFirst"));
    }
    const body = previous?.body ?? { ...chosen, expected_entity_revision: revision };
    const fingerprint = `${taskId}:${JSON.stringify(body)}`;
    if (launchOperation.current?.fingerprint !== fingerprint) launchOperation.current = { fingerprint, id: crypto.randomUUID() };
    const intent = previous ?? { id: launchOperation.current.id, body };
    try { sessionStorage.setItem(launchIntentKey(taskId), JSON.stringify(intent)); } catch { /* no site data */ }
    let receipt: LaunchReceipt;
    try {
      receipt = await api.post<LaunchReceipt>(`/api/board/${encodeURIComponent(taskId)}/launch`, { ...intent.body, client_operation_id: intent.id });
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        try { sessionStorage.removeItem(launchIntentKey(taskId)); } catch { /* no site data */ }
      }
      throw error;
    }
    try {
      sessionStorage.removeItem(launchIntentKey(taskId));
      sessionStorage.setItem(launchEffectKey(taskId), receipt.effect_id);
    } catch { /* no site data */ }
    launchOperation.current = null;
    setLaunchEffectId(receipt.effect_id);
    toast(t("pboard.launch.queued"));
  }

  async function launchAfterSave(saved: TaskCommand) {
    if (!assignee) return;
    try {
      await launch(saved.task.id, saved.task.entity_revision);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) launchOperation.current = null;
      toast(t("pboard.launch.failed", { detail: errorText(error) }));
    }
  }

  async function save() {
    if (offline) { toast(t("result.block.unconfirmed")); return; }
    setBusy(true);
    try {
      if (!task) {
        if (createRevision.current === null) {
          const revisions = await api.get<{ collection_revision: number }>(`/api/control/revisions?project=${encodeURIComponent(projectId)}`);
          if (!Number.isInteger(revisions.collection_revision)) { toast(t("result.block.unconfirmed")); return; }
          createRevision.current = revisions.collection_revision;
        }
        const body = { title: title.trim(), brief, assignee_staff_id: assignee || null, folder_id: folderId || null,
          depends_on: deps, priority, expected_collection_revision: createRevision.current };
        const made = await api.post<TaskCommand>(`${boardKey(projectId)}`, { ...body, client_operation_id: intentId(body) });
        operation.current = null;
        createRevision.current = null;
        toast(t("pboard.saved"));
        await launchAfterSave(made);
        onDone();
        onClose();
      } else {
        const body: Record<string, unknown> = {};
        if (title.trim() !== task.title) body.title = title.trim();
        const briefDiff = briefChanges(task.brief, brief);
        if (Object.keys(briefDiff).length) body.brief = briefDiff;
        if (assignee !== (task.assignee_staff_id ?? "")) body.assignee_staff_id = assignee;
        if (folderId !== (task.folder_id ?? "") && folderId) body.folder_id = folderId;
        if (deps.join(",") !== task.depends_on.join(",")) body.depends_on = deps;
        if (priority !== task.priority) body.priority = priority;
        if (note.trim()) body.note = note.trim();
        const revision = task.entity_revision;
        if (typeof revision !== "number" || !Number.isInteger(revision)) { toast(t("result.block.unconfirmed")); return; }
        let saved: TaskCommand = { task: { id: task.id, entity_revision: revision } };
        if (editorChanged) {
          body.expected_entity_revision = revision;
          saved = await api.put<TaskCommand>(`/api/board/${encodeURIComponent(task.id)}`, { ...body, client_operation_id: intentId(body) });
          operation.current = null;
          toast(t("pboard.saved"));
        }
        if (assignee && (assignee !== (task.assignee_staff_id ?? "") || !!resumeFrom)) await launchAfterSave(saved);
        // The dialog stays open on the task: an edit is a moment in tracking it, not the end of it.
        setEditing(new Set());
        setNote("");
        setResumeFrom("");
        setResumeOpen(false);
        setSyncing(editorChanged);
        onDone();
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { operation.current = null; createRevision.current = null; }
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function move(status: TaskStatus) {
    if (!task) return;
    if (offline || !Number.isInteger(task.entity_revision)) { toast(t("result.block.unconfirmed")); return; }
    try {
      await api.put(`/api/board/${encodeURIComponent(task.id)}`, { status, expected_entity_revision: task.entity_revision, client_operation_id: crypto.randomUUID() });
      onDone();
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function stopTask() {
    if (!task || writeBlocked || busy) return;
    if (!(await confirmAsync(t("result.stopTaskTitle", { title: task.title }), { body: t("result.stopTaskBody"), action: t("result.stopTaskAction"), danger: true }))) return;
    const id = stopOperation.current ?? crypto.randomUUID();
    stopOperation.current = id;
    setBusy(true);
    try {
      const response = await api.post<{ effect_id: string }>(`/api/board/${encodeURIComponent(task.id)}/stop`, { expected_entity_revision: task.entity_revision, client_operation_id: id, reason: "operator_requested" });
      stopOperation.current = null;
      setStopEffectId(response.effect_id);
      toast(t("result.stopTaskQueued"));
      onDone();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) stopOperation.current = null;
      toast(errorText(error));
    } finally { setBusy(false); }
  }
  async function startTask() {
    if (!task || typeof task.entity_revision !== "number") return;
    setBusy(true);
    try { await launch(task.id, task.entity_revision); onDone(); }
    catch (error) { if (error instanceof ApiError && error.status === 409) launchOperation.current = null; toast(errorText(error)); }
    finally { setBusy(false); }
  }
  const toggleDep = (id: string) => setDeps(deps.includes(id) ? deps.filter((d) => d !== id) : [...deps, id]);

  if (granting && task) return (
    <Sheet title={t(grantBundle === "execution" ? "pboard.grant.execution.title" : "pboard.grant.title")} onClose={() => navigate(taskPath)} className="pboard-sheet">
      <button type="button" className="linkbtn" onClick={() => navigate(taskPath)}>{t("pboard.grant.back")}</button>
      <p className="sub">{t(grantBundle === "execution" ? "pboard.grant.execution.intro" : "pboard.grant.intro", { title: task.title })}</p>
      <CoordinatorAuthority projectId={projectId} initialTaskId={task.id} initialBundle={grantBundle === "execution" ? "execution" : "assignment_execution"} toast={toast} onChanged={onDone} />
    </Sheet>
  );

  // Pieces both a new task's form and an existing task's side column use.
  const assigneeControl = team.length === 0 && !gone ? (
    <div className="sub">
      {canStart ? t("pboard.assignee.nobody") : <>{t("pboard.assignee.none")} <button className="linkbtn" onClick={() => navigate(projectPagePath(projectId, "team"))}>{t("pboard.team.hire")}</button></>}
    </div>
  ) : (
    <select id="ptask-assignee" className="field ptask-select" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
      <option value="">{t("pboard.assignee.nobody")}</option>
      {team.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
      {gone && <option value={gone.id}>{t("pboard.assignee.gone", { name: gone.name })}</option>}
    </select>
  );
  const missingNote = assignee && missing.length > 0 && (
    <div className="sub attn">{t("pboard.brief.missing", { parts: missing.map((field) => t(`pboard.brief.${field}`)).join(", ") })}</div>
  );
  const resumeControl = offersResume && resumeOpen && (
    <div className="ptask-resume">
      <label className="field" htmlFor="ptask-resume">{t("pboard.resume.label")}</label>
      <select id="ptask-resume" className="field ptask-select" value={resumeFrom} onChange={(event) => setResumeFrom(event.target.value)}>
        <option value="">{t("pboard.resume.fresh")}</option>
        {resumeSessions.map((session) => <option key={session.id} value={session.id} disabled={!session.can_resume}>
          {session.started_at.slice(0, 16).replace("T", " ")} · {session.owner_name} · {session.task_title || t("goal.card.untitled")} {session.can_resume ? "" : `· ${t(`pboard.resume.reason.${session.resume_reason}`)}`}
        </option>)}
      </select>
      <div className="sub">{t("pboard.resume.hint")}</div>
      {resumeMore && <button className="linkbtn" type="button" onClick={olderSessions}>{t("pboard.resume.older")}</button>}
      {resumeError && <div className="sub attn">{resumeError}</div>}
    </div>
  );
  const depsPicker = candidates.length === 0 ? (
    <div className="sub">{t("pboard.depends.none")}</div>
  ) : (
    <div className="pboard-deps" role="group" aria-label={t("pboard.depends")}>
      {candidates.map((other) => (
        <button key={other.id} type="button" className="chip select" aria-pressed={deps.includes(other.id)} onClick={() => toggleDep(other.id)} title={other.title}>
          <span className="ptask-chip-text">{other.title}</span>
        </button>
      ))}
    </div>
  );
  const priorityControl = (
    <div className="segmented inline ptask-priority" role="radiogroup" aria-label={t("board.priority")}>
      {[1, 2, 3, 4, 5].map((p) => (
        <button key={p} type="button" role="radio" aria-checked={priority === p} className={priority === p ? "on" : ""} onClick={() => setPriority(p)}>P{p}</button>
      ))}
    </div>
  );
  const folderName = (id: string) => folders.find((folder) => folder.id === id)?.label || folders.find((folder) => folder.id === id)?.path || t("pboard.folder.pinned");
  const folderControl = <>
    <p className="sub">{t("pboard.folder.hint")}</p>
    {projects.error && <div className="result-warning">{t("pboard.folder.unavailable")}</div>}
    {task && comparisons.error && <div className="result-warning">{t("pboard.folder.unavailable")}</div>}
    {folderBlocked && <div className="result-warning">{t("pboard.folder.blocked")}</div>}
    <select id="ptask-folder" className="field ptask-select" aria-label={t("pboard.folder.choose")} value={folderId} disabled={busy || offline || !!projects.error || !projects.data || folderBlocked} onChange={(event) => setFolderId(event.target.value)}>
      {!task?.folder_id && <option value="">{t("pboard.folder.none")}</option>}
      {task?.folder_id && !folders.some((folder) => folder.id === task.folder_id) && <option value={task.folder_id}>{t("pboard.folder.pinned")}</option>}
      {folders.map((folder) => <option key={folder.id} value={folder.id}>{folder.label || folder.path}{folder.readonly ? ` · ${t("pboard.folder.readonly")}` : ""}</option>)}
    </select>
  </>;
  const launchNotice = task && launchEffectId && <div className="result-warning" role="status">
    {t(launchStatus.key)}
    {launchStatus.wait && <div>{t(waitKey(launchStatus.wait))}</div>}
    {launchStatus.detail && <div className="sub">{launchStatus.detail}</div>}
    {launchStatus.blocked && launchStatus.key === "pboard.launch.unconfirmed" && <div className="sub">{t("pboard.launch.inspectFirst")}</div>}
    {launchStatus.key === "pboard.launch.deliveryFailed" && <div className="sub">{t("pboard.launch.repairFirst")}</div>}
    <button type="button" className="linkbtn" onClick={() => launchEffect.refresh()}>{t("pboard.launch.refresh")}</button>
  </div>;

  if (!task) {
    // A new task: a form, since everything in it is still to be written.
    const form = <>
      {/* Bringing in work that is already written down as issues sits beside making a card by hand,
          rather than as another button in the board's header. */}
      {onImport && <button type="button" className="linkbtn pboard-import" onClick={onImport}>{t("issues.open")}</button>}
      <label className="field" htmlFor="ptask-title">{t("board.title")}</label>
      <input id="ptask-title" className="field" autoFocus value={title} maxLength={200} onChange={(e) => setTitle(e.target.value)} />
      <fieldset className="pboard-brief">
        <legend>{t("pboard.brief")}</legend>
        {BRIEF_FIELDS.map((field) => (
          <div key={field}>
            <label className="field" htmlFor={`ptask-${field}`}>{t(`pboard.brief.${field}`)}</label>
            <AutoTextarea id={`ptask-${field}`} rows={2} maxLength={4000} value={brief[field]} placeholder={t(`pboard.brief.${field}.placeholder`)} onChange={(e) => setBrief({ ...brief, [field]: e.target.value })} />
          </div>
        ))}
      </fieldset>
      <label className="field" htmlFor="ptask-folder">{t("pboard.folder")}</label>
      {folderControl}
      <label className="field" htmlFor="ptask-assignee">{t("pboard.assignee")}</label>
      {assigneeControl}
      {missingNote}
      {resumeControl}
      <label className="field">{t("pboard.depends")}</label>
      {depsPicker}
      <label className="field">{t("board.priority")}</label>
      {priorityControl}
      <div className="sheet-foot">
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn primary" disabled={busy || writeBlocked || !title.trim() || !changed} onClick={save}>{t("common.create")}</button>
      </div>
      {writeBlocked && <div className="result-warning" role="status">{t("result.block.unconfirmed")}</div>}
    </>;
    return (
      <Sheet title={t("pboard.new")} onClose={onClose} className="pboard-sheet new"
        // A phone's new task is decided at the top, where the thumb starts: Create beside the title,
        // the close button as Cancel. The footer's pair stays for the desktop.
        head={phone ? <button className="ph-btn accent sm pboard-create" disabled={busy || writeBlocked || !title.trim() || !changed} onClick={save}>{t("common.create")}</button> : undefined}>
        {form}
      </Sheet>
    );
  }

  // An existing task: the view.
  const titles = Object.fromEntries(data.tasks.map((other) => [other.id, other]));
  const waitingOn = task.depends_on.filter((id) => titles[id] && titles[id].status !== "done" && titles[id].status !== "dropped");
  const asked = data.needs_you.filter((need) => need.task_id === task.id);
  const handoffApplies = ["todo", "blocked", "doing"].includes(task.status) && !!task.project_id;
  const manualApplies = !task.branch && (task.status === "todo" || task.status === "blocked" || task.status === "review");
  const diagnosticsApply = !!task.current_attempt_id && task.status !== "done" && task.status !== "dropped";
  const showResultTab = task.status === "review" || task.acceptance_state === "operator_approved" || !!resultReference || hasAcceptance(task) || !!task.finished_by;
  const toolLabels: Record<Tool, string> = {
    lifecycle: t("lifecycle.task.title"), comparison: t("pair.title"), handoff: t("handoff.title"), workflow: t("taskWorkflow.title"),
    context: t("taskContext.title"), manual: t("manual.title"), diagnostics: t("pboard.attempt.title"),
  };
  const openTool = (next: Tool) => { setTool(next); setResumeOpen(false); };
  // The rare actions, each in the menu and nowhere else until it is chosen; a CLI conversation to
  // continue is chosen beside Start, where it takes effect.
  const toolItems: { label: string; icon: IconName; onSelect: () => void }[] = [
    ...(offersResume && canStart ? [{ label: t("pboard.resume.label"), icon: "terminal" as const, onSelect: () => { setTool(null); setResumeOpen(true); } }] : []),
    ...(handoffApplies ? [{ label: toolLabels.handoff, icon: "forward" as const, onSelect: () => openTool("handoff") }] : []),
    { label: toolLabels.comparison, icon: "split", onSelect: () => openTool("comparison") },
    { label: toolLabels.workflow, icon: "loop", onSelect: () => openTool("workflow") },
    { label: toolLabels.context, icon: "book", onSelect: () => openTool("context") },
    ...(manualApplies ? [{ label: toolLabels.manual, icon: "check" as const, onSelect: () => openTool("manual") }] : []),
    ...(diagnosticsApply ? [{ label: toolLabels.diagnostics, icon: "wrench" as const, onSelect: () => openTool("diagnostics") }] : []),
    { label: toolLabels.lifecycle, icon: "stop", onSelect: () => openTool("lifecycle") },
  ];
  const toolPanel = tool && (
    <ToolPanel title={toolLabels[tool]} onClose={() => setTool(null)}>
      {tool === "lifecycle" && <LifecycleCancel bare kind="task" id={task.id} projectId={projectId} onDone={onDone} toast={toast} />}
      {tool === "comparison" && <TaskComparison bare task={task} staff={data.staff} onChanged={onDone} toast={toast} />}
      {tool === "handoff" && <RuntimeHandoff bare task={task} onChanged={onDone} toast={toast} />}
      {tool === "workflow" && <TaskWorkflow bare projectId={projectId} task={task} tasks={data.tasks} />}
      {tool === "context" && <TaskContext bare task={task} staff={data.staff} />}
      {/* A submitted result is the thing to look at next, so the form gives way to it. */}
      {tool === "manual" && <ManualResult bare task={task} toast={toast} onChanged={() => { setTool(null); onDone(); }} />}
      {tool === "diagnostics" && <TaskDiagnostics bare task={task} toast={toast} />}
    </ToolPanel>
  );

  const titleBlock = editing.has("title") ? (
    <input id="ptask-title" className="field ptask-title-input" autoFocus value={title} maxLength={200} aria-label={t("board.title")}
      onChange={(e) => setTitle(e.target.value)}
      onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); setEditing((open) => { const next = new Set(open); next.delete("title"); return next; }); } }} />
  ) : (
    <div className="ptask-title-row">
      <h3 className="ptask-title">{title.trim() || task.title}</h3>
      <button type="button" className="iconbtn small ptask-edit" onClick={() => edit("title")} aria-label={t("ptask.edit.title")} title={t("ptask.edit.title")}><Icon name="pen" size={14} /></button>
    </div>
  );
  const meta = (
    <div className="ptask-meta">
      <span className={`chip ${STATUS_TONE[task.status]}`}>{t(`board.col.${task.status}`)}</span>
      {task.issue && <IssueChip issue={task.issue} />}
      <span className="sub" title={absTime(task.updated_at)}>{t("board.updated", { t: relTime(task.updated_at) })}</span>
    </div>
  );

  // Where the task stands, in a line: who has it, what it waits for, and anything waiting on the
  // operator, which is the one thing drawn loud.
  const who = task.assignee?.on_task
    ? <span className={`status ${statusTone(task.assignee.status)}`}><span className="dot" aria-hidden />{t("ptask.state.working", { name: task.assignee.name, status: t(`team.status.${task.assignee.status}`) })}{task.assignee.status_at && <> · {relTime(task.assignee.status_at)}</>}</span>
    : task.assignee && canStart ? <span>{t("ptask.state.next", { name: task.assignee.name })}</span>
    : task.assignee ? <span>{t("ptask.state.assigned", { name: task.assignee.name })}</span>
    : task.status !== "done" && task.status !== "dropped" ? <span className="faint">{t("ptask.state.nobody")}</span> : null;
  const state = (
    <div className="ptask-state">
      <div className="ptask-state-line">
        {task.assignee && <StaffAvatar name={task.assignee.name} color={task.assignee.color} size="small" />}
        {who}
        {waitingOn.length > 0 && <span className="ptask-waits">{t("ptask.state.waits", { titles: waitingOn.map((id) => t("ptask.state.quoted", { title: titles[id].title })).join(", ") })}</span>}
      </div>
      {asked.map((need) => (
        <div key={need.id} className="ptask-needs" role="status">
          <Icon name="question" size={16} />
          <span className="grow">{need.staff ? t("pboard.need.from", { kind: t(`pboard.need.kind.${need.kind}`), name: need.staff.name }) : t("pboard.need.orchestrator", { kind: t(`pboard.need.kind.${need.kind}`) })}: {need.text}</span>
          {need.session_id && <button type="button" className="btn small warn" onClick={() => navigate(answerPath(projectId, need))}>{t("pboard.need.answer")}</button>}
        </div>
      ))}
      {task.status === "review" && (
        <div className="ptask-needs" role="status">
          <Icon name="eye" size={16} />
          <span className="grow">{t("ptask.state.review")}</span>
          {!phone && <button type="button" className="btn small warn" onClick={() => resultAnchor.current?.scrollIntoView({ behavior: "smooth", block: "start" })}>{t("result.openReview")}</button>}
        </div>
      )}
    </div>
  );

  // The brief as text, the parts that say what the work is first. A part nobody wrote is not drawn as
  // an empty heading; it is offered once, in the line under the others, to be added.
  const order: BriefField[] = ["objective", "deliverable", "done_when", "boundaries"];
  const shown = order.filter((field) => brief[field].trim() || editing.has(field));
  const absent = order.filter((field) => !shown.includes(field));
  const briefPart = <>
    {shown.map((field) => (
      <BriefBlock key={field} field={field} label={t(`pboard.brief.${field}`)} value={brief[field]} editing={editing.has(field)}
        placeholder={t(`pboard.brief.${field}.placeholder`)} onEdit={() => edit(field)} onChange={(value) => setBrief({ ...brief, [field]: value })} />
    ))}
    {absent.length > 0 && (
      <div className="ptask-add">
        <span className="sub">{t("ptask.brief.add")}</span>
        {absent.map((field) => <button key={field} type="button" className="btn small ghost" onClick={() => edit(field)}><Icon name="plus" size={14} />{t(`pboard.brief.${field}`)}</button>)}
      </div>
    )}
  </>;
  const requirementsPart = (task.requirements?.length ?? 0) > 0 && <RequirementsSection requirements={task.requirements!} />;
  const acceptancePart = hasAcceptance(task) && <AcceptanceSection task={task} />;
  const resultPart = <>
    <ChatFinish task={task} onOpenSession={(id) => navigate(projectSessionPath(projectId, id))} onChanged={onDone} toast={toast} />
    {resultReference && <AcceptedResultDetail task={task} reference={resultReference} />}
    {!resultReference && (task.status === "review" || task.acceptance_state === "operator_approved") && <ResultFlow task={task} onAccepted={onDone} toast={toast} />}
    {task.status === "done" && task.acceptance_state === "operator_approved" && (
      <p className="sub pboard-attempt-cost" title={t("pboard.cost.scope")}>
        {t("pboard.cost.label")} · <strong>{acceptedResultCost(task.accepted_result_cost_microusd)}</strong>
        {task.accepted_result_cost_microusd == null && (task.accepted_result_known_cost_microusd ?? 0) > 0 && (
          <span> · {t("pboard.cost.known", { usd: acceptedResultCost(task.accepted_result_known_cost_microusd) })}</span>
        )}
        {task.accepted_result_cost_unknown_reasons?.map((reason) => (
          <span key={reason}> · {t(`pboard.cost.reason.${reason}`)}</span>
        ))}
        <span> · <button className="linkbtn" onClick={() => navigate(projectPagePath(projectId, "journal"))}>{t("pboard.cost.coordination")}</button></span>
      </p>
    )}
  </>;
  const diffPart = task.status === "review" && task.branch && <ReviewPanel taskId={task.id} onChanged={onDone} toast={toast} />;
  const notesPart = (
    <section className="ptask-block ptask-notes">
      <h4 className="ptask-label">{t("board.notes")}</h4>
      {task.notes && <pre className="inbox-text ptask-notes-text">{task.notes}</pre>}
      <AutoTextarea id="ptask-note" rows={1} value={note} aria-label={t("pboard.note")} placeholder={t("pboard.note.placeholder")} onChange={(e) => setNote(e.target.value)} />
    </section>
  );

  const depsValue = (
    <>
      {deps.length > 0 ? (
        <ul className="ptask-deps">
          {deps.map((id) => {
            const other = titles[id];
            return <li key={id} className={other && (other.status === "done" || other.status === "dropped") ? "met" : ""}>
              <span className={`ptask-dep-dot ${other?.status ?? ""}`} aria-hidden />
              <span className="ptask-dep-title">{other?.title ?? id}</span>
              {other && <span className="sub">{t(`board.col.${other.status}`)}</span>}
            </li>;
          })}
        </ul>
      ) : !depsOpen && <span className="faint">{t("ptask.deps.none")}</span>}
      {depsOpen && depsPicker}
    </>
  );
  const props = (
    <div className="ptask-props">
      <Prop label={t("ptask.prop.status")}>
        <span className={`chip ${STATUS_TONE[task.status]}`}>{t(`board.col.${task.status}`)}</span>
        {NEXT[task.status].length > 0 && (
          <div className="pboard-moves" role="group" aria-label={t("board.moveto")}>
            <span className="sub">{t("board.moveto")}</span>
            {NEXT[task.status].map((status) => (
              <button key={status} type="button" className="btn small" disabled={writeBlocked} onClick={() => void move(status)}>{t(`board.col.${status}`)}</button>
            ))}
          </div>
        )}
      </Prop>
      <Prop label={t("pboard.assignee")} htmlFor={team.length === 0 && !gone ? undefined : "ptask-assignee"}>{assigneeControl}</Prop>
      <Prop label={t("board.priority")}>{priorityControl}</Prop>
      <Prop label={t("pboard.depends")} action={!depsOpen && candidates.length > 0 && <button type="button" className="linkbtn" onClick={() => setDepsOpen(true)}>{t("common.edit")}</button>}>{depsValue}</Prop>
      <Prop label={t("pboard.folder")} htmlFor={folderOpen ? "ptask-folder" : undefined}
        action={!folderOpen && <button type="button" className="linkbtn" onClick={() => setFolderOpen(true)}>{t("common.edit")}</button>}>
        {folderOpen ? folderControl : <span className={`ptask-folder ${folderId ? "" : "faint"}`}>{folderId ? folderName(folderId) : t("ptask.folder.unset")}</span>}
      </Prop>
      {task.branch && <Prop label={t("ptask.prop.branch")}><code className="pcard-branch">{task.branch}</code></Prop>}
    </div>
  );
  // The one thing to do next, by where the task stands.
  const action = (
    <div className="ptask-action">
      {canStart && !gone && asksFirst && <button className="linkbtn" onClick={() => navigate(projectPagePath(projectId, "board", { task: task.id, grant: task.assignee ? "execution" : "assignment_execution" }))}>{t(task.assignee ? "pboard.grant.execution.open" : "pboard.grant.open")}</button>}
      {canStart && !gone && team.length === 0 && !task.assignee && <div className="sub">{t("pboard.assignee.none")} <button className="linkbtn" onClick={() => navigate(projectPagePath(projectId, "team"))}>{t("pboard.team.hire")}</button></div>}
      {canStart && assignee && <>
        <button type="button" className="btn primary ptask-primary" disabled={busy || writeBlocked || editorChanged || launchPending} onClick={() => void startTask()}>{t(storedLaunchIntent(task.id) ? "pboard.launch.retry" : "pboard.launch.action")}</button>
        {editorChanged && <span className="sub">{t("pboard.launch.saveFirst")}</span>}
        {missingNote}
        {resumeControl}
        {offersResume && !resumeOpen && resumeSessions.length > 0 && <button type="button" className="linkbtn" onClick={() => setResumeOpen(true)}>{t("pboard.resume.label")}</button>}
      </>}
      {canStart && !assignee && team.length > 0 && <div className="sub">{t("ptask.action.assign")}</div>}
      {assignee && (canStart || launchPending) && <HostCapacity projectId={projectId} pending={launchPending} />}
      {launchNotice}
      {task.status === "doing" && <>
        <button type="button" className="btn warn ptask-primary" disabled={writeBlocked || busy || !!stopEffectId} onClick={() => void stopTask()}>{t("result.stopTaskAction")}</button>
        <span className="sub">{t("result.stopTaskScope")}</span>
        {task.assignee?.session_id && <button type="button" className="btn ghost small" onClick={() => navigate(projectSessionPath(projectId, task.assignee!.session_id!))}>{t("pboard.open.staff", { name: task.assignee.name })}</button>}
      </>}
      {stopEffectId && <div className="result-warning" role="status">{t(stopEffect.error ? "result.stopTaskUnconfirmed" : stopEffect.data?.state === "completed" ? "result.stopTaskObserved" : stopEffect.data?.state === "unknown" ? "result.stopTaskUnconfirmed" : "result.stopTaskPending")} <button type="button" className="linkbtn" onClick={() => stopEffect.refresh()}>{t("common.retry")}</button></div>}
      {writeBlocked && <div className="result-warning" role="status">{t("result.block.unconfirmed")}</div>}
    </div>
  );
  const foot = changed && !syncing && (
    <div className="sheet-foot ptask-foot">
      <span className="sub grow">{t("ptask.unsaved")}</span>
      <button type="button" className="btn ghost" disabled={busy} onClick={() => reset(task)}>{t("common.cancel")}</button>
      <button type="button" className="btn primary" disabled={busy || writeBlocked || !title.trim()} onClick={save}>{t("common.save")}</button>
    </div>
  );
  const menuItems: MenuItem[] = [
    ...(task.assignee?.session_id ? [{ label: t("pboard.open.staff", { name: task.assignee.name }), icon: "bots" as const, onSelect: () => navigate(projectSessionPath(projectId, task.assignee!.session_id!)) }] : []),
    { label: t("board.copyid"), icon: "copy", onSelect: async () => toast((await copyText(task.id)) ? t("board.copied") : task.id) },
    "-",
    ...toolItems,
    "-",
    ...closeItems(task, writeBlocked || busy, { onDone, toast, onRemoved: onClose }),
  ];

  if (phone) return (
    <PhoneTaskPage projectId={projectId} task={task} onClose={onClose} toast={toast} busy={busy} writeBlocked={writeBlocked} onDone={onDone} onMove={(status) => void move(status)}
      tools={toolItems}
      taskTab={<div className="ptask ptask-phone">
        {titleBlock}
        {state}
        <ManualReopenRecovery task={task} toast={toast} onChanged={onDone} />
        {toolPanel || <>
          {briefPart}
          {requirementsPart}
          {props}
          {action}
          {notesPart}
          <TaskExplorations task={task} />
        </>}
        {foot}
      </div>}
      resultTab={showResultTab ? <div className="ptask ptask-phone">{resultPart}{acceptancePart}</div> : null}
      diffTab={diffPart || null} />
  );
  return (
    <Sheet ariaLabel={task.title} onClose={onClose} className="pboard-sheet ptask"
      head={<>
        <div className="ptask-head">{meta}{titleBlock}</div>
        <OverflowMenu small label={t("board.actions")} items={menuItems} />
      </>}>
      {state}
      <div className="ptask-grid">
        <div className="ptask-main">
          <ManualReopenRecovery task={task} toast={toast} onChanged={onDone} />
          {toolPanel || <>
            {briefPart}
            {requirementsPart}
            {acceptancePart}
            <div ref={resultAnchor} className="ptask-result">{resultPart}{diffPart}</div>
            <TaskExplorations task={task} />
            {notesPart}
          </>}
        </div>
        <aside className="ptask-side" aria-label={t("ptask.side")}>
          {props}
          {action}
        </aside>
      </div>
      {foot}
    </Sheet>
  );
}
