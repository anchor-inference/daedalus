import { describe, expect, it } from "vitest";
import type { MessageView } from "./api";
import type { LiveState } from "./turns";
import { activitySummary, applyLive, buildTurns, EMPTY_LIVE, isOlderPage, liveAfter, liveBase, prepend, reconcile } from "./turns";

let clock = 1_700_000_000_000;

function msg(seq: number, over: Partial<MessageView> = {}): MessageView {
  clock += 1000;
  return { role: "assistant", seq, text: "", thinking: "", tool_calls: [], tool_results: [], created_at: new Date(clock).toISOString(), ...over };
}

const user = (seq: number, text: string) => msg(seq, { role: "user", text });
const answer = (seq: number, text: string) => msg(seq, { text });
const call = (seq: number, id: string, name = "Exec") => msg(seq, { tool_calls: [{ id, name, arguments: {} }] });
const result = (seq: number, id: string, content: string) => msg(seq, { role: "tool", tool_results: [{ id, content, is_error: false }] });

describe("buildTurns", () => {
  it("closes a run that produced no answer with its outcome, ahead of the host's context summary", () => {
    const outcome = { status: "failed" as const, cause: "context" as const, error_kind: "llm_context_window_exceeded", detail: "compaction exhausted retries", steps: 176 };
    const turns = buildTurns([
      { ...user(1, "Loop iteration 189"), run_id: "tick-189" },
      { ...call(2, "w1", "Write"), run_id: "tick-189" },
      { ...result(3, "w1", "written"), run_id: "tick-189" },
      { ...msg(4, { role: "system", text: "The run ended without an answer", outcome }), run_id: "tick-189" },
      msg(5, { role: "user", summary: true, compaction: { reason: "auto", messages: 304 }, text: "## Goal …" }),
    ]);
    expect(turns).toHaveLength(2);
    expect(turns[0].outcome).toEqual(outcome);
    expect(turns[0].answer).toBe("");
    expect(turns[1].summary).toBeTruthy();
  });
  it("gives a closing line whose run it cannot find a turn of its own", () => {
    const outcome = { status: "cancelled" as const, cause: "cancelled" as const };
    const turns = buildTurns([msg(1, { role: "system", text: "stopped", outcome, run_id: "gone" })]);
    expect(turns).toHaveLength(1);
    expect(turns[0].outcome).toEqual(outcome);
  });
  it("presents lifecycle state without reading reasoning and keeps keepalives out of its clock", () => {
    const state = { ...EMPTY_LIVE, lastActivityAt: 1000, thinking: "hidden", tools: [{ id: "r", name: "Read", args: "{}", startedAt: 1000 }] };
    const turn = applyLive(null, state, 32_000);
    expect(activitySummary(turn, true, 32_000)).toEqual({ phase: "reading", steps: 1, lastActivityAt: 1000, staleSeconds: 31 });
    expect(liveAfter(state, "keepalive", {})).toBe(state);
  });
  it("keeps the exact assistant row for retry, including answers demoted to activity", () => {
    const [turn] = buildTurns([user(1, "go"), answer(2, "first note"), call(3, "tool"), result(4, "tool", "ok"), answer(5, "done")]);
    expect(turn.answerSeq).toBe(5);
    expect(turn.activity.find((item) => item.kind === "note")).toMatchObject({ seq: 2, text: "first note" });
    const [replacement] = buildTurns([user(1, "go"), answer(6, "replacement")], [turn]);
    expect(replacement.answerSeq).toBe(6);
    expect(replacement.activity).toEqual([]);
  });
  it("does not attach a new run to an old answer while its user row is still loading", () => {
    const turns = buildTurns([{ ...user(1, "go"), run_id: "first" }, { ...answer(2, "done"), run_id: "first" }]);
    expect(liveBase(turns, "second")).toBeNull();
    const after = buildTurns([...turns.flatMap((t) => t.user ? [t.user] : []), { ...answer(2, "done"), run_id: "first" }, { ...answer(3, "new"), run_id: "second" }]);
    expect(after).toHaveLength(2);
    expect(after[0].answer).toBe("done");
  });

  it("keeps received steering between the tool batches it interrupted", () => {
    const turns = buildTurns([user(1, "start"), call(2, "before"), result(3, "before", "ok"), user(4, "steer"), call(5, "after"), result(6, "after", "ok"), answer(7, "done")]);
    expect(turns.map((t) => t.toolIds)).toEqual([["before"], ["after"]]);
  });
  it("groups a user message with the work that followed it", () => {
    const turns = buildTurns([user(1, "go"), call(2, "c1"), result(3, "c1", "done"), answer(4, "finished")]);
    expect(turns).toHaveLength(1);
    expect(turns[0].user?.text).toBe("go");
    expect(turns[0].answer).toBe("finished");
    expect(turns[0].activity.filter((a) => a.kind === "tool")).toHaveLength(1);
    expect(turns[0].pendingTools).toBe(0);
    expect(turns[0].toolIds).toEqual(["c1"]);
  });

  it("counts a tool call whose result has not landed as pending", () => {
    const turns = buildTurns([user(1, "go"), call(2, "c1")]);
    expect(turns[0].pendingTools).toBe(1);
  });

  it("hands back the same objects when a new message is appended", () => {
    const history = [user(1, "one"), answer(2, "first"), user(3, "two"), answer(4, "second")];
    const before = buildTurns(history);
    const after = buildTurns([...history, user(5, "three"), answer(6, "third")], before);
    expect(after).toHaveLength(3);
    expect(after[0]).toBe(before[0]);
    expect(after[1]).toBe(before[1]);
    expect(after[2]).not.toBe(before[1]);
  });

  it("rebuilds only the turn a late tool result belongs to", () => {
    const history = [user(1, "one"), answer(2, "first"), user(3, "two"), call(4, "c4")];
    const before = buildTurns(history);
    expect(before[1].pendingTools).toBe(1);
    const after = buildTurns([...history, result(5, "c4", "output")], before);
    expect(after[0]).toBe(before[0]);
    expect(after[1]).not.toBe(before[1]);
    expect(after[1].pendingTools).toBe(0);
    expect((after[1].activity[0] as { result?: string }).result).toBe("output");
  });

  it("keeps a host compaction as a block of its own and opens a new turn after it", () => {
    const turns = buildTurns([user(1, "go"), answer(2, "done"), msg(3, { summary: true, text: "…", compaction: { reason: "auto" } }), answer(4, "after")]);
    expect(turns.map((t) => t.key)).toEqual(["u1", "s3", "a4"]);
    expect(turns[1].summary?.text).toBe("…");
    expect(liveBase(turns)?.key).toBe("a4");
  });

  it("does not continue a compaction block with the streaming turn", () => {
    const turns = buildTurns([user(1, "go"), answer(2, "done"), msg(3, { summary: true, text: "…", compaction: { reason: "auto" } })]);
    expect(liveBase(turns)).toBeNull();
  });

  it("puts a core compaction inside the turn it interrupted", () => {
    const turns = buildTurns([user(1, "go"), answer(2, "part"), msg(3, { summary: true, text: "…", compaction: { reason: "core" } }), answer(4, "rest")]);
    expect(turns).toHaveLength(1);
    expect(turns[0].activity.map((a) => a.kind)).toEqual(["note", "summary"]);
    expect(turns[0].answer).toBe("rest");
  });

  it("leaves internal and system messages out", () => {
    const turns = buildTurns([user(1, "go"), msg(2, { role: "system", text: "x" }), msg(3, { internal: true, text: "y" }), answer(4, "z")]);
    expect(turns).toHaveLength(1);
    expect(turns[0].answer).toBe("z");
  });
});

