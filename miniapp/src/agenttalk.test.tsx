// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import type { AgentTalk } from "./api";
import { agentsWaiting, talkCounts, talkState, talksMoved } from "./agenttalk";

const base = { state: "open", reason: "", waiting_for: "", quiet: false, held: 0, unread: 0 } as const;

describe("what a conversation is doing", () => {
  it("puts what waits for the operator first", () => {
    expect(talkState({ ...base, held: 1, unread: 2 })).toEqual({ key: "held", tone: "attn" });
    expect(talkState({ ...base, unread: 1 })).toEqual({ key: "unread", tone: "attn" });
  });

  it("tells waiting for a reply from a door that said to stop waiting", () => {
    expect(talkState({ ...base, waiting_for: "reply" }).key).toBe("waiting");
    expect(talkState({ ...base, waiting_for: "reply", quiet: true }).key).toBe("quiet");
    expect(talkState({ ...base, waiting_for: "owner" }).key).toBe("owner");
    expect(talkState({ ...base, waiting_for: "offline", quiet: true }).key).toBe("offline");
  });

  it("calls a closed conversation closed whatever was left in it", () => {
    expect(talkState({ ...base, state: "closed", held: 1 })).toEqual({ key: "closed", tone: "" });
  });
});

const talk = (over: Partial<AgentTalk> = {}): AgentTalk => ({
  id: "t1", session_id: "s", host: "agents.example.test", topic: "", mode: "direct", state: "open", reason: "", waiting_for: "", quiet: false, review: "first",
  sends: 1, sends_limit: 30, wake_turns: 0, wake_usd: 0, created_at: "", last_at: "", closed_at: null, spent_usd: 0, replies: 0, unread: 0, held: 0, file_mb: 50, day_mb: 500, tasks: [], ...over,
});

describe("when the Agents tab opens by itself", () => {
  const seen = (talks: AgentTalk[]) => new Map(talks.map((one) => [one.id, { replies: one.replies, held: one.held }]));

  it("opens on a new conversation, a reply and a message that starts waiting, and not on the first read", () => {
    expect(talksMoved(null, [talk({ held: 1 })])).toBe(false);
    expect(talksMoved(seen([]), [talk()])).toBe(true);
    expect(talksMoved(seen([talk()]), [talk({ replies: 1 })])).toBe(true);
    expect(talksMoved(seen([talk()]), [talk({ held: 1 })])).toBe(true);
  });

  it("does not open again while nothing changed, nor for a conversation that is over", () => {
    expect(talksMoved(seen([talk({ held: 1, replies: 2 })]), [talk({ held: 1, replies: 2 })])).toBe(false);
    expect(talksMoved(seen([talk({ held: 1 })]), [talk({ held: 0 })])).toBe(false);
    expect(talksMoved(seen([]), [talk({ state: "closed" })])).toBe(false);
  });

  it("counts what waits in the live conversations only", () => {
    expect(agentsWaiting([talk({ held: 1, unread: 2 }), talk({ id: "t2", state: "closed", held: 1 })])).toBe(3);
  });
});

describe("the counts of a conversation", () => {
  it("says how many of the cap were sent, and just how many where there is no cap", () => {
    expect(talkCounts(talk({ sends: 3, sends_limit: 30 }))).toContain("3");
    expect(talkCounts(talk({ sends: 3, sends_limit: 30 }))).toContain("30");
    expect(talkCounts(talk({ sends: 31, sends_limit: 0 })).split(" · ")[0]).toBe("31 sent");
  });
});
