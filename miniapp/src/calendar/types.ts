// The shapes the calendar and planner API answer with. The host owns them; these mirror what the
// screen reads, and a field the host adds later is simply not read here.

import type { Day, View } from "./dates";

export type CalendarKind = "local" | "google" | "outlook" | "caldav" | "ics";
export type SyncStatus = "ok" | "error" | "syncing" | "never";

export type CalendarRow = {
  id: string;
  name: string;
  color: string;
  kind: CalendarKind;
  account_id: string | null;
  visible: boolean;
  writable: boolean;
  position: number;
  default_reminders: number[];
  sync: { last_sync_at: string | null; error: string; status: SyncStatus; conflicts?: number; next_sync_at?: string | null; failures?: number } | null;
};

/** One occurrence: a single event, or one instance of a recurring series (`id` is then
 *  `event_id:occurrence_start`). Times are UTC instants; an all-day event also has its dates, the end
 *  exclusive. */
export type Occurrence = {
  id: string;
  event_id: string;
  calendar_id: string;
  calendar_name?: string;
  account_id?: string | null;
  title: string;
  description: string;
  location: string;
  start_at: string;
  end_at: string;
  all_day: boolean;
  start_date?: Day | null;
  end_date?: Day | null;
  timezone: string;
  color: string;
  recurrence: string;
  recurring: boolean;
  /** The occurrence's original start, for one of a series; null for a single event. */
  occurrence_start: string | null;
  /** An occurrence of a series that was changed on its own. */
  exception?: boolean;
  reminders: number[];
  version: number;
  writable: boolean;
  pending_sync: boolean;
  /** Edited here and at the provider before the two synced: the host keeps both until one is chosen,
   *  a structured flag rather than words in an error, so the screen can offer the two versions. */
  conflict?: boolean;
};

export type Scope = "all" | "this";

export type TaskList = { id: string; name: string; color: string; is_default?: boolean; position?: number };

export type Task = {
  id: string;
  list_id: string;
  title: string;
  notes: string;
  due_date: Day | null;
  due_time: string | null;
  scheduled_start: string | null;
  scheduled_end: string | null;
  duration: number | null;
  priority: 0 | 1 | 2 | 3;
  done_at: string | null;
  done?: boolean;
  reminders: number[];
  recurrence: string;
  position: number;
  version: number;
};

export type TaskView = "inbox" | "today" | "upcoming" | "overdue";

export type CalendarSettings = {
  week_start: 0 | 1;
  work_start: string;
  work_end: string;
  default_view: Exclude<View, "3day">;
  default_duration: number;
  default_reminders: number[];
  timezone: string;
  show_weekends: boolean;
};

export const DEFAULT_SETTINGS: CalendarSettings = { week_start: 1, work_start: "09:00", work_end: "18:00", default_view: "week", default_duration: 60, default_reminders: [10], timezone: "", show_weekends: true };

export type Provider = "google" | "outlook" | "yandex" | "icloud" | "caldav" | "ics";

export type Account = {
  id: string;
  provider: "google" | "outlook" | "caldav" | "ics";
  name: string;
  remote_calendar_id?: string;
  /** A CalDAV account's server and login; for a subscription only the feed's host, since its
   *  address often carries a private token. */
  server_url?: string;
  username?: string;
  host?: string;
  calendar_id?: string | null;
  last_sync_at: string | null;
  sync_error: string;
  status?: SyncStatus;
  /** How many of its events wait for a conflict to be resolved. */
  conflicts?: number;
  failures?: number;
  next_sync_at?: string | null;
};

/** What the grid draws, an event or a task, reduced to what placing it needs. */
export type Item = {
  key: string;
  kind: "event" | "task";
  title: string;
  color: string;
  /** Instants, for a timed item. */
  start: number;
  end: number;
  allDay: boolean;
  /** Days, for an all-day item: `lastDay` inclusive. */
  firstDay: Day;
  lastDay: Day;
  event?: Occurrence;
  task?: Task;
  done?: boolean;
  location?: string;
};
