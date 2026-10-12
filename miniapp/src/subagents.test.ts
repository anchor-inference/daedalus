// @vitest-environment jsdom
// The Subagents tab's rules without a browser: where a helper opens, which event reads the list again,
// how a helper's state reads.

import { describe, expect, it } from "vitest";
import type { SessionList, SubagentView } from "./api";
import { listedLeaderOf, subagentIdFor, subagentLine, subagentPath, subagentState, touchesSubagents } from "./subagents";

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
  it("says for how long it has worked, and on which model", () => {
    const now = Date.parse("2026-10-13T12:05:00Z");
    const line = subagentLine(child({ running: true, status: "running", run_started_at: "2026-10-13T12:02:00Z" }), now);
    expect(line).toContain("3");
    expect(line).toContain("GPT");
  });
});
