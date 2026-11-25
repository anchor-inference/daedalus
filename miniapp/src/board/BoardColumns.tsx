// The board's cards in columns: the Board page's grid of status columns, and the same columns stacked
// one under another in a narrow place (a chat's own board in its panel). One card, one column, one
// way to drag: a page that mounts the board passes the columns it wants and gets the same drawing.

import { useState, type DragEvent } from "react";
import { Icon } from "../icons";
import { t } from "../i18n";
import { absTime, relTime } from "../format";
import { lanes, type Scope } from "../screens/boardview";
import { IconButton } from "../ui/primitives";
import { assigneeName, cardState, DEFAULT_PRIORITY, dueLabel, isFinished, type BoardTask, type Status } from "./tasks";

/** What the board knows about the chats its cards came from: a title for a lane or a card, and
 *  whether the chat is at work or waits on the operator, for the lane's dot and the card's line. */
export type ChatInfo = { title: (id: string) => string | undefined; running: (id: string) => boolean; waiting: (id: string) => boolean };

/** A move the host refused, drawn at the card or the control that asked for it. */
export type Refusal = { taskId: string; where: string; text: string };

export type BoardColumn = {
  key: string;
  name: string;
  tasks: BoardTask[];
  /** The status a card dropped here moves to; left out, nothing can be dropped. */
  drop?: Status;
  /** The column's own "add a task" button. */
  onAdd?: () => void;
  /** Folded by default in the stacked form (the finished cards of a chat's board). */
  folded?: boolean;
};

export type BoardColumnsProps = {
  columns: BoardColumn[];
  /** How the cards of a column are laned: by project for every card, by chat in one project's view. */
  scope: Scope["kind"] | "none";
  /** `grid`: the page's columns side by side; `stacked`: one under another, with compact cards. */
  layout?: "grid" | "stacked";
  selected?: string | null;
  onOpen: (task: BoardTask) => void;
  chats: ChatInfo;
  /** Whether a card can be dragged at all, and which statuses it may go to. */
  canMove?: (task: BoardTask) => Status[];
  onMove?: (task: BoardTask, status: Status) => void;
  refusal?: Refusal | null;
  /** Cards first drawn in a long column; the rest come with "Show more". */
  limit?: number;
};

const LIMIT = 60;

