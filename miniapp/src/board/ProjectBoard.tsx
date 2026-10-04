// A project's board: what waits on the operator, the work in progress, what is in review, the queue and
// what is finished. On a wide window the columns stand side by side; narrower, they become chips over one
// list, because five columns on a phone are five slivers nobody can read. The component takes the
// project by id and nothing else, so the project's focus panel and its phone tab can mount it as it is.

import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type Project } from "../api";
import { Skeleton, copyText } from "../ui/components";
import { OverflowMenu, Sheet } from "../ui/dialogs";
import { useEvent, useStreamUp } from "../events";
import { absTime, relTime } from "../format";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { navigate, pathFor, projectHome, projectPagePath, projectSessionPath } from "../router";
import { PageHeader, useMedia } from "../shell";
import { invalidate, useOffline, useQuery } from "../store";
import { HarnessBadge, StaffAvatar } from "../team/parts";
import { LifecycleCancel } from "../project/LifecycleCancel";
import { waitKey } from "../project/focus";
import { ReviewPanel } from "./ReviewPanel";
import { AcceptedResultDetail, ResultFlow, type AcceptedResultReference } from "./ResultFlow";
import { TaskWorkflow } from "./TaskWorkflow";
import { TaskComparison } from "./TaskComparison";
import { RuntimeHandoff } from "./RuntimeHandoff";
import { HostCapacity } from "./HostCapacity";
import { UnknownStops } from "./UnknownStops";
import { TaskContext } from "./TaskContext";
import { ManualResult } from "./ManualResult";
import { ManualReopenRecovery } from "./ManualReopen";
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

type BoardResponse = ProjectBoardData & { project: { id: string; name: string } };
type TaskCommand = { task: { id: string; entity_revision: number } };
type LaunchReceipt = { effect_id: string; state: "queued"; entity_revision: number };
type LaunchIntent = { id: string; body: { staff_id: string; resume_from: string | null; expected_entity_revision: number } };
type ResumeSession = { id: string; started_at: string; owner_name: string; task_title: string; can_resume: boolean; resume_reason: string };

