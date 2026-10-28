// @vitest-environment jsdom
// Where a schedule is listed, which conversation it belongs to, how its last run went and what a
// filter keeps: the answers the Schedules screen and the dialog over a chat both draw from.

import { describe, expect, it } from "vitest";
import { conversationOf, filterItems, groupItems, itemsFrom, lastRun, scheduleGroup, type ScheduleProposal, type ScheduleRow } from "./model";
import { parseLocalCron } from "./ScheduleForm";

const base: ScheduleRow = {
  id: "s1", name: "Morning digest", cron: "10 2 * * *", run_at: null, prompt: "Collect the overnight mail", enabled: 1,
  next_run_at: "2026-10-29T02:10:00Z", last_run_at: null, last_summary: null, kind: "agent", target_session: null,
  created_by_session: null, active_session_id: null, failure_count: 0, last_error: null, authority_state: "current",
  schedule_revision: 3, run_in: "new",
};
const row = (over: Partial<ScheduleRow>): ScheduleRow => ({ ...base, ...over });
const proposal: ScheduleProposal = {
  id: "p1", name: "Mine reset", prompt: "Reset the mine", cron: "10 2 * * *", run_at: null, kind: "agent", next_run_at: "2026-10-29T02:10:00Z",
  file_count: 0, files: [], legacy_file_review_required: false, source_session_id: "chat-a", source_project_id: null,
  proposal_revision: 1, request_digest: "d", status: "pending", created_at: "2026-10-25T19:22:35Z",
};

describe("the schedules list", () => {
  it("lists each schedule where its state puts it", () => {
    expect(scheduleGroup(row({}))).toBe("active");
    // Set but without its approval it cannot fire, so it waits with the requests rather than passing for active.
    expect(scheduleGroup(row({ authority_state: "needs_approval" }))).toBe("approval");
    expect(scheduleGroup(row({ enabled: 0 }))).toBe("paused");
    expect(scheduleGroup(row({ cron: null, run_at: "2026-10-27T13:50:00Z", enabled: 0, last_run_at: "2026-10-27T13:50:20Z", next_run_at: null }))).toBe("done");
    expect(scheduleGroup(row({ cron: null, run_at: "2026-11-27T13:50:00Z", enabled: 0, next_run_at: null }))).toBe("paused");
  });

  it("puts a waiting proposal first and orders each group by what comes soonest", () => {
    const items = itemsFrom([
      row({ id: "late", next_run_at: "2026-10-30T00:00:00Z" }),
      row({ id: "soon", next_run_at: "2026-10-28T12:00:00Z" }),
      row({ id: "old", cron: null, run_at: "2026-10-01T00:00:00Z", enabled: 0, next_run_at: null, last_run_at: "2026-10-01T00:00:10Z" }),
    ], [proposal, { ...proposal, id: "p-gone", status: "withdrawn" }]);
    const groups = groupItems(items);
    expect(groups.approval.map((i) => i.key)).toEqual(["p1"]);
    expect(groups.active.map((i) => i.key)).toEqual(["soon", "late"]);
    expect(groups.done.map((i) => i.key)).toEqual(["old"]);
  });

  it("knows the conversation a schedule belongs to and keeps one chat's rows", () => {
    const own = row({ id: "own", run_in: "self", target_session: "chat-a" });
    const asked = row({ id: "asked", source_session_id: "chat-a" });
    const other = row({ id: "other", created_by_session: "chat-b", name: "Backup check" });
    const items = itemsFrom([own, asked, other], [proposal]);
    expect(conversationOf(items.find((i) => i.key === "own")!)).toBe("chat-a");
    expect(conversationOf(items.find((i) => i.key === "asked")!)).toBe("chat-a");
    expect(filterItems(items, { sessionId: "chat-a" }).map((i) => i.key).sort()).toEqual(["asked", "own", "p1"]);
    // Every word typed must match the name, the text or the conversation's title.
    expect(filterItems(items, { query: "backup" }).map((i) => i.key)).toEqual(["other"]);
    expect(filterItems(items, { query: "mine reset" }).map((i) => i.key)).toEqual(["p1"]);
    expect(filterItems(items, { query: "bakery", titles: { "chat-b": "Bakery site" } }).map((i) => i.key)).toEqual(["other"]);
  });

  it("says how the last run went and where to read it", () => {
    expect(lastRun(row({}))).toBeNull();
    expect(lastRun(row({ last_run_at: "2026-10-28T02:10:00Z", last_session_id: "run-1" }))).toEqual({ status: "ok", at: "2026-10-28T02:10:00Z", sessionId: "run-1" });
    expect(lastRun(row({ last_run_at: "2026-10-28T02:10:00Z", failure_count: 2, run_in: "self", target_session: "chat-a" }))!.status).toBe("failed");
    expect(lastRun(row({ last_run_at: "2026-10-28T02:10:00Z", failure_count: 2, run_in: "self", target_session: "chat-a" }))!.sessionId).toBe("chat-a");
    expect(lastRun(row({ active_session_id: "run-2" }))).toEqual({ status: "running", at: null, sessionId: "run-2" });
  });
});

describe("the edit form", () => {
  it("opens an existing schedule on its own choice in the reader's clock", () => {
    expect(parseLocalCron("10 2 * * *", 300)).toMatchObject({ when: "daily", hour: 7, minute: 10 });
    expect(parseLocalCron("0 4 * * 1-5", 300)).toMatchObject({ when: "weekdays", hour: 9, minute: 0 });
    // Monday-first indices, moved a day on when the zone crosses midnight.
    expect(parseLocalCron("0 22 * * 1,3", 300)).toMatchObject({ when: "weekly", hour: 3, days: [1, 3] });
    expect(parseLocalCron("15 */3 * * *", 300)).toMatchObject({ when: "hours", minute: 15, every: 3 });
    expect(parseLocalCron("50 5,11 * * *", 300).when).toBe("cron");
  });
});
