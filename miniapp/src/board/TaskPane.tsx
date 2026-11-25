// One board card, opened beside the board: what it is, where it came from and who does it, its
// description and acceptance, its checklist, the moves the host allows, and what happened to it. A
// pane rather than a dialog, so the board stays in view and another card opens in its place; on a
// phone the same contents fill a sheet. A chat's own board mounts it in its panel at the panel's width.

import { useEffect, useRef, useState, type ReactNode } from "react";
import { copyText } from "../ui/components";
import { absTime, relTimeLong } from "../format";
import { Icon } from "../icons";
import { t } from "../i18n";
import { navigate, projectPagePath } from "../router";
import { invalidate, useQuery } from "../store";
import { Button, IconButton, OverflowMenu, Select, Sheet, useLayer, type MenuItem } from "../ui/primitives";
import { ChatFinish } from "./ChatFinish";
import { closeCard, closeItems } from "./operatorClose";
import type { ChatInfo } from "./BoardColumns";
import type { TaskWrites } from "./useTaskWrites";
import {
  cardState, columnLabel, dueLabel, dueValue, eventActor, eventText, isFinished, movesFor, priorityLabel, shortId, taskText,
  type BoardTask, type TaskEvent,
} from "./tasks";

/** How far back a card's history goes: the host keeps its events this long and no longer. */
const HISTORY_DAYS = 7;

export type TaskPaneProps = {
  task: BoardTask;
  chats: ChatInfo;
  writes: TaskWrites;
  onClose: () => void;
  /** Standing in a narrow panel in place of its list: the head leads back to the list instead of
   *  closing the pane, and the pane takes the panel's width. */
  back?: { label: string; onBack: () => void };
  /** The chat the pane stands in, when it is a chat's own panel: the way to that chat is not offered
   *  on its own cards. */
  here?: string;
  onChanged: () => void;
  onOpenSession: (sessionId: string) => void;
  /** Another card of the board, by id: a dependency opens in the pane's place. */
  onOpenTask?: (id: string) => void;
  /** The board's cards, to name the cards this one waits for. */
  others?: BoardTask[];
  toast: (text: string) => void;
  /** Gone from every board: the caller leaves the card's address. */
  onRemoved?: () => void;
};

/** The pane beside the board, 420 px on the Board page and the panel's width in a chat's panel. */
export function TaskPane(props: TaskPaneProps) {
  const { task, onClose, back } = props;
  // Escape closes the pane, unless a menu or a list opened from it is on top.
  useLayer(onClose, true);
  return (
    <aside className={`task-pane ${back ? "in-panel" : ""}`} aria-label={t("board.pane")} data-task={task.id}>
      <div className="task-pane-head">
        {back && <IconButton icon="back" label={back.label} onClick={back.onBack} className="task-pane-back" />}
        <span className="task-pane-kind">{t("board.pane")}</span>
        <span className="task-pane-code mono">{shortId(task.id)}</span>
        <span className="grow" />
        <OverflowMenu label={t("board.actions")} items={menuItems(props)} />
        {!back && <IconButton icon="close" label={t("board.pane.close")} onClick={onClose} />}
      </div>
      <div className="task-pane-body">
        <TaskBody {...props} />
      </div>
      <TaskFoot {...props} />
    </aside>
  );
}

/** The same card in a sheet, for a phone. */
export function TaskSheet(props: TaskPaneProps) {
  const { task, onClose } = props;
  return (
    <Sheet ariaLabel={task.title} onClose={onClose} className="task-sheet" size="full"
      head={<><span className="task-pane-code mono">{shortId(task.id)}</span><OverflowMenu small label={t("board.actions")} items={menuItems(props)} /></>}
      footer={<TaskFoot {...props} bare />}>
      <div className="task-pane in-sheet" data-task={task.id}>
        <TaskBody {...props} />
      </div>
    </Sheet>
  );
}

function chatOf(task: BoardTask): string | null {
  return task.origin_session_id ?? task.session_id;
}

/** The card's chat, unless that is the one the pane stands in: opening it would go nowhere. */
function otherChat(task: BoardTask, here?: string): string | null {
  const chat = chatOf(task);
  return chat && chat !== here ? chat : null;
}

