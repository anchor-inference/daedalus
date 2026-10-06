// The planner: the operator's events and tasks on one calendar, for work and for the rest of life.
//
// Day, week (or the working week), month and agenda on a desktop, with the month to pick dates in,
// the calendars and the tasks beside it; day, three days, month and agenda on a phone, with the side
// column as a drawer, a button to add, and a swipe to the next period. Everything is drawn on the
// clock of the time zone in the calendar's settings.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { locale, t, useLang } from "../i18n";
import { Icon } from "../icons";
import { navigate, pathFor } from "../router";
import { hold, invalidate, peek, prime, release, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { OverflowMenu, Sheet, toast as showToast } from "../ui/dialogs";
import { AgendaView, DayList } from "./Agenda";
import { ConnectionsSheet } from "./Connections";
import { addDays, dayLabel, rangeTitle, startOfWeek, step, visibleRange, weekdayNames, type Day, type View } from "./dates";
import {
  CALENDARS, LISTS, SETTINGS, completeTask, createEvent, createTask, deleteEvent, deleteTask, eventsKey, refreshPlanner, timesOf, tasksRangeKey, updateEvent, updateTask,
  type EventFields,
} from "./data";
import { EventEditor, draftFromOccurrence, fieldsFromDraft, type EventDraft } from "./EventEditor";
import { eventItem, isDone, taskItem } from "./items";
import { MiniMonth } from "./MiniMonth";
import { MonthView } from "./MonthView";
import { QuickCreate, type QuickDraft } from "./QuickCreate";
import { useScopeQuestion } from "./scope";
import { SearchBox, SearchSheet, type SearchHandle } from "./Search";
import { SettingsSheet } from "./SettingsSheet";
import { CalendarsSection } from "./Sidebar";
import { TaskEditor, TasksPanel, type TaskDraft } from "./Tasks";
import { TimeGrid, type Slot } from "./TimeGrid";
import { DEFAULT_SETTINGS, type CalendarRow, type CalendarSettings, type Item, type Occurrence, type Scope, type Task, type TaskList, type TaskView } from "./types";
import { browserZone, fromWall, toWall, todayIn, validZone } from "./zone";
import "./calendar.css";

const VIEW_KEY = "daedalus.calendar.view";
const PHONE_VIEW_KEY = "daedalus.calendar.view.phone";
const WORKWEEK_KEY = "daedalus.calendar.workweek";
const SIDE_KEY = "daedalus.calendar.side";
const TASKS_KEY = "daedalus.calendar.tasks";
const DESK_VIEWS: View[] = ["day", "week", "month", "agenda"];
const PHONE_VIEWS: View[] = ["day", "3day", "month", "agenda"];

function stored(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
function store(key: string, value: string): void {
  try { localStorage.setItem(key, value); } catch { /* private mode: the choice lasts the visit */ }
}

/** A view that exists on this kind of screen: a phone has no room for seven columns and shows three
 *  days where a desktop shows the week, and the other way round. */
function fitView(view: View, phone: boolean): View {
  if (phone) return view === "week" ? "3day" : view;
  return view === "3day" ? "week" : view;
}

type Editor = { kind: "event"; draft: EventDraft } | { kind: "task"; draft: TaskDraft };

export function CalendarScreen({ toast, query }: { toast: (message: string) => void; query?: URLSearchParams }) {
  useLang();
  const shell = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(() => window.innerWidth);
  useEffect(() => {
    const box = shell.current;
    if (!box) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(box);
    return () => observer.disconnect();
  }, []);
  // The layout follows the room the screen has, not the window: the app's own sidebar takes some of it.
  const phone = width < 640;
  const sideInline = width >= 940;

  const settingsQuery = useQuery<CalendarSettings>(SETTINGS, { staleMs: 60000 });
  const settings: CalendarSettings = { ...DEFAULT_SETTINGS, ...(settingsQuery.data ?? {}) };
  const zone = settings.timezone && validZone(settings.timezone) ? settings.timezone : browserZone();

  // The clock: the current-time line and "today" move on their own.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(timer);
  }, []);
  const today = todayIn(zone, now);
  const nowMinutes = toWall(now, zone).minutes;

  const [chosen, setChosen] = useState<View | null>(() => (stored(phone ? PHONE_VIEW_KEY : VIEW_KEY) as View | null) || null);
  const view = fitView(chosen ?? settings.default_view, phone);
  const [workWeek, setWorkWeek] = useState(() => stored(WORKWEEK_KEY) === "1");
  const [anchor, setAnchor] = useState<Day>(() => todayIn(browserZone()));
  const [scrollToken, setScrollToken] = useState(0);
  const [sideOpen, setSideOpen] = useState(() => stored(SIDE_KEY) !== "0");
  const [taskView, setTaskView] = useState<TaskView>(() => (stored(TASKS_KEY) as TaskView | null) || "today");

  const [editor, setEditor] = useState<Editor | null>(null);
  const [quick, setQuick] = useState<{ draft: QuickDraft; anchor: DOMRect | null } | null>(null);
  const [sheet, setSheet] = useState<"settings" | "connections" | "drawer" | "search" | "keys" | null>(null);
  const [busy, setBusy] = useState(false);
  const [dragTask, setDragTask] = useState<Task | null>(null);
  const [scopeDialog, askScope] = useScopeQuestion();
  const search = useRef<SearchHandle>(null);

  const weekends = settings.show_weekends && !workWeek;
  const range = visibleRange(view, anchor, { weekStart: settings.week_start, weekends });
  const eventsPath = eventsKey(range.start, range.end, zone);
  const tasksPath = tasksRangeKey(range.start, range.end, zone);
  const events = useQuery<Occurrence[]>(eventsPath, { pollMs: 30000, staleMs: 5000 });
  const rangeTasks = useQuery<Task[]>(tasksPath, { pollMs: 60000, staleMs: 5000 });
  const calendarsQuery = useQuery<CalendarRow[]>(CALENDARS, { pollMs: 60000, staleMs: 10000 });
  const listsQuery = useQuery<TaskList[]>(LISTS, { staleMs: 60000 });
  const calendars = calendarsQuery.data ?? [];
  const lists = listsQuery.data ?? [];

  // While the next range loads, the last one stays drawn: its items outside the new days are not
  // shown anyway, and the grid does not blink empty on every step.
  const lastEvents = useRef<Occurrence[]>([]);
  const lastTasks = useRef<Task[]>([]);
  if (events.data) lastEvents.current = events.data;
  if (rangeTasks.data) lastTasks.current = rangeTasks.data;

  const items = useMemo(() => {
    const hidden = new Set(calendars.filter((c) => !c.visible).map((c) => c.id));
    const result: Item[] = [];
    for (const o of events.data ?? lastEvents.current) if (!hidden.has(o.calendar_id)) result.push(eventItem(o, zone));
    for (const task of rangeTasks.data ?? lastTasks.current) {
      const list = lists.find((l) => l.id === task.list_id);
      const item = taskItem(task, zone, list && !list.is_default ? list.color : "var(--accent)");
      if (item) result.push(item);
    }
    return result;
  }, [events.data, rangeTasks.data, calendars, lists, zone]);

  const defaultCalendar = calendars.find((c) => c.kind === "local" && c.writable && c.visible) ?? calendars.find((c) => c.writable);
  const defaultList = lists.find((l) => l.is_default) ?? lists[0];

  // ── navigation ────────────────────────────────────────────────────────────────────────────

  const goToday = useCallback(() => {
    setAnchor(today);
    setScrollToken((n) => n + 1);
  }, [today]);
  const go = useCallback((direction: 1 | -1) => setAnchor((a) => step(view, a, direction)), [view]);
  const pickView = useCallback((next: View) => {
    setChosen(next);
    store(phone ? PHONE_VIEW_KEY : VIEW_KEY, next);
    setScrollToken((n) => n + 1);
  }, [phone]);
  const openDay = useCallback((day: Day) => {
    setAnchor(day);
    pickView("day");
  }, [pickView]);
  const toggleWorkWeek = () => {
    store(WORKWEEK_KEY, workWeek ? "0" : "1");
    setWorkWeek(!workWeek);
  };
  const toggleSide = () => {
    if (!sideInline) { setSheet("drawer"); return; }
    store(SIDE_KEY, sideOpen ? "0" : "1");
    setSideOpen(!sideOpen);
  };

  // ── deep links: /app/calendar?event=<id> and ?task=<id> ───────────────────────────────────

  const [pendingEvent, setPendingEvent] = useState<string | null>(null);
  const linkedEvent = query?.get("event") ?? null;
  const linkedTask = query?.get("task") ?? null;
  const linked = query?.get("linked") ?? null;
  useEffect(() => {
    if (!linkedEvent && !linkedTask && !linked) return;
    const clear = () => navigate(pathFor("calendar"), { replace: true });
    if (linked) {
      toast(t("cal.connected"));
      clear();
      return;
    }
    if (linkedEvent) {
      // An occurrence's id carries its start after the series id; a single event's is the event's own.
      const cut = linkedEvent.indexOf(":");
      const eventId = cut > 0 ? linkedEvent.slice(0, cut) : linkedEvent;
      const occurrenceStart = cut > 0 ? linkedEvent.slice(cut + 1) : null;
      const land = (instant: string, allDayDate?: string | null) => {
        setAnchor(allDayDate || toWall(instant, zone).day);
        setPendingEvent(linkedEvent);
        setScrollToken((n) => n + 1);
      };
      if (occurrenceStart && !Number.isNaN(Date.parse(occurrenceStart))) land(occurrenceStart);
      else api.get<Occurrence>(`/api/calendar/events/${encodeURIComponent(eventId)}`).then((found) => land(found.start_at, found.all_day ? (found.start_date || found.start_at.slice(0, 10)) : null), (exc) => toast(errorText(exc)));
      clear();
      return;
    }
    api.get<Task[]>("/api/planner/tasks?view=all").then((tasks) => {
      const task = tasks.find((x) => x.id === linkedTask);
      if (!task) { toast(t("cal.task.missing")); return; }
      const day = task.scheduled_start ? toWall(task.scheduled_start, zone).day : task.due_date;
      if (day) setAnchor(day);
      setScrollToken((n) => n + 1);
      setEditor({ kind: "task", draft: taskDraft(task) });
    }, (exc) => toast(errorText(exc)));
    clear();
  }, [linkedEvent, linkedTask, linked]); // eslint-disable-line react-hooks/exhaustive-deps

  // The linked event opens once the range holding it has been read.
  useEffect(() => {
    if (!pendingEvent || !events.data) return;
    const found = events.data.find((o) => o.id === pendingEvent) ?? events.data.find((o) => o.event_id === pendingEvent);
    if (found) setEditor({ kind: "event", draft: draftFromOccurrence(found, zone, colorOf(found.calendar_id)) });
    else toast(t("cal.event.missing"));
    setPendingEvent(null);
  }, [pendingEvent, events.data]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── creating ───────────────────────────────────────────────────────────────────────────────

  function startQuick(day: Day, start: number, end: number, allDay: boolean, rect: DOMRect | null) {
    if (!defaultCalendar) { toast(t("cal.no.calendar")); return; }
    setQuick({ draft: { kind: "event", title: "", day, start, end, allDay, calendar_id: defaultCalendar.id }, anchor: rect });
  }

  /** A new item at the next half hour of the day in view: what `c` and the phone's add button make. */
  function createHere() {
    const day = view === "month" || view === "agenda" ? (anchor >= range.start && anchor < range.end ? anchor : today) : (range.days.includes(today) ? today : range.days[0]);
    const base = day === today ? Math.min(1440 - settings.default_duration, Math.ceil((nowMinutes + 1) / 30) * 30) : parseClockSafe(settings.work_start);
    startQuick(day, base, Math.min(1440, base + settings.default_duration), false, null);
  }

  function reminderDefaults(calendarId: string): number[] {
    const calendar = calendars.find((c) => c.id === calendarId);
    return calendar?.default_reminders?.length ? calendar.default_reminders : settings.default_reminders;
  }

  async function saveQuick(q: QuickDraft) {
    setBusy(true);
    try {
      if (q.kind === "task") {
        const blocked = !q.allDay;
        await createTask({
          title: q.title.trim(), list_id: defaultList?.id, due_date: q.day, notes: "", priority: 0, reminders: [],
          scheduled_start: blocked ? fromWall(q.day, q.start, zone) : null, scheduled_end: blocked ? fromWall(q.day, q.end, zone) : null, duration: blocked ? q.end - q.start : null,
        });
        toast(t("cal.task.created"));
      } else {
        const fields: EventFields = {
          calendar_id: q.calendar_id, title: q.title.trim(), description: "", location: "", timezone: zone, recurrence: "", reminders: reminderDefaults(q.calendar_id), color: null,
          ...(q.allDay ? { all_day: true, start_at: `${q.day}T00:00:00Z`, end_at: `${addDays(q.day, 1)}T00:00:00Z`, start_date: q.day, end_date: addDays(q.day, 1) } : { all_day: false, start_at: fromWall(q.day, q.start, zone), end_at: fromWall(q.day, q.end, zone), start_date: null, end_date: null }),
        };
        const made = await createEvent(fields);
        showToast(t("cal.event.created"), { undo: made?.event_id ? () => { void deleteEvent(made, "all").then(refreshPlanner, (exc) => toast(errorText(exc))); } : undefined });
      }
      setQuick(null);
      refreshPlanner();
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  function moreQuick(q: QuickDraft) {
    setQuick(null);
    if (q.kind === "task") {
      setEditor({ kind: "task", draft: { title: q.title, notes: "", list_id: defaultList?.id ?? "", due_date: q.day, due_time: null, priority: 0, reminders: [], scheduled_start: q.allDay ? null : fromWall(q.day, q.start, zone), scheduled_end: q.allDay ? null : fromWall(q.day, q.end, zone) } });
      return;
    }
    setEditor({ kind: "event", draft: { calendar_id: q.calendar_id, title: q.title, description: "", location: "", allDay: q.allDay, startDay: q.day, startMin: q.allDay ? 540 : q.start, endDay: q.day, endMin: q.allDay ? 600 : q.end, timezone: zone, recurrence: "", reminders: reminderDefaults(q.calendar_id), color: null } });
  }

  function newEvent() {
    if (!defaultCalendar) { toast(t("cal.no.calendar")); return; }
    const day = range.days.includes(today) ? today : anchor;
    const start = day === today ? Math.min(1440 - settings.default_duration, Math.ceil((nowMinutes + 1) / 30) * 30) : parseClockSafe(settings.work_start);
    setEditor({ kind: "event", draft: { calendar_id: defaultCalendar.id, title: "", description: "", location: "", allDay: false, startDay: day, startMin: start, endDay: day, endMin: Math.min(1440, start + settings.default_duration), timezone: zone, recurrence: "", reminders: reminderDefaults(defaultCalendar.id), color: null } });
  }

  // ── editing events ─────────────────────────────────────────────────────────────────────────

  async function saveEvent(draft: EventDraft) {
    const fields = fieldsFromDraft(draft);
    if (!fields) { toast(t("cal.invalid.range")); return; }
    let scope: Scope = "all";
    if (draft.occurrence?.recurring) {
      const answer = await askScope("change");
      if (!answer) return;
      scope = answer;
    }
    setBusy(true);
    try {
      if (draft.occurrence) await updateEvent(draft.occurrence, fields, scope);
      else await createEvent(fields);
      setEditor(null);
      refreshPlanner();
      toast(t("cal.event.saved"));
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  async function removeEvent(occurrence: Occurrence) {
    let scope: Scope = "all";
    if (occurrence.recurring) {
      const answer = await askScope("delete");
      if (!answer) return;
      scope = answer;
    } else if (!(await confirmAsync(t("cal.event.delete.title", { title: occurrence.title || t("cal.untitled") }), { action: t("common.delete") }))) return;
    setBusy(true);
    try {
      await deleteEvent(occurrence, scope);
      setEditor(null);
      refreshPlanner();
      toast(t("cal.event.deleted"));
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  async function resolveConflict(occurrence: Occurrence, choice: "local" | "remote") {
    setBusy(true);
    try {
      await api.post(`/api/calendar/events/${encodeURIComponent(occurrence.event_id)}/resolve`, { choice });
      setEditor(null);
      refreshPlanner();
      invalidate("/api/calendar/accounts");
      toast(t("cal.conflict.resolved"));
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  /** A drag or a resize: drawn at once, sent after, and offered back with Undo. */
  async function moveItem(item: Item, startDay: Day, startMin: number, endDay: Day, endMin: number) {
    const startAt = fromWall(startDay, startMin, zone);
    const endAt = fromWall(endDay, endMin, zone);
    if (item.kind === "task" && item.task) {
      const task = item.task;
      await changeTask(task, { scheduled_start: startAt, scheduled_end: endAt, duration: Math.round((Date.parse(endAt) - Date.parse(startAt)) / 60000) }, t("cal.task.moved"));
      return;
    }
    const occurrence = item.event;
    if (!occurrence) return;
    let scope: Scope = "all";
    if (occurrence.recurring) {
      const answer = await askScope("change");
      if (!answer) return;
      scope = answer;
    }
    const shiftStart = Date.parse(startAt) - Date.parse(occurrence.start_at);
    const shiftEnd = Date.parse(endAt) - Date.parse(occurrence.end_at);
    const before = peek<Occurrence[]>(eventsPath);
    if (before) {
      prime(eventsPath, before.map((o) => (o.id === occurrence.id || (scope === "all" && o.event_id === occurrence.event_id)
        ? { ...o, start_at: new Date(Date.parse(o.start_at) + shiftStart).toISOString(), end_at: new Date(Date.parse(o.end_at) + shiftEnd).toISOString() }
        : o)));
    }
    hold(eventsPath);
    try {
      const answer = await updateEvent(occurrence, { ...timesOf(occurrence), start_at: startAt, end_at: endAt }, scope);
      // Moved as a series, the occurrence is now known by its new start; moved alone, it keeps its original one.
      const moved: Occurrence = { ...occurrence, start_at: startAt, end_at: endAt, occurrence_start: scope === "all" && occurrence.occurrence_start ? startAt : occurrence.occurrence_start, version: answer?.version ?? occurrence.version + 1 };
      showToast(t("cal.event.moved"), {
        undo: () => {
          void updateEvent(moved, timesOf(occurrence), scope).then(refreshPlanner, (exc) => { toast(errorText(exc)); refreshPlanner(); });
        },
      });
    } catch (exc) {
      if (before) prime(eventsPath, before);
      toast(errorText(exc));
    } finally {
      release(eventsPath);
      refreshPlanner();
    }
  }

  // ── tasks ──────────────────────────────────────────────────────────────────────────────────

  function taskDraft(task: Task): TaskDraft {
    return { task, title: task.title, notes: task.notes ?? "", list_id: task.list_id, due_date: task.due_date, due_time: task.due_time, priority: task.priority, reminders: task.reminders ?? [], scheduled_start: task.scheduled_start, scheduled_end: task.scheduled_end };
  }

  /** Rewrites a task in every list on screen that holds it, so the change shows before the host answers. */
  function patchTaskEverywhere(id: string, patch: Partial<Task>): () => void {
    const keys = [tasksPath, ...(["inbox", "today", "upcoming", "overdue"] as const).map((v) => `/api/planner/tasks?view=${v}`)];
    const saved: [string, Task[] | undefined][] = keys.map((key) => {
      return [key, peek(key)];
    });
    for (const [key, list] of saved) if (list) prime(key, list.map((x) => (x.id === id ? { ...x, ...patch } : x)));
    return () => { for (const [key, list] of saved) if (list) prime(key, list); };
  }

  async function changeTask(task: Task, patch: Partial<Task>, message: string) {
    const undoLocal = patchTaskEverywhere(task.id, patch);
    hold(tasksPath);
    try {
      const saved = await updateTask(task, patch);
      const previous: Partial<Task> = Object.fromEntries(Object.keys(patch).map((k) => [k, task[k as keyof Task]]));
      showToast(message, { undo: () => { void updateTask(saved ?? { ...task, ...patch, version: task.version + 1 }, previous).then(refreshPlanner, (exc) => toast(errorText(exc))); } });
    } catch (exc) {
      undoLocal();
      toast(errorText(exc));
    } finally {
      release(tasksPath);
      refreshPlanner();
    }
  }

  async function toggleTask(task: Task) {
    const done = !isDone(task);
    const undoLocal = patchTaskEverywhere(task.id, { done, done_at: done ? new Date().toISOString() : null });
    try {
      await completeTask(task, done);
      if (done) showToast(t("cal.task.done"), { undo: () => { void completeTask(task, false).then(refreshPlanner, (exc) => toast(errorText(exc))); } });
    } catch (exc) {
      undoLocal();
      toast(errorText(exc));
    }
    refreshPlanner();
  }

  async function dropTask(task: Task, day: Day, minutes: number | null) {
    setDragTask(null);
    if (minutes === null) {
      await changeTask(task, { due_date: day, scheduled_start: null, scheduled_end: null }, t("cal.task.dated", { day: dayLabel(day, locale(), { day: "numeric", month: "short" }) }));
      return;
    }
    const length = task.duration || 30;
    await changeTask(task, { scheduled_start: fromWall(day, minutes, zone), scheduled_end: fromWall(day, Math.min(1440, minutes + length), zone), duration: length, due_date: task.due_date ?? day }, t("cal.task.scheduled"));
  }

  async function addTask(title: string, tab: TaskView) {
    const due = tab === "today" || tab === "overdue" ? today : tab === "upcoming" ? addDays(today, 1) : null;
    try {
      await createTask({ title, list_id: defaultList?.id, due_date: due, notes: "", priority: 0, reminders: [] });
      refreshPlanner();
    } catch (exc) { toast(errorText(exc)); throw exc; }
  }

  async function saveTask(draft: TaskDraft) {
    setBusy(true);
    const fields = { title: draft.title.trim(), notes: draft.notes, list_id: draft.list_id || defaultList?.id, due_date: draft.due_date, due_time: draft.due_date ? draft.due_time : null, priority: draft.priority, reminders: draft.reminders, scheduled_start: draft.scheduled_start, scheduled_end: draft.scheduled_end };
    try {
      if (draft.task) await updateTask(draft.task, fields);
      else await createTask(fields);
      setEditor(null);
      refreshPlanner();
      toast(t("cal.task.saved"));
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  async function removeTask(task: Task) {
    if (!(await confirmAsync(t("cal.task.delete.title", { title: task.title }), { action: t("common.delete") }))) return;
    try {
      await deleteTask(task);
      setEditor(null);
      refreshPlanner();
      toast(t("cal.task.deleted"));
    } catch (exc) { toast(errorText(exc)); }
  }

  function openItem(item: Item) {
    if (item.event) setEditor({ kind: "event", draft: draftFromOccurrence(item.event, zone, colorOf(item.event.calendar_id)) });
    else if (item.task) setEditor({ kind: "task", draft: taskDraft(item.task) });
  }

  async function openFound(found: Occurrence) {
    const day = found.all_day ? (found.start_date || found.start_at.slice(0, 10)) : toWall(found.start_at, zone).day;
    setAnchor(day);
    setScrollToken((n) => n + 1);
    setEditor({ kind: "event", draft: draftFromOccurrence(found, zone, colorOf(found.calendar_id)) });
  }

  function colorOf(calendarId: string): string | undefined {
    return calendars.find((c) => c.id === calendarId)?.color;
  }

  // ── keyboard ───────────────────────────────────────────────────────────────────────────────

  const lastKey = useRef("");
  const lastKeyAt = useRef(0);
  const keys = useRef<(e: KeyboardEvent) => void>(() => undefined);
  keys.current = (e: KeyboardEvent) => {
    const target = e.target as HTMLElement | null;
    const typing = !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable);
    if (typing || e.metaKey || e.ctrlKey || e.altKey || e.defaultPrevented) return;
    // A sheet, a menu or the quick card has the keyboard while it is open.
    if (document.querySelector(".sheet-backdrop, .cal-quick, .menu, .navmenu")) return;
    const act: Record<string, () => void> = {
      t: goToday,
      d: () => pickView("day"),
      w: () => pickView(phone ? "3day" : "week"),
      m: () => pickView("month"),
      a: () => pickView("agenda"),
      j: () => go(1), n: () => go(1), ArrowRight: () => go(1),
      k: () => go(-1), p: () => go(-1), ArrowLeft: () => go(-1),
      c: createHere,
      "/": () => (phone ? setSheet("search") : search.current?.focus()),
      "?": () => setSheet("keys"),
    };
    // `g` then a letter is the app's own jump to another screen; that letter is not ours.
    if (lastKey.current === "g" && Date.now() - lastKeyAt.current < 1200) { lastKey.current = ""; return; }
    lastKey.current = e.key;
    lastKeyAt.current = Date.now();
    const run = act[e.key];
    if (!run) return;
    e.preventDefault();
    run();
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => keys.current(e);
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // ── phone swipes ───────────────────────────────────────────────────────────────────────────

  const touch = useRef<{ x: number; y: number; at: number } | null>(null);
  const onTouchStart = (e: React.TouchEvent) => {
    const point = e.touches[0];
    touch.current = e.touches.length === 1 ? { x: point.clientX, y: point.clientY, at: Date.now() } : null;
  };
  const onTouchEnd = (e: React.TouchEvent) => {
    const start = touch.current;
    touch.current = null;
    if (!start) return;
    const point = e.changedTouches[0];
    const dx = point.clientX - start.x;
    const dy = point.clientY - start.y;
    // A deliberate sideways swipe, not the sideways part of a scroll.
    if (Math.abs(dx) > 64 && Math.abs(dx) > 1.6 * Math.abs(dy) && Date.now() - start.at < 700) go(dx < 0 ? 1 : -1);
  };

  // ── drawing ────────────────────────────────────────────────────────────────────────────────

  // Capitalised here rather than with ::first-letter: Russian dates start lower case ("вт, 6 октября"),
  // and the wider capital drawn by the pseudo-element was not counted in the title's width, so the
  // title lost its last characters to an ellipsis with room to spare.
  const shown = rangeTitle(view, range, anchor, locale(), phone, today);
  const title = shown.charAt(0).toLocaleUpperCase(locale()) + shown.slice(1);
  const busyDays = useMemo(() => {
    const days = new Set<Day>();
    for (const item of items) for (let d = item.firstDay; d <= item.lastDay && days.size < 400; d = addDays(d, 1)) days.add(d);
    return days;
  }, [items]);
  const loadError = events.error && !events.data ? events.error : null;
  const loading = events.loading || (!events.data && !loadError);
  const viewLabel = (v: View) => t(`cal.view.${v}`);
  const work = { start: parseClockSafe(settings.work_start), end: parseClockSafe(settings.work_end, 1080) };

  const side = (
    <>
      <MiniMonth anchor={anchor} today={today} weekStart={settings.week_start} rangeStart={range.start} rangeEnd={range.end} busy={busyDays} onPick={(day) => { setAnchor(day); if (sheet === "drawer") setSheet(null); }} />
      <CalendarsSection calendars={calendars} onConnections={() => setSheet("connections")} toast={toast} />
      <TasksPanel today={today} zone={zone} lists={lists} view={taskView} onView={(v) => { setTaskView(v); store(TASKS_KEY, v); }} onAdd={addTask} onToggle={(task) => void toggleTask(task)}
        onOpen={(task) => { setSheet(null); setEditor({ kind: "task", draft: taskDraft(task) }); }} onDragTask={setDragTask} draggable={!phone && sheet !== "drawer"} />
    </>
  );

  const grid = (view === "day" || view === "3day" || view === "week") ? (
    <TimeGrid days={range.days} items={items} zone={zone} today={today} now={now} nowMinutes={nowMinutes} workStart={work.start} workEnd={work.end}
      hourHeight={phone ? 52 : 48} phone={phone} scrollToken={scrollToken + (view === "day" ? 1000 : view === "3day" ? 2000 : 3000)} dragTask={dragTask} defaultDuration={settings.default_duration}
      selection={quick && !quick.draft.allDay ? { day: quick.draft.day, start: quick.draft.start, end: quick.draft.end } : null}
      onSelect={(slot: Slot, rect) => startQuick(slot.day, slot.start, slot.end, false, phone ? null : rect)}
      onAllDay={(day) => startQuick(day, 540, 600, true, null)}
      onOpen={openItem} onMove={(item, sd, sm, ed, em) => void moveItem(item, sd, sm, ed, em)} onToggleTask={(task) => void toggleTask(task)}
      onDropTask={(task, day, minutes) => void dropTask(task, day, minutes)} onPickDay={(day) => (view === "day" ? setAnchor(day) : openDay(day))} />
  ) : view === "month" ? (
    phone ? (
      <div className="cal-month-phone">
        <MonthView weeks={chunk(range.days)} month={anchor.slice(0, 7)} items={items} zone={zone} today={today} weekStart={settings.week_start} compact selected={anchor} dragTask={null}
          onPickDay={setAnchor} onMore={openDay} onCreate={(day) => startQuick(day, 540, 600, false, null)} onOpen={openItem} onToggleTask={(task) => void toggleTask(task)} onDropTask={() => undefined} />
        <div className="cal-month-day">
          <h2 className="cal-month-day-title">{dayLabel(anchor, locale(), { weekday: "long", day: "numeric", month: "long" })}</h2>
          <DayList day={anchor} items={items} zone={zone} today={today} now={now} calendars={calendars} onOpen={openItem} onToggleTask={(task) => void toggleTask(task)} />
        </div>
      </div>
    ) : (
      <MonthView weeks={chunk(range.days)} month={anchor.slice(0, 7)} items={items} zone={zone} today={today} weekStart={settings.week_start} compact={false} selected={anchor} dragTask={dragTask}
        onPickDay={openDay} onMore={openDay} onCreate={(day) => startQuick(day, parseClockSafe(settings.work_start), parseClockSafe(settings.work_start) + settings.default_duration, false, null)}
        onOpen={openItem} onToggleTask={(task) => void toggleTask(task)} onDropTask={(task, day) => void dropTask(task, day, null)} />
    )
  ) : (
    <AgendaView days={range.days} items={items} zone={zone} today={today} now={now} calendars={calendars} onOpen={openItem} onToggleTask={(task) => void toggleTask(task)} onCreate={newEvent} />
  );

  return (
    <div ref={shell} className={`screen cal-screen ${phone ? "phone" : "desk"}`}>
      <header className="cal-head">
        <button type="button" className="iconbtn" onClick={toggleSide} aria-label={sideInline ? (sideOpen ? t("cal.side.hide") : t("cal.side.show")) : t("cal.side.open")} title={sideInline ? (sideOpen ? t("cal.side.hide") : t("cal.side.show")) : t("cal.side.open")} aria-expanded={sideInline ? sideOpen : sheet === "drawer"}>
          <Icon name={sideInline ? "sidebar" : "menu"} />
        </button>
        {!phone && <button type="button" className="btn cal-today" onClick={goToday} title={`${t("cal.today")} (T)`}>{t("cal.today")}</button>}
        {!phone && (
          <div className="cal-nav">
            <button type="button" className="iconbtn" onClick={() => go(-1)} aria-label={t(`cal.previous.${view}`)} title={`${t(`cal.previous.${view}`)} (K)`}><Icon name="back" /></button>
            <button type="button" className="iconbtn" onClick={() => go(1)} aria-label={t(`cal.next.${view}`)} title={`${t(`cal.next.${view}`)} (J)`}><Icon name="forward" /></button>
          </div>
        )}
        <h1 className="cal-title" aria-live="polite">{title}</h1>
        {!phone && <SearchBox ref={search} zone={zone} onPick={(o) => void openFound(o)} />}
        {!phone && (
          <div className="segmented inline cal-views" role="radiogroup" aria-label={t("cal.view")}>
            {DESK_VIEWS.map((v) => (
              <button key={v} type="button" role="radio" aria-checked={view === v} className={view === v ? "on" : ""} onClick={() => pickView(v)} title={`${viewLabel(v)} (${v[0].toUpperCase()})`}>
                {v === "week" && workWeek ? t("cal.view.workweek") : viewLabel(v)}
              </button>
            ))}
          </div>
        )}
        {phone && (
          <>
            <button type="button" className="iconbtn" onClick={() => setSheet("search")} aria-label={t("cal.search")} title={t("cal.search")}><Icon name="search" /></button>
            <OverflowMenu label={t("cal.view")} className="cal-view-pick" trigger={<span className="cal-view-current">{viewLabel(view)}<Icon name="chevron" size={14} /></span>}
              items={PHONE_VIEWS.map((v) => ({ label: viewLabel(v), icon: v === view ? "check" as const : undefined, onSelect: () => pickView(v) }))} />
            <button type="button" className="iconbtn cal-today-icon" onClick={goToday} aria-label={t("cal.today")} title={t("cal.today")}>
              <span className="cal-today-glyph" aria-hidden="true">{Number(today.slice(8, 10))}</span>
            </button>
          </>
        )}
        {!phone && <button type="button" className="iconbtn" onClick={() => setSheet("settings")} aria-label={t("cal.settings")} title={t("cal.settings")}><Icon name="settings" /></button>}
        <OverflowMenu label={t("cal.menu")} items={[
          ...(!phone ? [{ label: t("cal.view.workweek.toggle"), checked: workWeek, onSelect: toggleWorkWeek }, "-" as const] : []),
          ...(phone ? [{ label: t("cal.settings"), icon: "settings" as const, onSelect: () => setSheet("settings") }] : []),
          { label: t("cal.connections"), icon: "plug", onSelect: () => setSheet("connections") },
          ...(!phone ? [{ label: t("cal.keys"), icon: "question" as const, hint: "?", onSelect: () => setSheet("keys") }] : []),
        ]} />
        {!phone && <button type="button" className="btn primary cal-new" onClick={newEvent} title={`${t("cal.new")} (C)`}><Icon name="plus" size={16} /> {t("cal.new.short")}</button>}
      </header>
      {phone && (view === "day" || view === "3day") && (
        <WeekStrip anchor={anchor} today={today} weekStart={settings.week_start} span={view === "3day" ? 3 : 1} busy={busyDays} onPick={setAnchor} />
      )}
      <div className="cal-body">
        {sideInline && sideOpen && <aside className="cal-side" aria-label={t("cal.side")}>{side}</aside>}
        <main className={`cal-main view-${view}`} onTouchStart={phone ? onTouchStart : undefined} onTouchEnd={phone ? onTouchEnd : undefined} aria-busy={loading}>
          {loading && <div className="cal-loading" role="progressbar" aria-label={t("common.loading")} />}
          {loadError && (
            <div className="cal-banner" role="alert">
              <Icon name="alert" size={16} /><span>{t("cal.load.failed", { error: loadError })}</span>
              <button type="button" className="btn small" onClick={() => void events.refresh()}>{t("cal.retry")}</button>
            </div>
          )}
          {grid}
        </main>
      </div>
      {phone && (
        <button type="button" className="cal-fab" onClick={createHere} aria-label={t("cal.new")} title={t("cal.new")}><Icon name="plus" size={24} /></button>
      )}
      {sheet === "drawer" && <Sheet title={t("cal.side")} onClose={() => setSheet(null)} className="cal-drawer"><div className="cal-side in-drawer">{side}</div></Sheet>}
      {sheet === "settings" && <SettingsSheet settings={settings} zone={zone} onClose={() => setSheet(null)} toast={toast} />}
      {sheet === "connections" && <ConnectionsSheet onClose={() => setSheet(null)} toast={toast} />}
      {sheet === "search" && <SearchSheet zone={zone} onPick={(o) => void openFound(o)} onClose={() => setSheet(null)} />}
      {sheet === "keys" && <KeysSheet onClose={() => setSheet(null)} />}
      {quick && <QuickCreate draft={quick.draft} calendars={calendars} anchor={quick.anchor} phone={phone} busy={busy} onChange={(draft) => setQuick({ ...quick, draft })} onSave={(q) => void saveQuick(q)} onMore={moreQuick} onClose={() => setQuick(null)} />}
      {editor?.kind === "event" && <EventEditor key={editor.draft.occurrence?.id ?? "new"} draft={editor.draft} calendars={calendars} phone={phone} busy={busy} onClose={() => setEditor(null)} onSave={(d) => void saveEvent(d)} onDelete={(o) => void removeEvent(o)} onResolve={(o, c) => void resolveConflict(o, c)} />}
      {editor?.kind === "task" && <TaskEditor key={editor.draft.task?.id ?? "new"} draft={editor.draft} lists={lists} zone={zone} today={today} busy={busy} onClose={() => setEditor(null)} onSave={(d) => void saveTask(d)} onDelete={(task) => void removeTask(task)}
        onToggle={(task) => { void toggleTask(task); setEditor(null); }} />}
      {scopeDialog}
    </div>
  );
}

function parseClockSafe(text: string, fallback = 540): number {
  const match = /^(\d{1,2}):(\d{2})$/.exec(text ?? "");
  return match ? Number(match[1]) * 60 + Number(match[2]) : fallback;
}

function chunk(days: Day[]): Day[][] {
  const weeks: Day[][] = [];
  for (let i = 0; i < days.length; i += 7) weeks.push(days.slice(i, i + 7));
  return weeks;
}

/** The week around the day in view, on a phone above the day and three-day views: where you are in
 *  the week, which days have something on, and one tap to another day. */
function WeekStrip({ anchor, today, weekStart, span, busy, onPick }: { anchor: Day; today: Day; weekStart: number; span: number; busy: Set<Day>; onPick: (day: Day) => void }) {
  const first = startOfWeek(anchor, weekStart);
  const days = Array.from({ length: 7 }, (_, i) => addDays(first, i));
  const names = weekdayNames(locale(), weekStart, "narrow");
  const shown = new Set(Array.from({ length: span }, (_, i) => addDays(anchor, i)));
  return (
    <div className="cal-strip" role="group" aria-label={t("cal.picker")}>
      {days.map((day, i) => (
        <button key={day} type="button" className={`cal-strip-day ${day === today ? "today" : ""} ${day === anchor ? "anchor" : ""} ${shown.has(day) ? "shown" : ""}`} onClick={() => onPick(day)}
          aria-label={dayLabel(day, locale(), { weekday: "long", day: "numeric", month: "long" })} aria-pressed={day === anchor} aria-current={day === today ? "date" : undefined}>
          <span className="cal-strip-wd">{names[i]}</span>
          <span className="cal-strip-num">{Number(day.slice(8, 10))}</span>
          <span className={`cal-strip-dot ${busy.has(day) ? "on" : ""}`} aria-hidden="true" />
        </button>
      ))}
    </div>
  );
}

function KeysSheet({ onClose }: { onClose: () => void }) {
  const rows: [string, string][] = [["T", "cal.keys.today"], ["J · →", "cal.keys.next"], ["K · ←", "cal.keys.previous"], ["D", "cal.keys.day"], ["W", "cal.keys.week"], ["M", "cal.keys.month"], ["A", "cal.keys.agenda"], ["C", "cal.keys.create"], ["/", "cal.keys.search"], ["Esc", "cal.keys.close"], ["?", "cal.keys.help"]];
  return (
    <Sheet title={t("cal.keys")} onClose={onClose} size="narrow">
      <dl className="cal-keys">
        {rows.map(([key, what]) => <div key={what}><dt><kbd>{key}</kbd></dt><dd>{t(what)}</dd></div>)}
      </dl>
      <p className="cal-hint">{t("cal.keys.drag")}</p>
    </Sheet>
  );
}
