import { describe, expect, it } from "vitest";
import { eventItem, segment, taskItem } from "./items";
import type { Occurrence, Task } from "./types";

function event(over: Partial<Occurrence>): Occurrence {
  return { id: "e1", event_id: "e1", calendar_id: "c", title: "Event", description: "", location: "", start_at: "2026-10-06T07:00:00Z", end_at: "2026-10-06T08:00:00Z", all_day: false, timezone: "UTC", color: "#7db4ff", recurrence: "", recurring: false, occurrence_start: "2026-10-06T07:00:00Z", reminders: [], version: 1, writable: true, pending_sync: false, ...over };
}

describe("events and tasks on the grid", () => {
  it("places a timed event on the planner's own day", () => {
    const item = eventItem(event({ start_at: "2026-10-06T22:00:00Z", end_at: "2026-10-06T23:00:00Z" }), "Europe/Moscow");
    expect(item.firstDay).toBe("2026-10-07");
    expect(item.allDay).toBe(false);
    expect(segment(item, "2026-10-07", "Europe/Moscow")).toMatchObject({ start: 60, end: 120 });
  });

  it("cuts an overnight event into a piece per day", () => {
    const item = eventItem(event({ start_at: "2026-10-06T22:00:00Z", end_at: "2026-10-07T02:00:00Z" }), "UTC");
    expect(segment(item, "2026-10-06", "UTC")).toMatchObject({ start: 1320, end: 1440, continuesAfter: true });
    expect(segment(item, "2026-10-07", "UTC")).toMatchObject({ start: 0, end: 120, continuesBefore: true });
  });

  it("ends an event at midnight on the day it started", () => {
    const item = eventItem(event({ start_at: "2026-10-06T22:00:00Z", end_at: "2026-10-07T00:00:00Z" }), "UTC");
    expect(item.lastDay).toBe("2026-10-06");
    expect(segment(item, "2026-10-06", "UTC")).toMatchObject({ end: 1440, continuesAfter: false });
  });

  it("reads an all-day event's dates with the end exclusive", () => {
    const item = eventItem(event({ all_day: true, start_date: "2026-10-08", end_date: "2026-10-11", start_at: "2026-10-08T00:00:00Z", end_at: "2026-10-11T00:00:00Z" }), "Asia/Tokyo");
    expect([item.firstDay, item.lastDay, item.allDay]).toEqual(["2026-10-08", "2026-10-10", true]);
  });

  it("draws a day-long timed event in the all-day row", () => {
    expect(eventItem(event({ start_at: "2026-10-06T07:00:00Z", end_at: "2026-10-08T07:00:00Z" }), "UTC").allDay).toBe(true);
  });

  it("makes a time-blocked task a block and a dated one an all-day line", () => {
    const task: Task = { id: "t1", list_id: "inbox", title: "Write report", notes: "", due_date: "2026-10-06", due_time: null, scheduled_start: null, scheduled_end: null, duration: null, priority: 2, done_at: null, reminders: [], recurrence: "", position: 0, version: 1 };
    expect(taskItem(task, "UTC", "#fff")).toMatchObject({ allDay: true, firstDay: "2026-10-06" });
    expect(taskItem({ ...task, scheduled_start: "2026-10-06T10:00:00Z", scheduled_end: "2026-10-06T11:30:00Z" }, "UTC", "#fff")).toMatchObject({ allDay: false, kind: "task" });
    expect(taskItem({ ...task, due_date: null }, "UTC", "#fff")).toBeNull();
  });
});
