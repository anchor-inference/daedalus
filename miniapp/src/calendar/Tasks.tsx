// The planner's tasks: Inbox, Today, Upcoming and Overdue, added from one line, ticked off in place,
// and dragged onto the calendar to give them a time. Personal to-dos; the agents' Board is elsewhere.

import { useState, type CSSProperties } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { Sheet } from "../ui/dialogs";
import { useQuery } from "../store";
import { addDays, clockLabel, dayLabel, type Day } from "./dates";
import { tasksViewKey } from "./data";
import { isDone } from "./items";
import { Reminders } from "./fields";
import type { Task, TaskList, TaskView } from "./types";
import { dueLabel } from "./words";
import { toWall } from "./zone";

const VIEWS: TaskView[] = ["inbox", "today", "upcoming", "overdue"];
const PRIORITY_COLOR = ["", "var(--info)", "var(--warn)", "var(--bad)"];

type PanelProps = {
  today: Day;
  zone: string;
  lists: TaskList[];
  view: TaskView;
  onView: (view: TaskView) => void;
  onAdd: (title: string, view: TaskView) => Promise<void>;
  onToggle: (task: Task) => void;
  onOpen: (task: Task) => void;
  onDragTask: (task: Task | null) => void;
  draggable: boolean;
};

export function TasksPanel(props: PanelProps) {
  const { view } = props;
  const { data, error, loading } = useQuery<Task[]>(tasksViewKey(view), { pollMs: 60000, staleMs: 5000 });
  const { data: overdue } = useQuery<Task[]>(tasksViewKey("overdue"), { pollMs: 60000, staleMs: 5000 });
  const [title, setTitle] = useState("");
  const [adding, setAdding] = useState(false);
  const tasks = (data ?? []).filter((task) => !isDone(task) || view === "today");
  const late = (overdue ?? []).filter((task) => !isDone(task)).length;

  async function add(e: React.FormEvent) {
    e.preventDefault();
    const text = title.trim();
    if (!text) return;
    setAdding(true);
    try {
      await props.onAdd(text, view);
      setTitle("");
    } finally {
      setAdding(false);
    }
  }

  return (
    <section className="cal-tasks" aria-label={t("cal.tasks")}>
      <div className="cal-side-head"><span>{t("cal.tasks")}</span></div>
      <div className="cal-task-tabs" role="tablist" aria-label={t("cal.tasks")}>
        {VIEWS.map((v) => (
          <button key={v} type="button" role="tab" aria-selected={view === v} className={`cal-task-tab ${view === v ? "on" : ""} ${v === "overdue" && late ? "late" : ""}`} onClick={() => props.onView(v)}>
            {t(`cal.tasks.${v}`)}{v === "overdue" && late > 0 && <span className="cal-count" aria-label={t("cal.tasks.overdue.count", { n: late })}>{late}</span>}
          </button>
        ))}
      </div>
      <form className="cal-task-add" onSubmit={add}>
        <Icon name="plus" size={14} />
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder={t("cal.task.add")} aria-label={t("cal.task.add")} maxLength={240} disabled={adding} />
      </form>
      {error && !data && <div className="cal-task-empty error">{error}</div>}
      {loading && !data && <div className="cal-task-empty">{t("common.loading")}</div>}
      {data && !tasks.length && <div className="cal-task-empty">{t(`cal.tasks.empty.${view}`)}</div>}
      <ul className="cal-task-list" role="tabpanel">
        {tasks.map((task) => <TaskRow key={task.id} task={task} {...props} />)}
      </ul>
      {props.draggable && tasks.length > 0 && <div className="cal-hint cal-task-tip">{t("cal.task.drag.hint")}</div>}
    </section>
  );
}

function TaskRow({ task, today, zone, lists, onToggle, onOpen, onDragTask, draggable }: PanelProps & { task: Task }) {
  const list = lists.find((l) => l.id === task.list_id);
  const blocked = task.scheduled_start ? toWall(task.scheduled_start, zone) : null;
  const overdue = !!task.due_date && task.due_date < today && !isDone(task);
  return (
    <li className={`cal-task ${isDone(task) ? "done" : ""}`} draggable={draggable && !isDone(task)}
      onDragStart={(e) => { e.dataTransfer.setData("application/x-daedalus-task", task.id); e.dataTransfer.setData("text/plain", task.title); e.dataTransfer.effectAllowed = "move"; onDragTask(task); }}
      onDragEnd={() => onDragTask(null)}>
      <button type="button" className={`cal-check round ${isDone(task) ? "on" : ""}`} role="checkbox" aria-checked={!!isDone(task)} aria-label={t("cal.task.complete.named", { title: task.title })} onClick={() => onToggle(task)} style={{ "--c": task.priority ? PRIORITY_COLOR[task.priority] : "var(--fg-3)" } as CSSProperties}>
        {isDone(task) && <Icon name="check" size={12} />}
      </button>
      <button type="button" className="cal-task-open" onClick={() => onOpen(task)}>
        <span className="cal-task-title">{task.title}</span>
        <span className="cal-task-meta">
          {task.due_date && <span className={overdue ? "late" : ""}>{dueLabel(task.due_date, today)}{task.due_time ? ` ${task.due_time}` : ""}</span>}
          {blocked && <span className="cal-task-block"><Icon name="clock" size={11} />{dayLabel(blocked.day, locale(), { day: "numeric", month: "short" })} {clockLabel(blocked.minutes, locale())}</span>}
          {list && !list.is_default && <span className="cal-task-list-name" style={{ "--c": list.color } as CSSProperties}><span className="cal-dot" />{list.name}</span>}
        </span>
      </button>
      {task.priority > 0 && <span className="cal-flag" style={{ color: PRIORITY_COLOR[task.priority] }} title={t(`cal.priority.${task.priority}`)} aria-label={t(`cal.priority.${task.priority}`)}><Icon name="flag" size={13} /></span>}
    </li>
  );
}

