// The planner's phrases that are built from numbers and dates: a reminder's lead time, a day called
// Today or Tomorrow, a rule in words. Every word comes from the translation table.

import { locale, plural, t } from "../i18n";
import { addDays, clockLabel, dayLabel, durationLabel, type Day } from "./dates";
import { describeRule, type ByDay, type Freq } from "./rrule";
import type { Item } from "./types";
import { toWall } from "./zone";

export const REMINDER_PRESETS = [0, 5, 10, 15, 30, 60, 120, 1440, 2880, 10080];

export function reminderLabel(minutes: number): string {
  if (minutes === 0) return t("cal.reminder.at");
  if (minutes % 10080 === 0) return plural("cal.reminder.weeks", minutes / 10080);
  if (minutes % 1440 === 0) return plural("cal.reminder.days", minutes / 1440);
  if (minutes % 60 === 0) return plural("cal.reminder.hours", minutes / 60);
  return plural("cal.reminder.minutes", minutes);
}

export function duration(minutes: number): string {
  return durationLabel(minutes, { h: t("cal.unit.h"), min: t("cal.unit.min") });
}

/** "Today", "Tomorrow", "Yesterday" or the date with its weekday. */
export function relativeDay(day: Day, today: Day, long = true): string {
  if (day === today) return t("cal.today");
  if (day === addDays(today, 1)) return t("cal.tomorrow");
  if (day === addDays(today, -1)) return t("cal.yesterday");
  return dayLabel(day, locale(), long ? { weekday: "long", day: "numeric", month: "long" } : { weekday: "short", day: "numeric", month: "short" });
}

/** An item's time as a list shows it: "09:00 – 10:30", "All day", or the due time of a task. */
export function itemTime(item: Item, zone: string, day?: Day): string {
  if (item.kind === "task" && item.allDay) return item.task?.due_time ? clockLabel(Number(item.task.due_time.slice(0, 2)) * 60 + Number(item.task.due_time.slice(3, 5)), locale()) : t("cal.due");
  if (item.allDay) return t("cal.allday");
  const from = toWall(item.start, zone);
  const to = toWall(item.end, zone);
  const start = day && from.day < day ? "…" : clockLabel(from.minutes, locale());
  const end = day && to.day > day && !(to.minutes === 0 && addDays(day, 1) === to.day) ? "…" : clockLabel(to.minutes, locale());
  return `${start} – ${end}`;
}

export function ruleWords() {
  return {
    every: (n: number, unit: Freq) => plural(`cal.every.${unit.toLowerCase()}`, n),
    on: (days: string) => t("cal.rule.on", { days }),
    until: (day: string) => t("cal.rule.until", { day }),
    times: (n: number) => plural("cal.rule.times", n),
    dayName: (code: ByDay) => t(`cal.weekday.${code.toLowerCase()}`),
  };
}

export function ruleText(rule: string): string {
  return describeRule(rule, ruleWords(), (day) => dayLabel(day, locale(), { day: "numeric", month: "short", year: "numeric" }));
}

/** A due date relative to today, for a task row: "Today", "Overdue · 3 Oct", "Fri 9 Oct". */
export function dueLabel(due: Day, today: Day): string {
  if (due < today) return t("cal.task.overdue.since", { day: dayLabel(due, locale(), { day: "numeric", month: "short" }) });
  return relativeDay(due, today, false);
}
