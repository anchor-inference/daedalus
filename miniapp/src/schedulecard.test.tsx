// @vitest-environment jsdom
// The schedule card under a turn whose agent created one: drawn from the id the tool's answer names,
// it says what was set and when in the operator's own time; while a proposal waits (approval on) it
// carries Accept and Decline, which answer the host's proposal routes with the exact request shown.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { setLang } from "./i18n";
import { approvalExpiry, ScheduleCard, scheduleCardIds, ScheduleCardView } from "./schedulecard";
import type { Activity } from "./turns";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const future = new Date(Date.now() + 3 * 3600000).toISOString();

const set: ScheduleCardView = {
  schedule: { id: "s0123456789abcde", name: "Morning digest", cron: "0 6 * * *", run_at: null, next_run_at: future, kind: "agent", enabled: 1, deleted: false },
  proposal: null,
  collection_revision: null,
};

const waiting: ScheduleCardView = {
  schedule: null,
  proposal: {
    id: "p0123456789abcde", name: "Take the pills", prompt: "pills", cron: null, run_at: future, kind: "message",
    next_run_at: future, file_count: 0, legacy_file_review_required: false, source_project_id: "proj",
    proposal_revision: 1, request_digest: "d1", status: "pending", accepted_schedule_id: null,
  },
  collection_revision: 7,
};

describe("the schedule a turn created", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.restoreAllMocks();
  });

  const button = (action: string) => host.querySelector<HTMLButtonElement>(`button[data-action="${action}"]`);

  it("reads the id off a finished ScheduleCreate step in either form, and only off that", () => {
    const activity: Activity[] = [
      { kind: "tool", id: "a", name: "ScheduleList", args: {}, running: false, result: "Schedule: s9999999999999999" },
      { kind: "tool", id: "b", name: "ScheduleCreate", args: {}, running: false, result: "Schedule: s0123456789abcde\n'x' is set" },
      { kind: "tool", id: "c", name: "ScheduleCreate", args: {}, running: false, result: "Schedule proposal: p0123456789abcde\nwaits" },
      { kind: "tool", id: "d", name: "ScheduleCreate", args: {}, running: false, error: true, result: "give exactly one of cron or run_at" },
      { kind: "tool", id: "e", name: "ScheduleCreate", args: {}, running: true },
      { kind: "tool", id: "f", name: "ScheduleCreate", args: {}, running: false, result: "Schedule: s0123456789abcde\nagain" },
    ];
    expect(scheduleCardIds(activity)).toEqual(["s0123456789abcde", "p0123456789abcde"]);
  });

  it("shows a schedule set at once with its local time, next run and a way to Schedules, and no buttons to decide", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue(set);
    await act(async () => root.render(<ScheduleCard itemId="s0123456789abcde" />));
    expect(get).toHaveBeenCalledWith("/api/recurring/card/s0123456789abcde");
    const card = host.querySelector(".schedcard")!;
    expect(card.getAttribute("data-status")).toBe("set");
    expect(card.textContent).toContain("Morning digest");
    expect(card.textContent).toContain("Set");
    expect(card.textContent).toContain("Agent task");
    expect(card.querySelector(".schedcard-next")!.textContent).toMatch(/^Next run /);
    expect(button("open")).not.toBeNull();
    expect(button("accept")).toBeNull();
    expect(button("decline")).toBeNull();
  });

  it("offers Accept on a waiting proposal and sends the exact request it showed", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue({ ...set, proposal: { ...waiting.proposal!, status: "accepted", accepted_schedule_id: "s0123456789abcde" } });
    const post = vi.spyOn(api, "post").mockResolvedValue({ id: "s0123456789abcde" });
    await act(async () => root.render(<ScheduleCard itemId="p0123456789abcde" initial={waiting} />));
    expect(get).not.toHaveBeenCalled();
    expect(host.querySelector(".schedcard")!.getAttribute("data-status")).toBe("pending");
    expect(host.textContent).toContain("Waiting for you");
    await act(async () => button("accept")!.click());
    const [path, body] = post.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/api/recurring/proposals/p0123456789abcde/accept");
    expect(body).toMatchObject({ request_digest: "d1", expected_proposal_revision: 1, expected_collection_revision: 7 });
    expect(typeof body.expires_at).toBe("string");
    expect(typeof body.client_operation_id).toBe("string");
    // After the answer the card re-reads the host and shows the schedule the proposal became.
    expect(host.querySelector(".schedcard")!.getAttribute("data-status")).toBe("set");
    expect(button("accept")).toBeNull();
  });

  it("declines through the withdraw route, without an expiry", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ ...waiting, proposal: { ...waiting.proposal!, status: "withdrawn" } });
    const post = vi.spyOn(api, "post").mockResolvedValue({ status: "withdrawn" });
    await act(async () => root.render(<ScheduleCard itemId="p0123456789abcde" initial={waiting} />));
    await act(async () => button("decline")!.click());
    const [path, body] = post.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/api/recurring/proposals/p0123456789abcde/withdraw");
    expect(body.expires_at).toBeUndefined();
    expect(host.querySelector(".schedcard")!.getAttribute("data-status")).toBe("declined");
  });

  it("gives a one-off approval at least a day past its only occurrence", () => {
    const now = Date.parse("2026-10-28T00:00:00Z");
    expect(approvalExpiry({ cron: "0 6 * * *", next_run_at: "2027-06-01T00:00:00Z" }, now)).toBe("2026-11-27T00:00:00.000Z");
    expect(approvalExpiry({ cron: null, next_run_at: "2027-06-01T00:00:00Z" }, now)).toBe("2027-06-02T00:00:00.000Z");
  });

  it("approves a recurring schedule for as long as the host says when the operator does not review", () => {
    const now = Date.parse("2026-10-28T00:00:00Z");
    // With approval off the host names the standing ten years: an Accept by hand must not stop the
    // schedule a month later. A one-off keeps its own rule whatever the horizon.
    expect(approvalExpiry({ cron: "0 6 * * *", next_run_at: null }, now, 3650)).toBe(new Date(now + 3650 * 86400000).toISOString());
    expect(approvalExpiry({ cron: null, next_run_at: "2026-11-01T00:00:00Z" }, now, 3650)).toBe("2026-11-27T00:00:00.000Z");
  });
});
