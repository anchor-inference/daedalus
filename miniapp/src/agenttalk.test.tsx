// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { talkState } from "./agenttalk";

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
  });

  it("calls a closed conversation closed whatever was left in it", () => {
    expect(talkState({ ...base, state: "closed", held: 1 })).toEqual({ key: "closed", tone: "" });
  });
});
