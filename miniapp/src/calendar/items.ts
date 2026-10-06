// Events and tasks turned into what the views draw. An event of a day or more — all-day, or timed
// across 24 hours or longer — is a bar in the all-day row; anything shorter is a block in the time
// grid, cut at midnight into one piece per day it touches. A task with a time block is a block like
// an event; a task with only a due date is a line in the all-day row of its day.

import { addDays, diffDays, type Day } from "./dates";
import type { Item, Occurrence, Task } from "./types";
import { toWall } from "./zone";

const DAY_MS = 86400000;

export function eventItem(event: Occurrence, zone: string): Item {
  if (event.all_day) {
    const first = event.start_date || event.start_at.slice(0, 10);
    const end = event.end_date || event.end_at.slice(0, 10);
    const last = end > first ? addDays(end, -1) : first;
    return { key: `e:${event.id}`, kind: "event", title: event.title, color: event.color, start: Date.parse(`${first}T00:00:00Z`), end: Date.parse(`${end}T00:00:00Z`), allDay: true, firstDay: first, lastDay: last, event, location: event.location };
  }
  const start = Date.parse(event.start_at);
  const end = Math.max(Date.parse(event.end_at), start);
  const firstDay = toWall(start, zone).day;
  const lastDay = end > start ? toWall(end - 1, zone).day : firstDay;
  return { key: `e:${event.id}`, kind: "event", title: event.title, color: event.color, start, end, allDay: end - start >= DAY_MS, firstDay, lastDay, event, location: event.location };
}

export function taskItem(task: Task, zone: string, color: string): Item | null {
  const done = !!task.done_at;
  if (task.scheduled_start && task.scheduled_end) {
    const start = Date.parse(task.scheduled_start);
    const end = Math.max(Date.parse(task.scheduled_end), start + 15 * 60000);
    return { key: `t:${task.id}`, kind: "task", title: task.title, color, start, end, allDay: false, firstDay: toWall(start, zone).day, lastDay: toWall(end - 1, zone).day, task, done };
  }
  if (task.due_date) {
    const at = Date.parse(`${task.due_date}T00:00:00Z`);
    return { key: `t:${task.id}`, kind: "task", title: task.title, color, start: at, end: at + DAY_MS, allDay: true, firstDay: task.due_date, lastDay: task.due_date, task, done };
  }
  return null;
}

/** The piece of a timed item that falls on `day`, in minutes after its midnight, or null. */
export function segment(item: Item, day: Day, zone: string): { start: number; end: number; continuesBefore: boolean; continuesAfter: boolean } | null {
  if (item.allDay || day < item.firstDay || day > item.lastDay) return null;
  const from = toWall(item.start, zone);
  const to = toWall(item.end, zone);
  const start = from.day === day ? from.minutes : 0;
  // An end at exactly midnight belongs to the day before, drawn to its bottom edge.
  const end = to.day === day ? to.minutes : diffDays(day, to.day) >= 1 ? 1440 : 0;
  if (end <= start && !(item.end === item.start && from.day === day)) return null;
  return { start, end: Math.max(end, start), continuesBefore: from.day < day, continuesAfter: to.day > day && !(diffDays(day, to.day) === 1 && to.minutes === 0) };
}

/** Whether an item is drawn on a day at all, in any row. */
export function onDay(item: Item, day: Day): boolean {
  return day >= item.firstDay && day <= item.lastDay;
}

/** The order a day's items are listed in: all-day first, then by start, longer first, then by title. */
export function byStart(a: Item, b: Item): number {
  if (a.allDay !== b.allDay) return a.allDay ? -1 : 1;
  return a.start - b.start || (b.end - b.start) - (a.end - a.start) || a.title.localeCompare(b.title);
}