describe("applyLive", () => {
  it("does not demote the persisted final answer when its streamed copy overlaps", () => {
    const base = buildTurns([user(1, "go"), answer(2, "done")])[0];
    const turn = applyLive(base, { ...EMPTY_LIVE, text: "done", ended: true }, 5);
    expect(turn.answer).toBe("done");
    expect(turn.activity).toEqual([]);
  });

  it("drops the previous run's tools when a new run starts without an idle render", () => {
    const previous = { ...EMPTY_LIVE, runId: "first", tools: [{ id: "old", name: "Exec", args: "{}" }], text: "done" };
    const next = liveAfter(previous, "message_start", { run_id: "second" });
    expect(next.tools).toEqual([]);
    expect(next.text).toBe("");
    expect(next.runId).toBe("second");
    expect(liveAfter(next, "run_settled", { run_id: "first", housekeeping: false })).toBe(next);
  });
  it("merges the streamed answer into the settled tail without touching it", () => {
    const turns = buildTurns([user(1, "go")]);
    const live = applyLive(liveBase(turns), { ...EMPTY_LIVE, text: "half a sen" }, 5);
    expect(live.answer).toBe("half a sen");
    expect(live.key).toBe("u1");
    expect(turns[0].answer).toBe("");
    expect(turns[0].activity).toHaveLength(0);
  });

  it("cuts a trailing retrieval headline off the streamed text", () => {
    const live = applyLive(null, { ...EMPTY_LIVE, text: "the answer\n⟦index: a, b, c⟧" }, 5);
    expect(live.answer).toBe("the answer");
  });

  it("shows a streamed tool that is not in the history yet, once", () => {
    const turns = buildTurns([user(1, "go"), call(2, "c1")]);
    const state = { ...EMPTY_LIVE, tools: [{ id: "c1", name: "Exec", args: "{}" }, { id: "c9", name: "Read", args: '{"path":"a"}' }] };
    const live = applyLive(liveBase(turns), state, 5);
    const tools = live.activity.filter((a) => a.kind === "tool");
    expect(tools).toHaveLength(2);
    expect(live.pendingTools).toBe(2);
  });

  it("fills a persisted call with the result that streamed in", () => {
    const turns = buildTurns([user(1, "go"), call(2, "c1")]);
    const live = applyLive(liveBase(turns), { ...EMPTY_LIVE, tools: [{ id: "c1", name: "Exec", args: "{}", result: "ok" }] }, 5);
    const tool = live.activity[0] as { result?: string; running: boolean };
    expect(tool.result).toBe("ok");
    expect(tool.running).toBe(false);
    expect(live.pendingTools).toBe(0);
    expect((turns[0].activity[0] as { running: boolean }).running).toBe(true);
  });

  it("demotes an answer to a note once newer work streams in", () => {
    const turns = buildTurns([user(1, "go"), answer(2, "an earlier answer")]);
    const live = applyLive(liveBase(turns), { ...EMPTY_LIVE, text: "the next one" }, 5);
    expect(live.activity.map((a) => a.kind)).toEqual(["note"]);
    expect(live.answer).toBe("the next one");
  });
});

