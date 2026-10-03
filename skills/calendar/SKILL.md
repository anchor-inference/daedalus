---
name: calendar
description: Read, create, move or cancel real calendar events with CalendarEvents, CalendarCreate, CalendarUpdate and CalendarDelete.
---
# Calendar

- Use `CalendarEvents` before changing or deleting an event. Its `id` and `version` identify the exact event and revision; pass both to edits so a simultaneous change is not overwritten.
- Give `start_at` and `end_at` with an explicit UTC offset, and name an IANA `timezone` for how the event should be shown. For all-day events, use midnight at each boundary; the end is exclusive.
- `CalendarAccounts` lists connected calendars. Omit `account_id` to keep an event in Daedalus only. Give an account id when the event should sync to that provider.
- Calendar events and scheduled agent tasks are different: use `ScheduleCreate` to make the agent run later; use `CalendarCreate` for an appointment the operator sees on a calendar.
- A connected event may show `dirty` until synchronization succeeds. If the provider reports a conflict, tell the operator and read the event again before proposing a resolution.
