// Days as `YYYY-MM-DD` strings and the arithmetic of the planner's views. A day here is a date on the
// operator's wall calendar, not an instant: adding one to it is calendar arithmetic done in UTC, where
// no clock ever jumps, so a week across a daylight-saving change still has seven days of 24 hours.

export type Day = string;
export type View = "day" | "3day" | "week" | "month" | "agenda";

/** How many days the agenda lists ahead of the day it starts on. */
export const AGENDA_DAYS = 30;

function asDate(day: Day): Date {
  return new Date(`${day}T00:00:00Z`);
}

function asDay(date: Date): Day {
  return date.toISOString().slice(0, 10);
}

export function addDays(day: Day, n: number): Day {
  const date = asDate(day);
  date.setUTCDate(date.getUTCDate() + n);
  return asDay(date);
}

/** 0 is Sunday, as in JavaScript and in the settings' `week_start`. */
export function weekday(day: Day): number {
  return asDate(day).getUTCDay();
}

export function diffDays(from: Day, to: Day): number {
  return Math.round((asDate(to).getTime() - asDate(from).getTime()) / 86400000);
}

export function startOfWeek(day: Day, weekStart: number): Day {
  return addDays(day, -((weekday(day) - weekStart + 7) % 7));
}

export function monthStart(day: Day): Day {
  return `${day.slice(0, 7)}-01`;
}

/** The same day of a month `n` months away, clamped to that month's length (31 January + 1 is 28 or 29 February). */
export function addMonths(day: Day, n: number): Day {
  const date = asDate(day);
  const target = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + n, 1));
  const length = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
  target.setUTCDate(Math.min(date.getUTCDate(), length));
  return asDay(target);
}

export function isWeekend(day: Day): boolean {
  const n = weekday(day);
  return n === 0 || n === 6;
}

/** The weeks a month view draws: whole weeks from the one holding the 1st to the one holding the last day. */
export function monthWeeks(day: Day, weekStart: number): Day[][] {
  const first = monthStart(day);
  const last = addDays(addMonths(first, 1), -1);
  const weeks: Day[][] = [];
  for (let start = startOfWeek(first, weekStart); start <= last; start = addDays(start, 7)) {
    weeks.push(Array.from({ length: 7 }, (_, i) => addDays(start, i)));
  }
  return weeks;
}

export type Range = { days: Day[]; start: Day; end: Day };

/** The days a view shows around `anchor`. `end` is exclusive. A work week leaves Saturday and Sunday
 *  out of `days` but keeps them inside `start`…`end`, so stepping by a week still moves by seven. */
export function visibleRange(view: View, anchor: Day, opts: { weekStart: number; weekends: boolean }): Range {
  if (view === "day") return { days: [anchor], start: anchor, end: addDays(anchor, 1) };
  if (view === "3day") {
    const days = [anchor, addDays(anchor, 1), addDays(anchor, 2)];
    return { days, start: anchor, end: addDays(anchor, 3) };
  }
  if (view === "week") {
    const start = startOfWeek(anchor, opts.weekStart);
    const all = Array.from({ length: 7 }, (_, i) => addDays(start, i));
    return { days: opts.weekends ? all : all.filter((d) => !isWeekend(d)), start, end: addDays(start, 7) };
  }
  if (view === "month") {
    const weeks = monthWeeks(anchor, opts.weekStart);
    const days = weeks.flat();
    return { days, start: days[0], end: addDays(days[days.length - 1], 1) };
  }
  const days = Array.from({ length: AGENDA_DAYS }, (_, i) => addDays(anchor, i));
  return { days, start: anchor, end: addDays(anchor, AGENDA_DAYS) };
}

/** Where Previous and Next take the anchor: a view's own length, a month for the month view. */
export function step(view: View, anchor: Day, direction: 1 | -1): Day {
  if (view === "day") return addDays(anchor, direction);
  if (view === "3day") return addDays(anchor, 3 * direction);
  if (view === "week") return addDays(anchor, 7 * direction);
  if (view === "month") return addMonths(monthStart(anchor), direction);
  return addDays(anchor, AGENDA_DAYS * direction);
}

/** The heading over the view: "6 – 12 October 2026", "October 2026", "Tuesday, 7 October 2026". */
export function rangeTitle(view: View, range: Range, anchor: Day, locale: string, compact = false, today: Day = anchor): string {
  const at = (day: Day) => asDate(day);
  const utc = { timeZone: "UTC" } as const;
  // A phone's agenda is titled by the month it starts in: the month ahead does not fit a phone's header.
  if (view === "month" || (view === "agenda" && compact)) {
    return new Intl.DateTimeFormat(locale, { ...utc, month: "long", year: compact && anchor.slice(0, 4) === today.slice(0, 4) ? undefined : "numeric" }).format(at(anchor));
  }
  if (view === "day") {
    return new Intl.DateTimeFormat(locale, compact ? { ...utc, day: "numeric", month: "long" } : { ...utc, weekday: "short", day: "numeric", month: "long", year: "numeric" }).format(at(anchor));
  }
  const first = range.days[0];
  const last = range.days[range.days.length - 1];
  // The agenda's month ahead crosses a month boundary almost always; short month names keep it on one line.
  const format = new Intl.DateTimeFormat(locale, compact ? { ...utc, day: "numeric", month: "short" } : { ...utc, day: "numeric", month: view === "agenda" ? "short" : "long", year: "numeric" });
  try {
    return format.formatRange(at(first), at(last));
  } catch {
    return `${format.format(at(first))} – ${format.format(at(last))}`;
  }
}

/** Minutes snapped to the grid's step, 15 by default. */
export function snap(minutes: number, stepMinutes = 15): number {
  return Math.round(minutes / stepMinutes) * stepMinutes;
}

/** `HH:MM` as minutes after midnight; null when it is not a time. */
export function parseClock(text: string): number | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(text.trim());
  if (!match) return null;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  if (hours > 24 || minutes > 59 || (hours === 24 && minutes > 0)) return null;
  return hours * 60 + minutes;
}

export function clock(minutes: number): string {
  const m = ((minutes % 1440) + 1440) % 1440;
  return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
}

/** A clock time the way the reader's language writes it (24-hour in both of ours). */
export function clockLabel(minutes: number, locale: string): string {
  const m = ((minutes % 1440) + 1440) % 1440;
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: "UTC" }).format(new Date(Date.UTC(2000, 0, 1, 0, m)));
}

/** A day the way the reader's language writes it, with the given parts. */
export function dayLabel(day: Day, locale: string, options: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(locale, { ...options, timeZone: "UTC" }).format(asDate(day));
}

/** The short weekday names in week order, from the week's first day. */
export function weekdayNames(locale: string, weekStart: number, width: "short" | "narrow" = "short"): string[] {
  // 4 January 2026 was a Sunday.
  return Array.from({ length: 7 }, (_, i) => dayLabel(addDays("2026-01-04", (weekStart + i) % 7), locale, { weekday: width }));
}

/** A length of time as "1 h 30 min", the parts in the reader's words. */
export function durationLabel(minutes: number, words: { h: string; min: string }): string {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (!hours) return `${rest} ${words.min}`;
  return rest ? `${hours} ${words.h} ${rest} ${words.min}` : `${hours} ${words.h}`;
}