describe("reconcile", () => {
  const history = [user(1, "one"), answer(2, "first"), user(3, "two"), answer(4, "second")];

  it("appends what the tail added", () => {
    const tail = [history[3], user(5, "three")];
    const out = reconcile(history, tail);
    expect(out.gap).toBe(false);
    expect(out.messages.map((m) => m.seq)).toEqual([1, 2, 3, 4, 5]);
  });

  it("returns the same array when the tail says nothing new", () => {
    const out = reconcile(history, history.slice(2));
    expect(out.messages).toBe(history);
    expect(out.gap).toBe(false);
  });

  it("replaces a message the run has since filled in", () => {
    const grown = [{ ...history[3], text: "second, at last" }];
    const out = reconcile(history, grown);
    expect(out.messages).toHaveLength(4);
    expect(out.messages[3].text).toBe("second, at last");
  });

  it("asks for a whole read when the tail does not reach what is on screen", () => {
    expect(reconcile(history, [user(40, "much later")]).gap).toBe(true);
  });

  it("asks for a whole read when the history got shorter", () => {
    expect(reconcile(history, [history[0]]).gap).toBe(true);
  });

  it("asks for a whole read when a message carries no seq", () => {
    expect(reconcile(history, [{ ...history[3], seq: null }]).gap).toBe(true);
  });

  it("takes the tail as the history when nothing is on screen yet", () => {
    expect(reconcile([], history).messages).toHaveLength(4);
  });

  it("keeps the turn objects alive across a tail that changed nothing", () => {
    const turns = buildTurns(history);
    const out = reconcile(history, history.slice(3));
    expect(buildTurns(out.messages, turns)[0]).toBe(turns[0]);
  });

  // What the engine holds and the transcript has not written yet arrives with the row number it is
  // going to get and says it is live. Reading that as a hole in the history is what made every
  // event of a run re-read the whole session.
  const live = (seq: number, text: string) => msg(seq, { text, live: true });

  it("folds a live tail in without calling it a gap", () => {
    const out = reconcile(history, [history[3], live(5, "still writing")]);
    expect(out.gap).toBe(false);
    expect(out.messages).toHaveLength(5);
    expect(out.messages[4].text).toBe("still writing");
  });

  it("replaces the live tail it already holds instead of keeping both", () => {
    const once = reconcile(history, [live(5, "still")]);
    const twice = reconcile(once.messages, [live(5, "still writ")]);
    expect(twice.gap).toBe(false);
    expect(twice.messages).toHaveLength(5);
    expect(twice.messages[4].text).toBe("still writ");
  });

  it("takes the row over the live copy of it once it is written", () => {
    const once = reconcile(history, [live(5, "done")]);
    const settled = reconcile(once.messages, [answer(5, "done")]);
    expect(settled.gap).toBe(false);
    expect(settled.messages).toHaveLength(5);
    expect(settled.messages[4].live).toBeUndefined();
  });

  it("holds the array still when the live tail said the same thing twice", () => {
    const once = reconcile(history, [live(5, "still")]);
    const again = reconcile(once.messages, [history[3], live(5, "still")]);
    expect(again.messages).toBe(once.messages);
  });

  it("still asks for a whole read when a settled row carries no seq", () => {
    expect(reconcile(history, [{ ...history[3], seq: null }]).gap).toBe(true);
  });

  it("still asks for a whole read when the settled tail does not reach what is on screen", () => {
    expect(reconcile(history, [answer(40, "much later"), live(41, "…")]).gap).toBe(true);
  });
});

