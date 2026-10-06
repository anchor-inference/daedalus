// The planner's requests: the keys the screen reads through the shared cache, and the writes, each in
// one place so a move, an edit and an undo all send the same body.

import { api } from "../api";
import { invalidate } from "../store";
import { addDays, type Day } from "./dates";
import type { Occurrence, Scope, Task } from "./types";
import { fromWall } from "./zone";

export const CALENDARS = "/api/calendar/calendars";
export const SETTINGS = "/api/calendar/settings";
export const ACCOUNTS = "/api/calendar/accounts";
export const LISTS = "/api/planner/lists";

export function eventsKey(start: Day, end: Day, zone: string): string {
  return `/api/calendar/events?start=${encodeURIComponent(fromWall(start, 0, zone))}&end=${encodeURIComponent(fromWall(end, 0, zone))}`;
}

/** Tasks due or time-blocked inside the visible range, for drawing them on the calendar. */
export function tasksRangeKey(start: Day, end: Day, zone: string): string {
  return `/api/planner/tasks?start=${encodeURIComponent(fromWall(start, 0, zone))}&end=${encodeURIComponent(fromWall(end, 0, zone))}`;
}

export function tasksViewKey(view: string): string {
  return `/api/planner/tasks?view=${view}`;
}

/** After any write: every event range and task list on screen reads again. */
export function refreshPlanner(): void {
  invalidate("/api/calendar/events");
  invalidate("/api/planner/tasks");
}

/** The stored event, the series master for a recurring one. */
export type StoredEvent = Omit<Occurrence, "id" | "occurrence_start" | "recurring"> & { id: string };

/** The fields an event is written with. An all-day event carries its dates as well as midnight
 *  instants, the end exclusive, as the contract has it. */
export type EventFields = {
  calendar_id: string;
  title: string;
  description: string;
  location: string;
  start_at: string;
  end_at: string;
  all_day: boolean;
  start_date?: Day | null;
  end_date?: Day | null;
  timezone: string;
  recurrence: string;
  reminders: number[];
  color: string | null;
};

export function fieldsOf(event: Occurrence | StoredEvent): EventFields {
  return {
    calendar_id: event.calendar_id, title: event.title, description: event.description ?? "", location: event.location ?? "",
    start_at: event.start_at, end_at: event.end_at, all_day: !!event.all_day, start_date: event.start_date ?? null, end_date: event.end_date ?? null,
    timezone: event.timezone, recurrence: event.recurrence ?? "", reminders: event.reminders ?? [], color: event.color_override ?? null,
  };
}

/** All-day dates as the instants the contract wants beside them. `lastDay` is inclusive. */
export function allDayTimes(firstDay: Day, lastDay: Day): Pick<EventFields, "start_at" | "end_at" | "start_date" | "end_date"> {
  const end = addDays(lastDay, 1);
  return { start_at: `${firstDay}T00:00:00Z`, end_at: `${end}T00:00:00Z`, start_date: firstDay, end_date: end };
}

export function createEvent(fields: EventFields): Promise<Occurrence> {
  return api.post<Occurrence>("/api/calendar/events", fields);
}

/** Writes an occurrence's new fields.

    For `scope: "this"` the host writes an override of that one occurrence. For `scope: "all"` on a
    recurring event the change is made to the series: the times the reader moved this occurrence by
    are applied to the master's own times, because the occurrence being edited is usually not the
    first one, and sending its date as the series start would move the whole series to it. */
export async function updateEvent(occurrence: Occurrence, fields: EventFields, scope: Scope): Promise<{ version?: number }> {
  if (scope === "all" && occurrence.recurring) {
    const master = await api.get<StoredEvent>(`/api/calendar/events/${encodeURIComponent(occurrence.event_id)}`);
    const shiftStart = Date.parse(fields.start_at) - Date.parse(occurrence.start_at);
    const shiftEnd = Date.parse(fields.end_at) - Date.parse(occurrence.end_at);
    const start = new Date(Date.parse(master.start_at) + shiftStart).toISOString();
    const end = new Date(Date.parse(master.end_at) + shiftEnd).toISOString();
    const dates = fields.all_day ? allDayTimes(start.slice(0, 10), addDays(end.slice(0, 10), -1)) : { start_at: start, end_at: end, start_date: null, end_date: null };
    return api.put(`/api/calendar/events/${encodeURIComponent(occurrence.event_id)}`, { ...fields, ...dates, version: master.version, scope: "all" });
  }
  return api.put(`/api/calendar/events/${encodeURIComponent(occurrence.event_id)}`, { ...fields, version: occurrence.version, scope, occurrence_start: occurrence.occurrence_start });
}

export function deleteEvent(occurrence: Occurrence, scope: Scope): Promise<unknown> {
  const query = new URLSearchParams({ version: String(occurrence.version), scope });
  if (scope === "this") query.set("occurrence_start", occurrence.occurrence_start);
  return api.delete(`/api/calendar/events/${encodeURIComponent(occurrence.event_id)}?${query}`);
}

export type TaskFields = Partial<Omit<Task, "id" | "version" | "done_at">>;

export function createTask(fields: TaskFields): Promise<Task> {
  return api.post<Task>("/api/planner/tasks", fields);
}

export function updateTask(task: Task, fields: TaskFields): Promise<Task> {
  const { id: _id, done_at: _done, version, ...rest } = task;
  return api.put<Task>(`/api/planner/tasks/${encodeURIComponent(task.id)}`, { ...rest, ...fields, version });
}

export function completeTask(task: Task, done: boolean): Promise<Task> {
  return api.post<Task>(`/api/planner/tasks/${encodeURIComponent(task.id)}/complete`, { done });
}

export function deleteTask(task: Task): Promise<unknown> {
  return api.delete(`/api/planner/tasks/${encodeURIComponent(task.id)}?version=${task.version}`);
}

/** The colours offered for a calendar or an event. Mid-tone, so a tinted block keeps its text
 *  readable in both themes. */
export const PALETTE: { id: string; hex: string }[] = [
  { id: "blue", hex: "#4f8ff7" },
  { id: "teal", hex: "#14b8a6" },
  { id: "green", hex: "#34a853" },
  { id: "amber", hex: "#e0a526" },
  { id: "orange", hex: "#f2782f" },
  { id: "red", hex: "#e5534b" },
  { id: "rose", hex: "#e3608f" },
  { id: "violet", hex: "#9b6ef3" },
  { id: "slate", hex: "#7d8798" },
  { id: "brown", hex: "#a07a52" },
];
