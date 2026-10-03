// The goal line and the fate of the operator's messages, decided without a browser: which counts the
// line names, how the list orders what it shows, what a message's receipt says, and what a reply sends.

import { describe, expect, it } from "vitest";
import {
  DECISION_NEXT, cardTitle, deliveryState, excerptOf, goalLine, goalRow, goalRows, hasFate, isFocusState, lastAnswerSeq, lineNeedsAttention,
  messageBody, messageFate, resultKey, resultRows, uploadText, type FocusGoal, type Receipt,
} from "./goalmodel";

function goal(over: Partial<FocusGoal> = {}): FocusGoal {
  return { task_id: "t1", title: "Photos", status: "doing", acceptance: "", owner: { id: "s1", name: "Lev" }, previous_owner: null, next: "", updated_at: "2026-09-24T09:00:00Z", ...over };
}

describe("the goal line", () => {
  it("names only the counts that are not zero, in a fixed order", () => {
    expect(goalLine({ in_work: 3, decisions: 0, waiting_for_you: 1, unconfirmed: 0, commitments: 2 })).toEqual([
      { kind: "in_work", n: 3 }, { kind: "waiting_for_you", n: 1 }, { kind: "commitments", n: 2 },
    ]);
    // A quiet project has no line at all, and a count the host did not send is no count.
    expect(goalLine({ in_work: 0, decisions: 0, waiting_for_you: 0, unconfirmed: 0, commitments: 0 })).toEqual([]);
    expect(goalLine({ decisions: 2 })).toEqual([{ kind: "decisions", n: 2 }]);
    expect(goalLine(null)).toEqual([]);
  });

  it("is edged in amber only when a decision or the operator is waited on", () => {
    expect(lineNeedsAttention({ in_work: 4, commitments: 1 })).toBe(false);
    expect(lineNeedsAttention({ in_work: 4, decisions: 1 })).toBe(true);
    expect(lineNeedsAttention({ waiting_for_you: 1 })).toBe(true);
  });

  it("takes only an answer of the right shape", () => {
    expect(isFocusState({ counts: {}, goals: [], open_results: [], commitments: [], receipts: {} })).toBe(true);
    expect(isFocusState({ detail: "Not Found" })).toBe(false);
    expect(isFocusState("<html>")).toBe(false);
    expect(isFocusState(null)).toBe(false);
  });
});

describe("the list behind the line", () => {
  it("reads a goal as card → owner → acceptance → what it waits on", () => {
    expect(goalRow(goal({ acceptance: "handed_in" }))).toEqual({ id: "t1", title: "Photos", status: "doing", owner: "Lev", unowned: false, acceptance: "handed_in", next: null, blocked: false });
    // Nobody holds it: the row names who held it last, for rework goes back to them.
    const orphan = goalRow(goal({ owner: null, previous_owner: "Olga" }));
    expect([orphan.owner, orphan.unowned]).toEqual(["Olga", true]);
    expect(goalRow(goal({ owner: null })).owner).toBe("");
  });

  it("says the host's own sentence in the reader's language and a note's words as they are", () => {
    expect(goalRow(goal({ next: DECISION_NEXT })).next).toEqual({ key: "goal.next.decision" });
    expect(goalRow(goal({ next: "the operator: which bot token" })).next).toEqual({ text: "the operator: which bot token" });
    expect(goalRow(goal({ next: DECISION_NEXT })).blocked).toBe(true);
    expect(goalRow(goal({ status: "blocked" })).blocked).toBe(true);
  });

  it("puts the goals that wait on someone first, keeping the host's order otherwise", () => {
    const rows = goalRows([goal({ task_id: "a" }), goal({ task_id: "b", next: "the operator" }), goal({ task_id: "c" }), goal({ task_id: "d", status: "blocked" })]);
    expect(rows.map((r) => r.id)).toEqual(["b", "d", "a", "c"]);
  });

  it("reads the open results oldest first, each in a sentence of its cause", () => {
    const at = (m: number) => `2026-09-24T09:${String(m).padStart(2, "0")}:00Z`;
    const rows = resultRows([
      { id: 2, cause: "report_done", task_id: "t1", title: "Photos", staff_name: "Lev", summary: "", opened_at: at(40), reminded: false },
      { id: 1, cause: "task_unowned", task_id: "t2", title: "Hours", staff_name: null, summary: "", opened_at: at(10), reminded: true },
    ]);
    expect(rows.map((r) => r.id)).toEqual([1, 2]);
    expect(resultKey("report_stuck")).toBe("goal.result.report_stuck");
    expect(resultKey("something_new")).toBe("goal.result.other");
    expect(cardTitle("t1", [goal()])).toBe("Photos");
    expect(cardTitle("t9", [goal()])).toBe("");
    expect(cardTitle(null, [goal()])).toBe("");
  });
});

