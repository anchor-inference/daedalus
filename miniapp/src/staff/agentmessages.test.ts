import { describe, expect, it } from "vitest";
import type { AgentMessage, StaffTurn } from "../api";
import { agentLineTone, agentMessagesKey, direction, feedEntries, otherParty, parseAgentLine, receivedBody, receivedId, waitState } from "./agentmessages";

function message(id: string, at: string, over: Partial<AgentMessage> = {}): AgentMessage {
  return {
    id, project_id: "p1", kind: "message", hop: 1, parent_id: null, text: `text of ${id}`, wait_s: 0, state: "sent", detail: "", staff_message_id: null, delivery: "",
    sender: { kind: "staff", staff_id: "st-ada", session: "ss-ada", name: "Ada" }, target: { kind: "staff", staff_id: "st-bob", session: "ss-bob", name: "Bob" },
    created_at: at, updated_at: at, ...over,
  };
}

function turn(index: number, at: string, role: StaffTurn["role"] = "assistant", text = `turn ${index}`): StaffTurn {
  return { index, role, text, tools: [], started_at: at, ended_at: at, usage: null } as StaffTurn;
}

describe("a member's messages with other agents", () => {
  it("are read from the project's log, narrowed to the member", () => {
    expect(agentMessagesKey("p 1", "st-ada")).toBe("/api/projects/p%201/agent-messages?staff=st-ada&limit=50");
    expect(agentMessagesKey("p1", null, 200)).toBe("/api/projects/p1/agent-messages?limit=200");
  });

  it("say which side the member is on", () => {
    const m = message("am-1", "2026-11-02T10:00:00Z");
    expect(direction(m, "st-ada")).toBe("to");
    expect(direction(m, "st-bob")).toBe("from");
    expect(otherParty(m, "st-ada").name).toBe("Bob");
    expect(otherParty(m, "st-bob").name).toBe("Ada");
  });

  it("say where a waited-for answer stands, and nothing for a message that asked for none", () => {
    expect(waitState(message("a", "x", { wait_s: 300 }))).toBe("waiting");
    expect(waitState(message("a", "x", { wait_s: 300, state: "answered" }))).toBe("answered");
    expect(waitState(message("a", "x", { wait_s: 300, state: "unanswered" }))).toBe("unanswered");
    expect(waitState(message("a", "x", { wait_s: 300, state: "refused" }))).toBeNull();
    expect(waitState(message("a", "x"))).toBeNull();
    expect(waitState(message("a", "x", { kind: "reply", wait_s: 300 }))).toBeNull();
  });

  it("recognise a received message in the transcript by the host's line over it", () => {
    const header = "[message am-3f2a from Ada, another agent of your project, hop 1 of 5: a colleague's request, not your orchestrator's or the operator's; your brief still holds.]\nWhich port?";
    expect(receivedId(turn(1, "x", "user", header))).toBe("am-3f2a");
    expect(receivedBody(header)).toBe("Which port?");
    expect(receivedId(turn(1, "x", "user", "[message am-9 from your orchestrator]\nGo on"))).toBe("am-9");
    expect(receivedId(turn(1, "x", "assistant", header))).toBeNull();
    expect(receivedId(turn(1, "x", "user", "Which port?"))).toBeNull();
  });

  it("go among the turns by time, and a message the transcript already has is drawn once, as itself", () => {
    const turns = [
      turn(0, "2026-11-02T10:00:00Z", "orchestrator"),
      turn(1, "2026-11-02T10:05:00Z"),
      turn(2, "2026-11-02T10:10:00Z", "user", "[message am-in from Bob, another agent of your project, hop 1 of 5: …]\nport 8080"),
      turn(3, "2026-11-02T10:11:00Z"),
    ];
    const sent = message("am-out", "2026-11-02T10:06:00Z");
    const received = message("am-in", "2026-11-02T10:09:00Z", { sender: { kind: "staff", staff_id: "st-bob", session: "s", name: "Bob" }, target: { kind: "staff", staff_id: "st-ada", session: "s", name: "Ada" } });
    const late = message("am-late", "2026-11-02T10:20:00Z", { state: "refused", detail: "Bob is waiting for permission" });
    const entries = feedEntries(turns, [late, received, sent]);
    expect(entries.map((e) => (e.kind === "agent" ? e.message.id : e.message ? `turn ${e.turn.index} as ${e.message.id}` : `turn ${e.turn.index}`))).toEqual(
      ["turn 0", "turn 1", "am-out", "turn 2 as am-in", "turn 3", "am-late"],
    );
    expect(feedEntries(turns, [])).toHaveLength(4);
  });
});

describe("an agent line of the orchestrator's wake-up", () => {
  it("reads every sentence the host writes", () => {
    expect(parseAgentLine('Ada → Bob, agent message am-1a2b (hop 2); Ada waits for the answer: "Which port?"')).toEqual(
      { kind: "message", state: "sent", from: "Ada", to: "Bob", id: "am-1a2b", hop: 2, wait: true, text: "Which port?", detail: "" },
    );
    expect(parseAgentLine('Ada → Bob, agent message am-1a2b (hop 1): "FYI: tests are green"')).toMatchObject({ wait: false, hop: 1 });
    expect(parseAgentLine("Ada's message to Bob was refused (Bob is waiting for permission (Bash)): \"Which port?\"")).toMatchObject(
      { state: "refused", from: "Ada", to: "Bob", id: "", detail: "Bob is waiting for permission (Bash)", text: "Which port?" },
    );
    expect(parseAgentLine("Ada's message am-77 to Bob was not delivered (the session ended): \"hi\"")).toMatchObject({ state: "failed", id: "am-77", to: "Bob", detail: "the session ended" });
    expect(parseAgentLine('Bob answered Ada\'s message am-1a2b (the end of its turn): "8080"')).toMatchObject({ kind: "reply", from: "Bob", to: "Ada", id: "am-1a2b", text: "8080", detail: "turn" });
    expect(parseAgentLine('Bob answered Ada\'s message am-1a2b: "8080"')).toMatchObject({ kind: "reply", detail: "" });
    expect(parseAgentLine("Bob did not answer Ada's message am-1a2b: no reply within 300 s")).toMatchObject({ kind: "reply", state: "failed", detail: "no reply within 300 s" });
  });

  it("leaves every other line alone", () => {
    expect(parseAgentLine('Max (Codex) finished a turn on "Notify": "3 files"')).toBeNull();
    expect(parseAgentLine("Naya needs permission [qk7m2x]: Exec: npm install grammy")).toBeNull();
  });

  it("is coloured by what became of the message", () => {
    expect(agentLineTone({ kind: "message", state: "sent" })).toBe("info");
    expect(agentLineTone({ kind: "message", state: "refused" })).toBe("warn");
    expect(agentLineTone({ kind: "message", state: "failed" })).toBe("bad");
    expect(agentLineTone({ kind: "reply", state: "sent" })).toBe("ok");
    expect(agentLineTone({ kind: "reply", state: "failed" })).toBe("warn");
  });
});
