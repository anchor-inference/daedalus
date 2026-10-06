---
name: calendar
description: Plan the operator's time - read, create, move or cancel calendar events (one-off and recurring), find free time, and keep their personal to-do tasks with CalendarEvents, CalendarFindTime, CalendarCreate, CalendarUpdate, CalendarDelete, CalendarList and the Planner tools.
---
# Calendar and planner

## Which tool for which request

- **An appointment the operator will attend** (a meeting, a doctor's visit, a flight, a birthday): a calendar
  event, `CalendarCreate`. It has a start and an end and shows on the calendar.
- **Something the operator has to do** ("buy a gift", "send the report by Friday"): a planner task,
  `PlannerTaskCreate`. Give it a `due_date` (and `due_time` if it matters). When they want to set time aside
  for it, also give `scheduled_start`/`scheduled_end`: that blocks the time on their calendar.
- **Something *you* must do later** ("check the deploy tomorrow at 9", "every Monday send me a summary"):
  that is not the calendar at all. Use `ScheduleCreate`, which runs you at that time.
- "Remind me to…" is a task with a reminder when it is the operator's own to-do, and `ScheduleCreate` when
  you are the one who has to act. Ask if it is unclear.
- These planner tasks are the operator's own list. The Board (`BoardAdd`) is your plan of work in a session;
  never put the operator's errands there.

## Reading

- `CalendarEvents` returns occurrences: a weekly meeting appears once per week, each with its own `id`,
  its series' `event_id`, its `occurrence_start` and the calendar's name. With `query` it searches instead.
- `CalendarList` gives the calendars (ids, which are writable, which are read-only subscriptions), their
  sync status, and the settings: the operator's time zone and working hours.
- `PlannerTasks` with `view` = `today`, `overdue`, `upcoming`, `inbox`, `done` or `all`.
- Before saying "you are free at…", call `CalendarFindTime`. Never infer free time from memory or from a
  partial read; it looks across every visible calendar and every time-blocked task.

## Dates, times and zones

- Times you send are ISO 8601 with an explicit offset. Name the IANA `timezone` the event lives in;
  without one the operator's calendar zone is used. A recurring event repeats on that zone's wall clock,
  so "every Monday at 10:00" stays at 10:00 across daylight-saving changes.
- Resolve relative dates ("tomorrow", "next Friday", "in the evening") in the operator's time zone from
  `CalendarList`, not in UTC. When a date or time is genuinely ambiguous ("next weekend", "after lunch",
  a time with no zone for a call with someone abroad), ask; when you choose, state the assumption in your
  reply ("I put it at 19:00 Moscow time").
- All-day events take the dates' midnights; the end is exclusive (a one-day event on 5 October ends on 6 October).

## Changing and deleting

- Read first, then pass the event's `event_id` and `version`; a stale version is refused so that you never
  overwrite a change made meanwhile. Read again and retry rather than guessing.
- For a recurring event, decide the scope and confirm it with the operator when they did not say:
  `scope="this"` with that occurrence's `occurrence_start` changes or removes one occurrence;
  `scope="all"` changes the whole series. Moving "the meeting on Thursday" means `this`.
- Repeat rules are RRULEs: `FREQ=WEEKLY;BYDAY=MO,WE`, `FREQ=MONTHLY;BYMONTHDAY=1`, `FREQ=DAILY;COUNT=10`.
- Subscriptions are read-only; create events in a writable calendar. An event in a connected calendar
  shows `pending_sync` until its provider confirms it. When it shows `conflict`, the provider changed it
  too: tell the operator what differs and let them choose in the app; do not overwrite it yourself.
- Completing a repeating task with `PlannerTaskComplete` records it as done and moves it to the next date.