describe("what became of the operator's message", () => {
  const receipts: Record<string, Receipt[]> = {
    "18": [
      { kind: "requirement", label: "R4", text: "Menu photos only", state: "active", task_id: "t1", task_title: "Photos", deliveries: [{ staff_name: "Lev", acknowledged: true, opened: false, via: "message", cli: false }] },
      { kind: "commitment", id: 7, text: "Tell you when the gallery is live", task_id: "t1", kept: false },
    ],
    "23": [{ kind: "requirement", label: "R2", text: "List the holidays", state: "active", task_id: "t5", task_title: "", deliveries: [{ staff_name: "Olga", acknowledged: false, opened: false, via: "message", cli: false }] }],
  };

  it("names each requirement with its card and each member's standing, and each commitment with whether it was kept", () => {
    const fate = messageFate({ seq: 18, delivery: null }, receipts, 30);
    expect(fate.lines).toEqual([
      { kind: "requirement", label: "R4", card: "Photos", text: "Menu photos only", state: "active", deliveries: [{ name: "Lev", state: "confirmed" }] },
      { kind: "commitment", text: "Tell you when the gallery is live", kept: false },
    ]);
    // A receipt says more than "read", so it is not said twice.
    expect(fate.read).toBe(false);
    // An absent title is rendered with a localized generic label rather than exposing the host ID.
    expect(messageFate({ seq: 23 }, receipts, 30).lines[0]).toMatchObject({ card: "", deliveries: [{ name: "Olga", state: "sent" }] });
  });

  it("tells confirmed from confirmed in words, opened and only sent", () => {
    const d = { staff_name: "Lev", acknowledged: false, opened: false, via: "message", cli: false };
    expect(deliveryState(d)).toBe("sent");
    expect(deliveryState({ ...d, opened: true })).toBe("opened");
    expect(deliveryState({ ...d, acknowledged: true, opened: true })).toBe("confirmed");
    expect(deliveryState({ ...d, acknowledged: true, cli: true })).toBe("words");
  });

  it("says a message with no receipt was read once the orchestrator answered after it, and not before", () => {
    expect(messageFate({ seq: 10 }, receipts, 13)).toEqual({ lines: [], arrival: null, read: true });
    expect(hasFate(messageFate({ seq: 10 }, receipts, 9))).toBe(false);
    expect(hasFate(messageFate({ seq: 10 }, null, null))).toBe(false);
    // A message still being sent has no seq and nothing to say yet.
    expect(hasFate(messageFate({ seq: null }, receipts, 99))).toBe(false);
  });

  it("says how a message arrived when it arrived during a turn or as one ended", () => {
    expect(messageFate({ seq: 21, delivery: "steer" }, receipts, 22)).toMatchObject({ arrival: "steer", read: true });
    expect(messageFate({ seq: 21, delivery: "drained" }, receipts, 20)).toMatchObject({ arrival: "drained", read: false });
    expect(hasFate(messageFate({ seq: 21, delivery: "drained" }, null, null))).toBe(true);
    expect(messageFate({ seq: 21, delivery: "follow_up" }, receipts, 20).arrival).toBeNull();
  });

  it("measures read against the latest answer of the model, not against notes or tool rows", () => {
    expect(lastAnswerSeq([
      { role: "user", seq: 10 }, { role: "assistant", seq: 13 }, { role: "user", seq: 14 }, { role: "tool", seq: 16 }, { role: "assistant", seq: 15, internal: true },
    ])).toBe(13);
    expect(lastAnswerSeq([])).toBeNull();
  });
});

describe("a reply", () => {
  it("quotes one line with markdown's marks off, cut where the host would cut it", () => {
    expect(excerptOf("**Done** — see `cart.ts`\n\n- one\n- two")).toBe("Done — see cart.ts - one - two");
    expect(excerptOf("[the page](https://example.org/x) is up")).toBe("the page is up");
    const long = excerptOf("word ".repeat(200));
    expect(long.length).toBe(300);
    expect(long.endsWith("…")).toBe(true);
  });

  it("carries what it answers in the body, and nothing when it answers nothing", () => {
    expect(messageBody("yes", { steer: false, clientMessageId: "c1", reply: { seq: 14, excerpt: "09:51 Max finished" } })).toEqual({ text: "yes", client_message_id: "c1", reply_to: { seq: 14, excerpt: "09:51 Max finished" } });
    expect(messageBody("yes", { steer: true, clientMessageId: "c1", reply: null })).toEqual({ text: "yes", steer: true, client_message_id: "c1" });
  });

  it("travels in the words of an upload, which takes no reference", () => {
    expect(uploadText("see the file", { seq: 3, excerpt: "the report" })).toBe("[In reply to: «the report»]\nsee the file");
    expect(uploadText("see the file", null)).toBe("see the file");
  });
});