describe("older pages", () => {
  const history = [user(10, "one"), answer(11, "first")];

  it("recognises a page that really is older", () => {
    expect(isOlderPage([user(1, "x"), answer(2, "y")], 10)).toBe(true);
  });

  it("refuses a page an API without the cursor just repeated", () => {
    expect(isOlderPage(history, 10)).toBe(false);
    expect(isOlderPage([], 10)).toBe(false);
  });

  it("puts an older page in front and drops what is not older", () => {
    const out = prepend(history, [user(1, "x"), answer(11, "dup")]);
    expect(out.map((m) => m.seq)).toEqual([1, 10, 11]);
  });

  it("changes nothing when the page holds nothing older", () => {
    expect(prepend(history, [answer(11, "dup")])).toBe(history);
  });
});

describe("when the streaming turn ends", () => {
  /** A run of one tool step and one answer, as the stream carries it. */
  const run: [string, Record<string, any>][] = [
    ["message_start", {}],
    ["content_block_delta", { delta: { type: "thinking_delta", text: "the log is on the box" } }],
    ["tool_use_start", { tool_call_id: "c1", tool_name: "Exec" }],
    ["tool_use_stop", { tool_call_id: "c1", final_input: { command: "tail -n 40 log" } }],
    ["message_stop", { stop_reason: "tool_use" }],
    ["tool_result", { tool_call_id: "c1", content: "ok" }],
    ["message_start", {}],
    ["content_block_delta", { delta: { type: "text_delta", text: "One slow query, " } }],
    ["content_block_delta", { delta: { type: "text_delta", text: "on the events table." } }],
    ["message_stop", { stop_reason: "end_turn" }],
  ];

  const replay = (upto: number): LiveState => run.slice(0, upto).reduce((s, [event, p]) => liveAfter(s, event, p), EMPTY_LIVE);

  it("ends on the model's full stop and not on the one before it", () => {
    // The message that ended to call a tool is not the end of the turn: the run goes on.
    expect(replay(5).ended).toBe(false);
    expect(replay(run.length - 1).ended).toBe(false);
    const done = replay(run.length);
    expect(done.ended).toBe(true);
    // The text stays put: the written copy takes its place when the read of the transcript lands,
    // and until then the answer must not blink out.
    expect(done.text).toBe("One slow query, on the events table.");
  });

  it("is live again from the first event of the next run", () => {
    const next = liveAfter(replay(run.length), "message_start", {});
    expect(next.ended).toBe(false);
    expect(next.text).toBe("");
  });

  it("is not put back by anything the session does after the answer", () => {
    // These arrive in the gap between the last token and the session reporting itself idle: the
    // snapshot, the delivery to the other fronts, the record of what the run learned.
    const after: [string, Record<string, any>][] = [
      ["run_settled", { status: "completed", housekeeping: true }],
      ["state_changed", { from: "running", to: "running" }],
      ["run_settled", { status: "completed", housekeeping: false }],
    ];
    const state = after.reduce((s, [event, p]) => liveAfter(s, event, p), replay(run.length));
    expect(state.ended).toBe(true);
    expect(state.text).toBe("One slow query, on the events table.");
  });
});

