// @vitest-environment jsdom
// The Subagents tab's rules without a browser: where a helper opens, which event reads the list again,
// how a helper's state reads.

import { describe, expect, it } from "vitest";
import type { SessionList, SubagentView } from "./api";
import { t } from "./i18n";
import { stripToday } from "./panelstrip";
import { activityLine, contextPercent, effortLabel, listedLeaderOf, subagentGroups, subagentIdFor, subagentLine, subagentPath, subagentPhase, subagentState, subagentSummary, touchesSubagents } from "./subagents";
import { systemNote } from "./turns";

const child = (over: Partial<SubagentView> = {}): SubagentView => ({ session_id: "c1", name: "triage", running: false, status: "idle", model: "GPT", kept: true, ...over });

const listing = (sessions: { id: string; leader?: string }[]): SessionList =>
  ({ sessions: sessions.map((s) => ({ id: s.id, metadata: s.leader ? { subagent_of: s.leader } : {} })) }) as unknown as SessionList;

describe("where a subagent opens", () => {
  it("beside its leader, on the Subagents tab", () => {
    expect(subagentPath("lead", "c1")).toBe("/app/agents/lead?panel=subagents&sub=c1");
  });
  it("finds the leader in a listing that holds both", () => {
    expect(listedLeaderOf("c1", [listing([{ id: "lead" }, { id: "c1", leader: "lead" }])])).toBe("lead");
  });
  it("opens a helper whose leader is gone as the chat it is", () => {
    expect(listedLeaderOf("c1", [listing([{ id: "c1", leader: "lead" }])])).toBeNull();
    expect(listedLeaderOf("lead", [listing([{ id: "lead" }])])).toBeNull();
    expect(listedLeaderOf("c1", [undefined])).toBeNull();
  });
});

describe("which helper a step names", () => {
  const kids = [child(), child({ session_id: "c2", name: "review" })];
  it("by session first, then by name", () => {
    expect(subagentIdFor(kids, { id: "c2", name: "triage" })).toBe("c2");
    expect(subagentIdFor(kids, { name: "review" })).toBe("c2");
    expect(subagentIdFor(kids, { id: "gone", name: "triage" })).toBe("c1");
    expect(subagentIdFor(kids, { name: "nobody" })).toBeNull();
  });
});

describe("which events read the list again", () => {
  const ev = (type: string, session_id: string | null, payload: Record<string, unknown> = {}) => ({ type, session_id, payload });
  const known = new Set(["c1"]);
  it("hears a helper made under it and the helpers it lists", () => {
    expect(touchesSubagents(ev("session.created", "c2", { parent: "lead" }), "lead", known)).toBe(true);
    expect(touchesSubagents(ev("session.status", "c1", { status: "idle" }), "lead", known)).toBe(true);
    expect(touchesSubagents(ev("session.deleted", "c1"), "lead", known)).toBe(true);
  });
  it("ignores the leader itself and strangers", () => {
    expect(touchesSubagents(ev("session.status", "lead"), "lead", known)).toBe(false);
    expect(touchesSubagents(ev("session.created", "x", { parent: "other" }), "lead", known)).toBe(false);
    expect(touchesSubagents(ev("run.finished", "stranger"), "lead", known)).toBe(false);
  });
});

describe("how a helper's state reads", () => {
  it("works, waits, fails or rests", () => {
    expect(subagentState(child({ running: true, status: "running" }))).toBe("running");
    expect(subagentState(child({ status: "compacting" }))).toBe("running");
    expect(subagentState(child({ status: "waiting" }))).toBe("waiting");
    expect(subagentState(child({ status: "failed" }))).toBe("failed");
    expect(subagentState(child())).toBe("idle");
  });
  it("reads a helper the daily cap stopped as parked, not failed and not gone", () => {
    const parked = child({ status: "parked", parked: { reason: "budget", at: "2026-11-09T20:01:02Z" }, kept: false });
    expect(subagentState(parked)).toBe("parked");
    expect(subagentState(child({ status: "failed", parked: { reason: "budget", at: "" } }))).toBe("parked");
    expect(subagentLine(parked, Date.now())).toContain(t("subagents.parked"));
    // Its run going on again outranks the mark the host clears a moment later.
    expect(subagentState(child({ running: true, status: "running", parked: { reason: "budget", at: "" } }))).toBe("running");
  });
  it("says when its report waits for the leader", () => {
    expect(subagentLine(child({ reports_held: 1 }), Date.now())).toContain(t("subagents.held"));
    expect(subagentLine(child(), Date.now())).not.toContain(t("subagents.held"));
  });
  it("says for how long it has worked, and on which model", () => {
    const now = Date.parse("2026-10-13T12:05:00Z");
    const line = subagentLine(child({ running: true, status: "running", run_started_at: "2026-10-13T12:02:00Z" }), now);
    expect(line).toContain("3");
    expect(line).toContain("GPT");
  });
});

