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

/** The fields an event is written with. An all-day event carries its dates as well as midnight
 *  instants, the end exclusive, as the contract has it. The host changes only the fields sent, so a
 *  move sends its times alone and leaves the reminders and the colour as they were. */
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
  /** The event's own colour, or null for its calendar's. */
  color: string | null;
};

export type EventTimes = Pick<EventFields, "start_at" | "end_at" | "all_day" | "start_date" | "end_date">;

export function timesOf(event: Occurrence): EventTimes {
  return { start_at: event.start_at, end_at: event.end_at, all_day: !!event.all_day, start_date: event.start_date ?? null, end_date: event.end_date ?? null };
}

/** All-day dates as the instants the contract wants beside them. `lastDay` is inclusive. */
export function allDayTimes(firstDay: Day, lastDay: Day): Pick<EventFields, "start_at" | "end_at" | "start_date" | "end_date"> {
  const end = addDays(lastDay, 1);
  return { start_at: `${firstDay}T00:00:00Z`, end_at: `${end}T00:00:00Z`, start_date: firstDay, end_date: end };
}

export function createEvent(fields: EventFields): Promise<Occurrence> {
  return api.post<Occurrence>("/api/calendar/events", fields);
}

/** Writes an occurrence's new fields, always addressed through the occurrence.

    With `scope: "this"` the host writes an override of that one occurrence. With `scope: "all"` and
    the occurrence's start, the host reads the times sent as this occurrence's new times and moves
    the whole series by the same amount: the occurrence being edited is usually not the first one,
    and its date sent as the series' start would have moved the series to it. A series is checked
    against the master's version, which an occurrence changed on its own does not carry, so that one
    asks for it first. */
export async function updateEvent(occurrence: Occurrence, fields: Partial<EventFields>, scope: Scope): Promise<{ version?: number }> {
  let version = occurrence.version;
  if (scope === "all" && occurrence.exception) version = (await api.get<{ version: number }>(`/api/calendar/events/${encodeURIComponent(occurrence.event_id)}`)).version;
  const at = occurrence.occurrence_start ? { occurrence_start: occurrence.occurrence_start } : {};
  return api.put(`/api/calendar/events/${encodeURIComponent(occurrence.event_id)}`, { ...fields, version, scope, ...at });
}

export function deleteEvent(occurrence: Occurrence, scope: Scope): Promise<unknown> {
  const query = new URLSearchParams({ version: String(occurrence.version), scope });
  if (scope === "this" && occurrence.occurrence_start) query.set("occurrence_start", occurrence.occurrence_start);
  return api.delete(`/api/calendar/events/${encodeURIComponent(occurrence.event_id)}?${query}`);
}

export type TaskFields = Partial<Omit<Task, "id" | "version" | "done_at" | "done">>;

export function createTask(fields: TaskFields): Promise<Task> {
  return api.post<Task>("/api/planner/tasks", fields);
}

export function updateTask(task: Task, fields: TaskFields): Promise<Task> {
  const { id: _id, done_at: _doneAt, done: _done, version, ...rest } = task;
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