export function acceptedAttemptCost(amount: number | null | undefined): string {
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
      {creating && data && <TaskSheet projectId={projectId} data={data} onClose={() => setCreating(false)} onDone={reload} toast={toast} />}
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

/** Creating a task and changing one: the same sheet, because a task is its title, its brief, who does it and what it waits for. */
function TaskSheet({ projectId, data, task, resultReference, onClose, onDone, toast }: { projectId: string; data: BoardResponse; task?: ProjectTask; resultReference?: AcceptedResultReference | null; onClose: () => void; onDone: () => void; toast: (text: string) => void }) {
  const offline = useOffline();
  const operation = useRef<{ fingerprint: string; id: string } | null>(null);
  const launchOperation = useRef<{ fingerprint: string; id: string } | null>(null);
  const archiveOperation = useRef<string | null>(null);
  const stopOperation = useRef<string | null>(null);
  const createRevision = useRef<number | null>(null);
  const intentId = (body: Record<string, unknown>) => {
    const fingerprint = JSON.stringify(body);
    if (operation.current?.fingerprint !== fingerprint) operation.current = { fingerprint, id: crypto.randomUUID() };
    return operation.current.id;
  };
  const [title, setTitle] = useState(task?.title ?? "");
  const [brief, setBrief] = useState<Brief>(task?.brief ?? emptyBrief());
  const [assignee, setAssignee] = useState(task?.assignee_staff_id ?? "");
  const [folderId, setFolderId] = useState(task?.folder_id ?? "");
  const [folderOpen, setFolderOpen] = useState(false);
  const projects = useQuery<Project[]>("/api/projects", { staleMs: 15000 });
  const folders = projects.data?.find((project) => project.id === projectId)?.folders ?? [];
  const comparisons = useQuery<{ groups: { state: string }[] }>(task && folderOpen ? `/api/board/${encodeURIComponent(task.id)}/comparisons?limit=20` : null, { staleMs: 0 });
  const folderBlocked = !!task && (task.status !== "todo" || !comparisons.data || !!comparisons.error ||
    comparisons.data.groups.some((group) => ["planned", "active", "ready", "unknown"].includes(group.state)));
  const [deps, setDeps] = useState<string[]>(task?.depends_on ?? []);
  const [priority, setPriority] = useState(task?.priority ?? 3);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [resumeFrom, setResumeFrom] = useState("");
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
      }
      onDone();
      onClose();
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
  async function archiveTask() {
    if (!task || writeBlocked || busy) return;
    if (!(await confirmAsync(t("pboard.archive.title", { title: task.title }), { body: t("pboard.archive.body"), action: t("pboard.archive.action"), danger: true }))) return;
    const id = archiveOperation.current ?? crypto.randomUUID();
    archiveOperation.current = id;
    setBusy(true);
    try {
      await api.delete(`/api/board/${encodeURIComponent(task.id)}?client_operation_id=${encodeURIComponent(id)}&expected_entity_revision=${task.entity_revision}`);
      archiveOperation.current = null;
      toast(t("pboard.archive.saved"));
      onDone();
      onClose();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) archiveOperation.current = null;
      toast(errorText(error));
    } finally { setBusy(false); }
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
              { label: t("pboard.archive.action"), icon: "trash", danger: true, disabled: writeBlocked || busy || !["todo", "blocked"].includes(task.status), hint: t("pboard.archive.unavailable"), onSelect: () => void archiveTask() },
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
        <ReviewPanel taskId={task.id} onChanged={onDone} toast={toast} />
      )}
      {task && resultReference && <AcceptedResultDetail task={task} reference={resultReference} />}
      {task && !resultReference && (task.status === "review" || task.acceptance_state === "operator_approved") && <ResultFlow task={task} onAccepted={onDone} toast={toast} />}
      {task?.status === "done" && task.acceptance_state === "operator_approved" && (
        <p className="sub pboard-attempt-cost" title={t("pboard.cost.scope")}>
          {t("pboard.cost.label")} · <strong>{acceptedAttemptCost(task.accepted_attempt_cost_microusd)}</strong>
        </p>
      )}
      {task && (NEXT[task.status].length > 0 || task.status === "review") && (
        <div className="btnrow pboard-moves" role="group" aria-label={t("board.moveto")}>
          {NEXT[task.status].length > 0 && <span className="sub">{t("board.moveto")}</span>}
          {NEXT[task.status].map((status) => (
            <button key={status} className="btn small" disabled={writeBlocked} onClick={() => move(status)}>{t(`board.col.${status}`)}</button>
          ))}
        </div>
      )}
      {task?.status === "doing" && <div className="btnrow"><button type="button" className="btn small warn" disabled={writeBlocked || busy || !!stopEffectId} onClick={() => void stopTask()}>{t("result.stopTaskAction")}</button><span className="sub">{t("result.stopTaskScope")}</span></div>}
      {stopEffectId && <div className="result-warning" role="status">{t(stopEffect.error ? "result.stopTaskUnconfirmed" : stopEffect.data?.state === "completed" ? "result.stopTaskObserved" : stopEffect.data?.state === "unknown" ? "result.stopTaskUnconfirmed" : "result.stopTaskPending")} <button type="button" className="linkbtn" onClick={() => stopEffect.refresh()}>{t("common.retry")}</button></div>}
      {task && <LifecycleCancel kind="task" id={task.id} projectId={projectId} onDone={onDone} toast={toast} />}
      {task && <TaskWorkflow projectId={projectId} task={task} tasks={data.tasks} />}
      {task && <TaskContext task={task} staff={data.staff} />}
      {task && !task.branch && (task.status === "todo" || task.status === "blocked" || task.status === "review") && <ManualResult task={task} toast={toast} onChanged={onDone} />}
      {task && task.status !== "done" && <ManualReopenRecovery task={task} toast={toast} onChanged={onDone} />}
      {task && <TaskComparison task={task} staff={data.staff} onChanged={onDone} toast={toast} />}
      {task && <RuntimeHandoff task={task} onChanged={onDone} toast={toast} />}

      {task && hasAcceptance(task) && <AcceptanceSection task={task} />}

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

      <details open={folderOpen} onToggle={(event) => setFolderOpen(event.currentTarget.open)}>
        <summary>{t("pboard.folder")}{folderId ? ` · ${folders.find((folder) => folder.id === folderId)?.label || folders.find((folder) => folder.id === folderId)?.path || t("pboard.folder.pinned")}` : ""}</summary>
        <p className="sub">{t("pboard.folder.hint")}</p>
        {projects.error && <div className="result-warning">{t("pboard.folder.unavailable")}</div>}
        {folderOpen && task && comparisons.error && <div className="result-warning">{t("pboard.folder.unavailable")}</div>}
        {folderOpen && folderBlocked && <div className="result-warning">{t("pboard.folder.blocked")}</div>}
        <label className="field" htmlFor="ptask-folder">{t("pboard.folder.choose")}</label>
        <select id="ptask-folder" className="field" value={folderId} disabled={busy || offline || !!projects.error || !projects.data || folderBlocked} onChange={(event) => setFolderId(event.target.value)}>
          {!task?.folder_id && <option value="">{t("pboard.folder.none")}</option>}
          {task?.folder_id && !folders.some((folder) => folder.id === task.folder_id) && <option value={task.folder_id}>{t("pboard.folder.pinned")}</option>}
          {folders.map((folder) => <option key={folder.id} value={folder.id}>{folder.label || folder.path}{folder.readonly ? ` · ${t("pboard.folder.readonly")}` : ""}</option>)}
        </select>
      </details>

      {task && (task.requirements?.length ?? 0) > 0 && <RequirementsSection requirements={task.requirements!} />}

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
      {chosenMember && chosenMember.harness !== "daedalus" && (task || chosenMember.isolation !== "worktree") && (
        <>
          <label className="field" htmlFor="ptask-resume">{t("pboard.resume.label")}</label>
          <select id="ptask-resume" className="field" value={resumeFrom} onChange={(event) => setResumeFrom(event.target.value)}>
            <option value="">{t("pboard.resume.fresh")}</option>
            {resumeSessions.map((session) => <option key={session.id} value={session.id} disabled={!session.can_resume}>
              {session.started_at.slice(0, 16).replace("T", " ")} · {session.owner_name} · {session.task_title || t("goal.card.untitled")} {session.can_resume ? "" : `· ${t(`pboard.resume.reason.${session.resume_reason}`)}`}
            </option>)}
          </select>
          <div className="sub">{t("pboard.resume.hint")}</div>
          {resumeMore && <button className="linkbtn" type="button" onClick={olderSessions}>{t("pboard.resume.older")}</button>}
          {resumeError && <div className="sub attn">{resumeError}</div>}
        </>
      )}
      {task && assignee && ((task.status === "todo" || task.status === "blocked") || launchPending) &&
        <HostCapacity projectId={projectId} pending={launchPending} />}
      {task && assignee && (task.status === "todo" || task.status === "blocked") && (
        <div className="btnrow">
          <button type="button" className="btn small" disabled={busy || writeBlocked || editorChanged || launchPending} onClick={async () => {
            if (typeof task.entity_revision !== "number") return;
            setBusy(true);
            try { await launch(task.id, task.entity_revision); onDone(); }
            catch (error) { if (error instanceof ApiError && error.status === 409) launchOperation.current = null; toast(errorText(error)); }
            finally { setBusy(false); }
          }}>{t(storedLaunchIntent(task.id) ? "pboard.launch.retry" : "pboard.launch.action")}</button>
          {editorChanged && <span className="sub">{t("pboard.launch.saveFirst")}</span>}
        </div>
      )}
      {task && launchEffectId && <div className="result-warning" role="status">
        {t(launchStatus.key)}
        {launchStatus.wait && <div>{t(waitKey(launchStatus.wait))}</div>}
        {launchStatus.detail && <div className="sub">{launchStatus.detail}</div>}
        {launchStatus.blocked && launchStatus.key === "pboard.launch.unconfirmed" && <div className="sub">{t("pboard.launch.inspectFirst")}</div>}
        {launchStatus.key === "pboard.launch.deliveryFailed" && <div className="sub">{t("pboard.launch.repairFirst")}</div>}
        <button type="button" className="linkbtn" onClick={() => launchEffect.refresh()}>{t("pboard.launch.refresh")}</button>
      </div>}

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
        <button className="btn primary" disabled={busy || writeBlocked || !title.trim() || !changed} onClick={save}>{task ? t("common.save") : t("common.create")}</button>
      </div>
      {writeBlocked && <div className="result-warning" role="status">{t("result.block.unconfirmed")}</div>}
    </Sheet>
  );
}