describe("the leader's chat says a helper is parked", () => {
  it("carries the host's notice on the helper's line", () => {
    const notice = { state: "parked" as const, cause: "budget", name: "cand-a", session_id: "c1", budget: true, held: true };
    const note = systemNote({ role: "user", text: "Subagent 'cand-a' is parked by the daily spending cap.", origin: "subagent:cand-a", subagent_notice: notice });
    expect(note?.kind).toBe("subagent");
    expect(note?.origin).toBe("cand-a");
    expect(note?.notice?.state).toBe("parked");
    expect(systemNote({ role: "user", text: "report", origin: "subagent:cand-a" })?.notice).toBeUndefined();
  });
});

const report = (cause: NonNullable<SubagentView["report"]>["cause"], delivered = true) => ({ at: "2026-11-10T10:00:00Z", cause, summary: "s", delivered, delivered_at: delivered ? "2026-11-10T10:00:00Z" : null });

describe("the state a card names", () => {
  it("tells working, asking and parked apart", () => {
    expect(subagentPhase(child({ running: true, status: "running" }))).toBe("working");
    expect(subagentPhase(child({ status: "waiting" }))).toBe("question");
    expect(subagentPhase(child({ status: "parked", parked: { reason: "budget", at: "" } }))).toBe("parked");
  });
  it("tells a finished run from a stopped, a failed and one never given work", () => {
    expect(subagentPhase(child({ report: report("completed") }))).toBe("done");
    expect(subagentPhase(child({ report: report("cancelled") }))).toBe("stopped");
    expect(subagentPhase(child({ status: "failed" }))).toBe("failed");
    // A cap or the provider ended it: failed, though the session's own error is cleared since.
    expect(subagentPhase(child({ report: report("cap") }))).toBe("failed");
    expect(subagentPhase(child({ idle_start: true }))).toBe("standby");
    expect(subagentPhase(child({ idle_start: true, report: report("completed") }))).toBe("done");
  });
});

describe("how the tab orders and sums its helpers", () => {
  const kids = [
    child({ session_id: "old-done", report: report("completed"), finished_at: "2026-11-10T08:00:00Z", usage: { calls: 3, input: 10, output: 1, cache_read: 0, usd: 0.5, unpriced: 0 } }),
    child({ session_id: "old-run", running: true, status: "running", created_at: "2026-11-10T07:00:00Z" }),
    child({ session_id: "new-done", report: report("completed"), finished_at: "2026-11-10T09:00:00Z", usage: { calls: 2, input: 10, output: 1, cache_read: 0, usd: 0.25, unpriced: 1 } }),
    child({ session_id: "new-run", running: true, status: "running", created_at: "2026-11-10T09:30:00Z" }),
    child({ session_id: "broke", status: "failed", finished_at: "2026-11-10T06:00:00Z" }),
    child({ session_id: "asks", status: "waiting", created_at: "2026-11-10T05:00:00Z" }),
  ];
  it("puts the working ones first, newest first, then the rest by when they ended", () => {
    const groups = subagentGroups(kids);
    expect(groups.active.map((c) => c.session_id)).toEqual(["new-run", "old-run", "asks"]);
    expect(groups.finished.map((c) => c.session_id)).toEqual(["new-done", "old-done", "broke"]);
  });
  it("counts running, done and failed, and says a sum over unpriced calls is a floor", () => {
    expect(subagentSummary(kids)).toEqual({ running: 3, done: 2, failed: 1, usd: 0.75, partial: true });
    expect(subagentSummary([]).partial).toBe(false);
  });
});

describe("what a card says about the run", () => {
  it("names the effort, off as no thinking, and keeps a provider's own word", () => {
    expect(effortLabel("high")).toBe(t("add.effort.high"));
    expect(effortLabel("off")).toBe(t("subagents.effort.off"));
    expect(effortLabel("auto")).toBe("auto");
    expect(effortLabel(undefined)).toBe("");
  });
  it("draws the context against its window, and nothing without one", () => {
    expect(contextPercent({ tokens: 50_000, window: 200_000 })).toBe(25);
    expect(contextPercent({ tokens: 300_000, window: 200_000 })).toBe(100);
    expect(contextPercent({ tokens: 10, window: 0 })).toBeNull();
    expect(contextPercent(undefined)).toBeNull();
  });
  it("says the tool in flight with the step, or that it thinks", () => {
    expect(activityLine({ tool: "Exec", last_tool: "Read", steps: 14 })).toEqual({ what: "Exec", tool: true, steps: t("subagents.now.steps", { n: 14 }) });
    expect(activityLine({ tool: null, last_tool: "Read", steps: 0 })).toEqual({ what: t("subagents.now.thinking"), tool: false, steps: "" });
    expect(activityLine(null)).toBeNull();
  });
});

describe("the strip's today", () => {
  it("says a day of calls that cost nothing was unmetered, not that it spent nothing", () => {
    // The session that raised it: $13.72 metered yesterday, 53 subscription calls today.
    const unmetered = stripToday({ usd: 13.72, usd_today: 0, c_today: 53 });
    expect(unmetered.short).toBe(t("strip.cost.today.unmetered"));
    expect(unmetered.title).toContain("53");
    expect(stripToday({ usd: 13.72, usd_today: 1.5, c_today: 4 }).short).toBe(t("strip.cost.today", { sum: "$1.50" }));
    expect(stripToday({ usd: 13.72, usd_today: 0, c_today: 0 }).short).toBe(t("strip.cost.today", { sum: "$0" }));
  });
});