function menuItems({ task, writes, onChanged, onOpenSession, toast, onRemoved, here }: TaskPaneProps): MenuItem[] {
  const chat = otherChat(task, here);
  return [
    ...(chat ? [{ label: t("board.open.chat"), icon: "chat" as const, onSelect: () => onOpenSession(chat) }] : []),
    { label: t("board.copyid"), icon: "copy", ticks: true, onSelect: async () => { const ok = await copyText(task.id); if (!ok) toast(task.id); return ok; } },
    "-",
    ...closeItems(task, writes.blocked(task), { onDone: onChanged, toast, onRemoved }),
  ];
}

function TaskBody(props: TaskPaneProps) {
  const { task, chats, writes, onOpenSession, onChanged, toast, onOpenTask, others = [], here } = props;
  const finished = isFinished(task);
  const state = cardState(task, chats.running);
  const text = taskText(task);
  const done = task.checklist.filter((c) => c.done).length;
  const chat = otherChat(task, here);
  const chatName = chat ? chats.title(chat) : undefined;
  const blocked = writes.blocked(task);
  const moves = movesFor(task);
  const refused = (where: string) => (writes.refusal?.taskId === task.id && writes.refusal.where === where ? writes.refusal.text : null);
  const dot = finished ? "" : state?.tone ?? "";
  return (
    <>
      <h2 className="task-pane-title">{task.title}</h2>
      {task.needs_you && !finished && (
        <div className="task-pane-need" role="status" data-needs={task.needs_you}>
          <span className="kanban-dot attention" aria-hidden="true" />
          <span>{t(`board.need.${task.needs_you}`)}</span>
        </div>
      )}
      <div className="task-kv">
        <Kv label={t("board.kv.status")}>
          <span className="task-kv-status"><span className={`kanban-dot ${dot}`} aria-hidden="true" />{columnLabel(task.status)}
            {state && <span className={`task-kv-state ${state.tone}`}>· {state.text}</span>}</span>
        </Kv>
        {chat && (
          <Kv label={t("board.kv.chat")}>
            <button type="button" className="task-link" onClick={() => onOpenSession(chat)}>
              <Icon name="chat" size={16} /><span className="truncate">{chatName ?? t("board.of.gone")}</span><Icon name="forward" size={14} />
            </button>
          </Kv>
        )}
        {task.project_id && (
          <Kv label={t("board.kv.project")}>
            <button type="button" className="task-link" aria-label={t("board.on.project", { name: task.project_name ?? task.project_id })}
              onClick={() => navigate(projectPagePath(task.project_id!, "board", { task: task.id }))}>
              <Icon name="folder" size={16} /><span className="truncate">{task.project_name ?? task.project_id}</span>
              <span className="task-link-hint">{t("board.kv.project.board")}</span><Icon name="forward" size={14} />
            </button>
          </Kv>
        )}
        <Kv label={t("board.kv.assignee")} refusal={refused("assignee")}>
          <AssigneePick task={task} writes={writes} disabled={blocked || finished} />
        </Kv>
        <Kv label={t("board.priority")} refusal={refused("priority")}>
          <Select label={t("board.priority")} value={String(task.priority)} disabled={blocked || finished} className={`task-select ${task.priority <= 2 ? "high" : ""}`}
            options={[1, 2, 3, 4, 5].map((n) => ({ id: String(n), label: priorityLabel(n) }))}
            onChange={(value) => void writes.edit(task, { priority: Number(value) }, "priority")} />
        </Kv>
        <Kv label={t("board.kv.due")} refusal={refused("due")}>
          <DuePick task={task} writes={writes} disabled={blocked || finished} />
        </Kv>
        <Kv label={t("board.kv.changed")}><span className="task-kv-quiet" title={absTime(task.updated_at)}>{relTimeLong(task.updated_at)}</span></Kv>
      </div>

      <Block title={t("board.description")}>
        {text.description.length > 0
          ? <div className="task-prose">{text.description.map((part, i) => <p key={i}>{part.lead && <span className="task-lead">{part.lead}: </span>}{part.text}</p>)}</div>
          : <div className="task-none">{t("board.description.none")}</div>}
      </Block>
      {text.acceptance.length > 0 && (
        <Block title={t("board.acceptance")}>
          <div className="task-prose quiet">{text.acceptance.map((part, i) => <p key={i}>{part}</p>)}</div>
        </Block>
      )}
      {task.checklist.length > 0 && (
        <Block title={<>{t("board.subtasks")} <span className="task-sec-n">{t("board.subtasks.count", { done, n: task.checklist.length })}</span></>}>
          <div className="task-progress" aria-hidden="true"><i style={{ width: `${Math.round((100 * done) / task.checklist.length)}%` }} /></div>
          {task.checklist.map((item, i) => (
            <div key={i}>
              <button type="button" className={`task-sub ${item.done ? "done" : ""}`} role="checkbox" aria-checked={item.done} disabled={blocked || task.status === "done"} onClick={() => void writes.check(task, i)}>
                <span className="task-box" aria-hidden="true">{item.done && <Icon name="check" size={12} />}</span>
                <span className="task-sub-text">{item.text}</span>
              </button>
              {refused(`check:${i}`) && <div className="task-refusal" role="alert">{refused(`check:${i}`)}</div>}
            </div>
          ))}
        </Block>
      )}
      {task.depends_on.length > 0 && (
        <Block title={t("board.depends")}>
          {task.depends_on.map((id) => {
            const other = others.find((o) => o.id === id);
            return (
              <button key={id} type="button" className="task-dep" disabled={!other || !onOpenTask} onClick={() => onOpenTask?.(id)}>
                <span className={`kanban-dot ${other && isFinished(other) ? "" : "attention"}`} aria-hidden="true" />
                <span className="truncate">{other?.title ?? id}</span>
                {other && <span className="task-dep-status">{columnLabel(other.status)}</span>}
              </button>
            );
          })}
        </Block>
      )}
      {task.notes && (
        <Block title={t(task.project_id ? "board.history" : "board.notes")}>
          <pre className="task-notes">{task.notes}</pre>
        </Block>
      )}
      <ChatFinish task={task} onOpenSession={onOpenSession} onChanged={onChanged} toast={toast} reopen={false} />
      {moves.length > 0 && (
        <Block title={t("board.moveto")}>
          <div className="task-moves">
            {moves.map((status) => (
              <Button key={status} size="md" className="task-move" disabled={blocked} busy={writes.busy === `move:${status}`} onClick={() => void writes.move(task, status, `move:${status}`)}>{columnLabel(status)}</Button>
            ))}
          </div>
          {moves.map((status) => refused(`move:${status}`) && <div key={status} className="task-refusal" role="alert">{refused(`move:${status}`)}</div>)}
        </Block>
      )}
      {blocked && <div className="task-refusal" role="status">{t("result.block.unconfirmed")}</div>}
      <History task={task} live={state?.tone === "live"} />
    </>
  );
}