export function BoardColumns({ columns, scope, layout = "grid", selected = null, onOpen, chats, canMove, onMove, refusal = null, limit = LIMIT }: BoardColumnsProps) {
  const [dragging, setDragging] = useState<BoardTask | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const [unfolded, setUnfolded] = useState<Record<string, boolean>>({});
  const [more, setMore] = useState<Record<string, boolean>>({});
  const stacked = layout === "stacked";
  const accepts = (column: BoardColumn) => Boolean(dragging && column.drop && dragging.status !== column.drop && (canMove?.(dragging) ?? []).includes(column.drop));
  const dropProps = (column: BoardColumn) => ({
    onDragOver: (e: DragEvent) => {
      if (!accepts(column)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      if (over !== column.key) setOver(column.key);
    },
    onDragLeave: (e: DragEvent) => {
      if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
      if (over === column.key) setOver(null);
    },
    onDrop: (e: DragEvent) => {
      e.preventDefault();
      const task = dragging;
      const ok = accepts(column);
      setDragging(null);
      setOver(null);
      if (task && ok && column.drop) onMove?.(task, column.drop);
    },
  });
  return (
    <div className={`kanban board-cols ${stacked ? "stacked" : "grid"}`} data-layout={layout}
      style={stacked ? undefined : { gridTemplateColumns: `repeat(${columns.length}, minmax(240px, 1fr))` }}>
      {columns.map((column) => {
        const folded = stacked && column.folded && !unfolded[column.key];
        const shown = more[column.key] ? column.tasks : column.tasks.slice(0, limit);
        const grouped = lanes(shown, scope === "none" ? "chat" : scope, chats.title);
        return (
          <section key={column.key} className={`kanban-col ${over === column.key ? "over" : ""} ${dragging && accepts(column) ? "can-drop" : ""} ${column.tasks.length === 0 ? "is-empty" : ""}`}
            data-column={column.key} aria-label={column.name} {...dropProps(column)}>
            <div className="kanban-head">
              {stacked && column.folded ? (
                <button type="button" className="kanban-fold" aria-expanded={!folded} onClick={() => setUnfolded({ ...unfolded, [column.key]: !unfolded[column.key] })}>
                  <span className="kanban-name">{column.name}</span>
                  <span className={`kanban-caret ${folded ? "" : "open"}`}><Icon name="forward" size={14} /></span>
                </button>
              ) : <span className="kanban-name">{column.name}</span>}
              <span className="kanban-n">{column.tasks.length}</span>
              <span className="grow" />
              {column.onAdd && <IconButton icon="plus" size={28} label={t("board.col.add", { name: column.name })} onClick={column.onAdd} className="kanban-add" />}
            </div>
            {!folded && column.tasks.length === 0 && !stacked && <div className="kanban-empty">{t("board.col.none")}</div>}
            {!folded && grouped.map((lane) => (
              <div key={lane.key} className="kanban-lane" data-lane={lane.key}>
                {lane.label !== null && <LaneHead label={lane.label} chat={lane.chat} scope={scope} chats={chats} />}
                {lane.items.map((task) => (
                  <TaskCard key={task.id} task={task} compact={stacked} showChat={scope === "all"} chats={chats} selected={selected === task.id} onOpen={() => onOpen(task)}
                    refusal={refusal?.taskId === task.id && refusal.where === "card" ? refusal.text : null}
                    dragging={dragging?.id === task.id}
                    drag={onMove && (canMove?.(task) ?? []).length > 0 ? { start: () => setDragging(task), end: () => { setDragging(null); setOver(null); } } : undefined} />
                ))}
              </div>
            ))}
            {!folded && !more[column.key] && column.tasks.length > limit && (
              <button type="button" className="btn small ghost kanban-more" onClick={() => setMore({ ...more, [column.key]: true })}>{t("board.col.more", { n: column.tasks.length - limit })}</button>
            )}
          </section>
        );
      })}
    </div>
  );
}

function LaneHead({ label, chat, scope, chats }: { label: string; chat: string | null; scope: BoardColumnsProps["scope"]; chats: ChatInfo }) {
  const running = chat ? chats.running(chat) : false;
  const waiting = chat ? chats.waiting(chat) : false;
  const tone = waiting ? "attention" : running ? "live" : "";
  const fallback = scope === "project" ? t("board.lane.nochat") : t("pbph.noproject");
  return (
    <div className="kanban-lane-head">
      {tone && <span className={`kanban-dot ${tone}`} aria-hidden="true" />}
      <span className="kanban-lane-name">{label || fallback}</span>
      <span className="grow" />
      {tone && <span className={`kanban-lane-state ${tone}`}>{t(waiting ? "board.lane.waiting" : "board.state.running")}</span>}
    </div>
  );
}

export type TaskCardProps = {
  task: BoardTask;
  chats: ChatInfo;
  selected?: boolean;
  onOpen: () => void;
  /** The chat it came from, on a board of every chat. */
  showChat?: boolean;
  /** One line of state under the title, for a narrow column. */
  compact?: boolean;
  dragging?: boolean;
  drag?: { start: () => void; end: () => void };
  refusal?: string | null;
};

/** One task: its title over what it waits on, the chat it came from, and a line of who does it, when
 *  it is due, how far its checklist is and its priority when that is not the usual one. */
export function TaskCard({ task, chats, selected = false, onOpen, showChat = false, compact = false, dragging = false, drag, refusal = null }: TaskCardProps) {
  const finished = isFinished(task);
  const state = cardState(task, chats.running);
  const done = task.checklist.filter((c) => c.done).length;
  const chat = task.origin_session_id ?? task.session_id;
  const chatName = chat ? chats.title(chat) : undefined;
  const you = task.assignee?.kind === "operator";
  const due = task.due_at && !finished ? dueLabel(task.due_at) : null;
  const marked = task.priority !== DEFAULT_PRIORITY && !finished;
  const className = ["task-card", selected && "on", finished && "finished", compact && "compact", dragging && "dragging", drag && "draggable"].filter(Boolean).join(" ");
  const dragProps = drag ? {
    draggable: true,
    onDragStart: (e: DragEvent) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", task.id); drag.start(); },
    onDragEnd: drag.end,
  } : {};
  if (compact) {
    const meta = [state?.text, assigneeName(task), task.checklist.length ? `${done}/${task.checklist.length}` : null, finished ? relTime(task.updated_at) : null].filter(Boolean).join(" · ");
    return (
      <button type="button" className={className} data-task={task.id} aria-pressed={selected} onClick={onOpen} {...dragProps}>
        <span className="task-card-title">{task.title}</span>
        <span className={`task-card-line ${state?.tone ?? ""}`}>{meta}</span>
        {refusal && <span className="task-card-refusal" role="alert">{refusal}</span>}
      </button>
    );
  }
  return (
    <button type="button" className={className} data-task={task.id} aria-pressed={selected} onClick={onOpen} title={absTime(task.updated_at)} {...dragProps}>
      <span className="task-card-title">{task.title}</span>
      {state && <span className={`task-card-state ${state.tone}`} data-needs={task.needs_you ?? undefined}><span className="kanban-dot" aria-hidden="true" />{state.text}</span>}
      {showChat && chatName && <span className="task-card-chat"><Icon name="chat" size={12} /><span className="truncate">{chatName}</span></span>}
      <span className="task-card-meta">
        <span className={`task-card-who ${you ? "you" : ""}`}>{assigneeName(task)}</span>
        {due && <span className={`task-card-due ${task.overdue ? "late" : ""}`}><Icon name="calendar" size={12} />{due}</span>}
        {task.checklist.length > 0 && <span className="task-card-subs"><Icon name="tasks" size={12} />{done}/{task.checklist.length}</span>}
        <span className="grow" />
        {marked && <span className={`task-card-prio p${task.priority}`}><Icon name="flag" size={12} />P{task.priority}</span>}
      </span>
      {refusal && <span className="task-card-refusal" role="alert">{refusal}</span>}
    </button>
  );
}