describe("a long run's steps and the words between them", () => {
  // Nothing of a run is written to the transcript until it ends, so every message after the
  // operator's is live, and an event reads only the last TAIL of them. Twelve steps are enough to
  // outgrow that tail.
  const TAIL = 24;
  const live = (m: MessageView): MessageView => ({ ...m, live: true });
  const steps = (prefix: string, n: number, from: number): MessageView[] =>
    Array.from({ length: n }, (_, k) => [live(call(from + 2 * k, `${prefix}${k + 1}`)), live(result(from + 2 * k + 1, `${prefix}${k + 1}`, "ok"))]).flat();
  const start = user(1, "build it");
  const before = steps("a", 14, 2); // seq 2…29
  const words = live(msg(30, { text: "The screenshot caught the wrong window. Bringing it forward and", tool_calls: [{ id: "b1", name: "Exec", arguments: {} }, { id: "b2", name: "Exec", arguments: {} }] }));
  const wordsResults = [live(result(31, "b1", "ok")), live(result(32, "b2", "ok"))];
  const after = steps("c", 3, 33); // seq 33…38

  /** What the turn shows, in order: tool ids, and `note` where the words are. */
  const order = (turn: ReturnType<typeof applyLive>) => turn.activity.map((a) => (a.kind === "tool" ? a.id : a.kind));
  const streamed = (ids: string[], text = ""): LiveState => ({ ...EMPTY_LIVE, runId: undefined, text, tools: ids.map((id) => ({ id, name: "Exec", args: "{}", result: "ok" })) });
  const ids = (prefix: string, n: number) => Array.from({ length: n }, (_, k) => `${prefix}${k + 1}`);

  it("keeps the steps before the words above them once a short read no longer reaches them", () => {
    // The screen opened on the whole page while the words were streaming…
    let known = reconcile([], [start, ...before]).messages;
    const streaming = applyLive(liveBase(buildTurns(known)), streamed(ids("a", 14), "The screenshot caught"), Date.now());
    expect(order(streaming)).toEqual(ids("a", 14));
    expect(streaming.answer).toBe("The screenshot caught");
    // …then the words ended in two commands, and the steps went on. Each event reads a short tail.
    for (const upto of [words, ...wordsResults, ...after]) {
      const all = [start, ...before, words, ...wordsResults, ...after];
      const page = all.slice(0, all.indexOf(upto) + 1).slice(-TAIL);
      const merged = reconcile(known, page);
      expect(merged.gap).toBe(false);
      known = merged.messages;
    }
    const turn = applyLive(liveBase(buildTurns(known)), streamed([...ids("a", 14), "b1", "b2", ...ids("c", 3)]), Date.now());
    expect(order(turn)).toEqual([...ids("a", 14), "note", "b1", "b2", ...ids("c", 3)]);
    expect(turn.answer).toBe("");
  });

  it("stays in that order when the run ends and its rows are written", () => {
    const all = [start, ...before, words, ...wordsResults, ...after];
    const known = reconcile([], all).messages;
    const written = all.map((m) => ({ ...m, live: undefined }));
    const end = reconcile(known, written.slice(-TAIL));
    // The written tail reaches back past what was settled: the screen reads the whole page again,
    // and that page is the same order.
    expect(end.gap).toBe(true);
    const [turn] = buildTurns(reconcile([], written).messages);
    expect(order(turn)).toEqual([...ids("a", 14), "note", "b1", "b2", ...ids("c", 3)]);
  });

  it("matches the live messages by who they are when a queued message takes a row in the middle", () => {
    const all = [start, ...before, words];
    let known = reconcile([], all).messages;
    // The operator queues a message: it is written at once (hidden), and every live number moves up.
    const queuedRow = { ...user(2, "later"), internal: true };
    const shifted = all.slice(1).map((m) => ({ ...m, seq: m.seq! + 1 }));
    known = reconcile(known, [start, queuedRow, ...shifted].slice(-TAIL)).messages;
    const [turn] = buildTurns(known);
    expect(order(turn)).toEqual([...ids("a", 14), "note", "b1", "b2"]);
  });

  it("does not put a streamed step the history has not read below the steps that came after it", () => {
    // The turn's first steps are on a page the screen has not read: the stream still carries them.
    const known = [start, ...before.slice(8), words];
    const turn = applyLive(liveBase(buildTurns(known)), streamed([...ids("a", 14), "b1", "b2", "c1"]), Date.now());
    expect(order(turn)).toEqual([...ids("a", 14).slice(4), "note", "b1", "b2", "c1"]);
  });
});