function Kv({ label, refusal, children }: { label: string; refusal?: string | null; children: ReactNode }) {
  return (
    <>
      <div className="task-kv-row"><span className="task-kv-key">{label}</span><span className="task-kv-value">{children}</span></div>
      {refusal && <div className="task-refusal" role="alert">{refusal}</div>}
    </>
  );
}

function Block({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <section className="task-block">
      <div className="task-sec">{title}</div>
      {children}
    </section>
  );
}

/** Who does the card: the agent of its chat, the operator, or the member a project's team gave it. A
 *  member is chosen on the project's own board, where the team is; here the card can only be handed
 *  back to the agents or kept by the operator. */
function AssigneePick({ task, writes, disabled }: { task: BoardTask; writes: TaskWrites; disabled: boolean }) {
  const who = task.assignee;
  const value = !who ? "agent" : who.kind === "operator" ? "you" : `staff:${who.id}`;
  const options = [
    { id: "agent", label: t("board.who.agent.full") },
    { id: "you", label: t("board.who.you.full") },
    ...(who?.kind === "staff" ? [{ id: value, label: t("board.who.staff", { name: who.name }) }] : []),
  ];
  return (
    <Select label={t("board.kv.assignee")} value={value} options={options} disabled={disabled} className={`task-select ${value === "you" ? "you" : ""}`}
      onChange={(next) => {
        if (next === "you") void writes.edit(task, { assignee_operator: true }, "assignee");
        else if (next === "agent") void writes.edit(task, who?.kind === "staff" ? { assignee_staff_id: "" } : { assignee_operator: false }, "assignee");
      }} />
  );
}

