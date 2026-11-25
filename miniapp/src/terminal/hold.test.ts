import { describe, expect, it } from "vitest";
import type { TerminalView } from "../api";
import { holdOf, keyboardOf } from "./hold";

type Facts = Parameters<typeof holdOf>[0];

const facts = (fields: Partial<NonNullable<Facts>> = {}): NonNullable<Facts> => ({
  status: "running", owner: { kind: "free", id: null }, created_by: "operator", activity: null, live: null, exit_code: null, exit_signal: null, ...fields,
});

describe("who holds a terminal", () => {
  it("leaves the operator's own terminal to the operator", () => {
    expect(holdOf(facts())).toBe("yours");
    expect(holdOf(facts({ owner: { kind: "project", id: "p" } }))).toBe("yours");
    expect(holdOf(null)).toBe("yours");
  });

  it("gives a session's or a staff member's terminal to the agent that writes there", () => {
    expect(holdOf(facts({ owner: { kind: "session", id: "s" } }))).toBe("agent");
    expect(holdOf(facts({ owner: { kind: "staff", id: "st" } }))).toBe("agent");
    expect(holdOf(facts({ created_by: "agent:ira" }))).toBe("agent");
  });

  it("calls an agent that waits for the operator's answer held, because typing would not reach it", () => {
    const waiting = { status: "permission" } as TerminalView["activity"];
    expect(holdOf(facts({ owner: { kind: "staff", id: "st" }, activity: waiting }))).toBe("held");
    // A session's terminal is not a member's request; the answer is in the chat.
    expect(holdOf(facts({ owner: { kind: "session", id: "s" }, activity: waiting }))).toBe("agent");
  });

  it("calls a terminal that ended, in the listing or on the screen, ended", () => {
    expect(holdOf(facts({ status: "exited" }))).toBe("ended");
    expect(holdOf(facts({ status: "lost" }))).toBe("ended");
    expect(holdOf(facts(), true)).toBe("ended");
  });

  it("reads the keyboard from the live connection first, then the listing, else gives it to nobody in particular", () => {
    const live = { clients: 1, busy: false, keyboard: { owner: "human" as const, until: null }, size_owner: "you", alt_screen: false };
    expect(keyboardOf({ live }, "agent")).toBe("agent");
    expect(keyboardOf({ live }, null)).toBe("human");
    expect(keyboardOf({ live: null })).toBe("auto");
    expect(keyboardOf(null)).toBe("auto");
  });
});