export type TaskDraft = { task?: Task; title: string; notes: string; list_id: string; due_date: Day | null; due_time: string | null; priority: 0 | 1 | 2 | 3; reminders: number[]; scheduled_start: string | null; scheduled_end: string | null };

export function TaskEditor({ draft: initial, lists, zone, today, busy, onClose, onSave, onDelete, onToggle }: {
  draft: TaskDraft; lists: TaskList[]; zone: string; today: Day; busy: boolean;
  onClose: () => void; onSave: (draft: TaskDraft) => void; onDelete: (task: Task) => void; onToggle: (task: Task) => void;
}) {
  const [d, setD] = useState(initial);
  const set = (patch: Partial<TaskDraft>) => setD((cur) => ({ ...cur, ...patch }));
  const blocked = d.scheduled_start && d.scheduled_end ? { from: toWall(d.scheduled_start, zone), to: toWall(d.scheduled_end, zone) } : null;
  return (
    <Sheet title={initial.task ? t("cal.task.edit") : t("cal.new.task")} onClose={onClose} size="narrow" className="cal-editor">
      <form className="cal-form" onSubmit={(e) => { e.preventDefault(); if (d.title.trim()) onSave(d); }}>
        <div className="cal-task-title-row">
          {initial.task && (
            <button type="button" className={`cal-check round big ${isDone(initial.task) ? "on" : ""}`} role="checkbox" aria-checked={!!isDone(initial.task)} aria-label={t("cal.task.complete.named", { title: initial.task.title })} onClick={() => onToggle(initial.task!)}>
              {isDone(initial.task) && <Icon name="check" size={14} />}
            </button>
          )}
          <input className="field cal-title-input" autoFocus={!initial.task} required maxLength={240} value={d.title} onChange={(e) => set({ title: e.target.value })} placeholder={t("cal.task.placeholder")} aria-label={t("cal.field.title")} />
        </div>
        <div className="cal-line">
          <Icon name="calendar" size={16} />
          <div className="cal-when-row">
            <input type="date" className="field cal-date" value={d.due_date ?? ""} aria-label={t("cal.task.due")} onChange={(e) => set({ due_date: e.target.value || null })} />
            <input type="time" className="field cal-time" step={900} value={d.due_time ?? ""} disabled={!d.due_date} aria-label={t("cal.task.due.time")} onChange={(e) => set({ due_time: e.target.value || null })} />
            <div className="cal-due-quick">
              <button type="button" className="chip select" aria-pressed={d.due_date === today} onClick={() => set({ due_date: today })}>{t("cal.today")}</button>
              <button type="button" className="chip select" aria-pressed={d.due_date === addDays(today, 1)} onClick={() => set({ due_date: addDays(today, 1) })}>{t("cal.tomorrow")}</button>
              {d.due_date && <button type="button" className="chip select" onClick={() => set({ due_date: null, due_time: null })}>{t("cal.task.nodate")}</button>}
            </div>
          </div>
        </div>
        {blocked && (
          <div className="cal-line">
            <Icon name="clock" size={16} />
            <div className="cal-blocked">
              <span>{t("cal.task.blocked", { day: dayLabel(blocked.from.day, locale(), { weekday: "short", day: "numeric", month: "short" }), from: clockLabel(blocked.from.minutes, locale()), to: clockLabel(blocked.to.minutes, locale()) })}</span>
              <button type="button" className="btn small ghost" onClick={() => set({ scheduled_start: null, scheduled_end: null })}>{t("cal.task.unblock")}</button>
            </div>
          </div>
        )}
        <div className="cal-line">
          <Icon name="flag" size={16} />
          <div className="segmented inline cal-priority" role="radiogroup" aria-label={t("cal.task.priority")}>
            {([0, 1, 2, 3] as const).map((p) => (
              <button key={p} type="button" role="radio" aria-checked={d.priority === p} className={d.priority === p ? "on" : ""} onClick={() => set({ priority: p })}>{t(`cal.priority.${p}`)}</button>
            ))}
          </div>
        </div>
        {lists.length > 1 && (
          <div className="cal-line">
            <Icon name="inbox" size={16} />
            <select className="field cal-grow" value={d.list_id} onChange={(e) => set({ list_id: e.target.value })} aria-label={t("cal.task.list")}>
              {lists.map((l) => <option key={l.id} value={l.id}>{l.name}</option>)}
            </select>
          </div>
        )}
        <div className="cal-line">
          <Icon name="bell" size={16} />
          <div className="cal-grow"><Reminders value={d.reminders} onChange={(reminders) => set({ reminders })} /></div>
        </div>
        <div className="cal-line top">
          <Icon name="journal" size={16} />
          <textarea className="field cal-grow" rows={3} value={d.notes} onChange={(e) => set({ notes: e.target.value })} placeholder={t("cal.task.notes")} aria-label={t("cal.task.notes")} />
        </div>
        <div className="sheet-foot cal-foot">
          {initial.task && <button type="button" className="btn danger" disabled={busy} onClick={() => onDelete(initial.task!)}><Icon name="trash" size={14} /> {t("common.delete")}</button>}
          <span className="cal-spacer" />
          <button type="button" className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button type="submit" className="btn primary" disabled={busy || !d.title.trim()}>{t("common.save")}</button>
        </div>
      </form>
    </Sheet>
  );
}