/** The due date as words ("Tomorrow, 26 Nov"), opening the browser's date picker; an x clears it. */
function DuePick({ task, writes, disabled }: { task: BoardTask; writes: TaskWrites; disabled: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const due = task.due_at ?? null;
  const words = due ? `${dueLabel(due, new Date(), true)}${task.overdue ? ` · ${t("board.due.late")}` : ""}` : t("board.due.none");
  const open = () => {
    const field = input.current;
    if (!field) return;
    try { field.showPicker(); } catch { field.focus(); }
  };
  return (
    <span className="task-due">
      <button type="button" className={`task-due-btn ${task.overdue ? "late" : ""} ${due ? "" : "none"}`} disabled={disabled} onClick={open}>{words}</button>
      <input ref={input} type="date" className="task-due-input" aria-label={t("board.kv.due")} value={dueValue(due)} disabled={disabled} tabIndex={-1}
        onChange={(e) => { if (e.target.value) void writes.edit(task, { due_at: e.target.value }, "due"); }} />
      {due && !disabled && <IconButton icon="close" size={28} label={t("board.due.clear")} onClick={() => void writes.edit(task, { due_at: "" }, "due")} />}
    </span>
  );
}

function History({ task, live }: { task: BoardTask; live: boolean }) {
  const key = `/api/board/${encodeURIComponent(task.id)}/events?limit=50`;
  const { data, error } = useQuery<TaskEvent[]>(key, { staleMs: 5000 });
  // A card that changed has a new line to show. The change used to ride in the address as a
  // parameter the host does not read, which the route contract refuses; the list is asked again instead.
  const seen = useRef(`${key} ${task.updated_at}`);
  useEffect(() => {
    const now = `${key} ${task.updated_at}`;
    if (seen.current !== now) { seen.current = now; invalidate(key); }
  }, [key, task.updated_at]);
  const events = [...(data ?? [])].reverse();
  const old = Date.now() - Date.parse(task.created_at) > HISTORY_DAYS * 86400_000;
  // The host keeps events a week: an older card's earliest lines are gone, and the list says so.
  const cut = old && !(data ?? []).some((e) => e.type === "task.created");
  return (
    <Block title={t("board.activity")}>
      {error && !data && <div className="task-none">{error}</div>}
      {data && events.length === 0 && <div className="task-none">{t("board.activity.none", { n: HISTORY_DAYS })}</div>}
      {events.length > 0 && (
        <ol className="task-log">
          {events.map((event, i) => {
            const actor = eventActor(event);
            return (
              <li key={event.seq} className="task-log-row">
                <span className="task-log-rail" aria-hidden="true"><span className={`kanban-dot ${i === 0 && live ? "live" : ""}`} /><i /></span>
                <span className="task-log-body">
                  <span className="task-log-text">{eventText(event)}</span>
                  <span className="task-log-time">{[absTime(event.at), actor].filter(Boolean).join(" · ")}</span>
                </span>
              </li>
            );
          })}
        </ol>
      )}
      {cut && events.length > 0 && <div className="task-none">{t("board.activity.cut", { n: HISTORY_DAYS })}</div>}
    </Block>
  );
}

/** The pane's foot: the way to the card's chat, and the one way to end it or take it back to work. */
function TaskFoot({ task, writes, onOpenSession, onChanged, toast, here, bare = false }: TaskPaneProps & { bare?: boolean }) {
  const [busy, setBusy] = useState(false);
  const chat = otherChat(task, here);
  const blocked = writes.blocked(task);
  const reopenable = (task.status === "done" && task.finished_by) || task.status === "dropped";
  const open = !isFinished(task);
  const refusal = writes.refusal?.taskId === task.id && writes.refusal.where === "reopen" ? writes.refusal.text : null;
  const body = (
    <>
      {chat && <Button icon="chat" onClick={() => onOpenSession(chat)}>{t("board.open.chat")}</Button>}
      {open && <Button icon="check" className="quietly" disabled={blocked} onClick={() => void closeCard(task, "done", { onDone: onChanged, toast })}>{t("pboard.close.done.action")}</Button>}
      {reopenable && (
        <Button icon="undo" className="quietly" disabled={blocked} busy={busy}
          onClick={async () => { setBusy(true); await writes.move(task, "todo", "reopen"); setBusy(false); }}>{t("board.finishedBy.reopen")}</Button>
      )}
      {refusal && <div className="task-refusal" role="alert">{refusal}</div>}
    </>
  );
  return bare ? body : <div className="task-pane-foot">{body}</div>;
}
