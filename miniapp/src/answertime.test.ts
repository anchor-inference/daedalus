import { afterEach, describe, expect, it, vi } from "vitest";
import type { MessageView } from "./api";
import { fullTime, stamp } from "./format";
import { setLang } from "./i18n";
import { minuteNow, subscribeMinute } from "./minute";
import { answerTime, applyLive, buildTurns, closingText, EMPTY_LIVE, splitActivity, type Turn } from "./turns";

const T0 = Date.parse("2026-11-05T10:00:00Z");

/** A message `s` seconds after T0. */
function msg(seq: number, s: number, over: Partial<MessageView> = {}): MessageView {
  return { role: "assistant", seq, text: "", thinking: "", tool_calls: [], tool_results: [], created_at: new Date(T0 + s * 1000).toISOString(), ...over };
}
const user = (seq: number, s: number, text: string, over: Partial<MessageView> = {}) => msg(seq, s, { role: "user", text, ...over });
const call = (seq: number, s: number, id: string, text = "") => msg(seq, s, { text, tool_calls: [{ id, name: "Exec", arguments: {} }] });
const result = (seq: number, s: number, id: string) => msg(seq, s, { role: "tool", tool_results: [{ id, content: "ok", is_error: false }] });

/** What the transcript shows for a settled turn, split into replies as the chat does by default. */
function timeOf(turn: Turn, live = false) {
  const segments = splitActivity(turn.activity);
  return answerTime(turn, closingText(turn, segments, live), live);
}

describe("the time of a turn's final answer", () => {
  it("is the answer's own message, after intermediate texts and tool calls", () => {
    const [turn] = buildTurns([user(1, 0, "go"), call(2, 5, "c1", "Let me look first."), result(3, 9, "c1"), msg(4, 12, { thinking: "so" }), msg(5, 134, { text: "Done." })]);
    expect(turn.answer).toBe("Done.");
    expect(timeOf(turn)).toEqual({ at: T0 + 134_000, took: 134_000 });
    // The text the agent wrote between its steps has a time of its own, which the transcript does not show.
    const note = turn.activity.find((a) => a.kind === "note");
    expect(note).toMatchObject({ text: "Let me look first.", at: T0 + 5000 });
  });

  it("moves with a text that turned out not to be final", () => {
    const [turn] = buildTurns([user(1, 0, "go"), msg(2, 3, { text: "First thought." }), call(3, 4, "c1"), result(4, 5, "c1"), msg(5, 60, { text: "Final." })]);
    expect(turn.activity[0]).toMatchObject({ kind: "note", text: "First thought.", at: T0 + 3000 });
    expect(timeOf(turn)?.at).toBe(T0 + 60_000);
  });

  it("is the closing reply's when the turn ended on a step", () => {
    const [turn] = buildTurns([user(1, 0, "send it"), call(2, 20, "c1", "Here is the file."), result(3, 21, "c1")]);
    expect(turn.answer).toBe("");
    const segments = splitActivity(turn.activity);
    expect(closingText(turn, segments, false)?.text).toBe("Here is the file.");
    expect(timeOf(turn)).toEqual({ at: T0 + 20_000, took: 20_000 });
  });

  it("goes to the turn a steer opened, not the one it cut short", () => {
    const turns = buildTurns([
      user(1, 0, "start", { run_id: "r" }),
      msg(2, 5, { text: "Working on it.", run_id: "r" }),
      user(3, 30, "also do this", { run_id: "r", delivery: "steer" }),
      msg(4, 31, { tool_calls: [{ id: "c", name: "Exec", arguments: {} }], run_id: "r" }),
      result(5, 40, "c"),
      msg(6, 90, { text: "Both done.", run_id: "r" }),
    ]);
    expect(turns).toHaveLength(2);
    expect(turns[0].steered).toBe(true);
    expect(timeOf(turns[0])).toBeNull();
    // The duration is from the steer: the message that turn answers.
    expect(timeOf(turns[1])).toEqual({ at: T0 + 90_000, took: 60_000 });
  });

  it("is absent for a turn that broke off without answering", () => {
    const outcome = { status: "cancelled" as const, cause: "cancelled" as const };
    const [turn] = buildTurns([user(1, 0, "go", { run_id: "r" }), call(2, 5, "c1", "Looking."), result(3, 6, "c1"), msg(4, 7, { role: "system", text: "stopped", outcome, run_id: "r" })]);
    expect(turn.outcome).toEqual(outcome);
    expect(timeOf(turn)).toBeNull();
    // A run that failed after it had answered still answered.
    const [answered] = buildTurns([user(1, 0, "go", { run_id: "r" }), msg(2, 8, { text: "Here.", run_id: "r" }), msg(3, 9, { role: "system", text: "failed", outcome: { status: "failed", cause: "compaction", answered: true }, run_id: "r" })]);
    expect(timeOf(answered)?.at).toBe(T0 + 8000);
  });

  it("is absent while the turn is still being written", () => {
    const [base] = buildTurns([user(1, 0, "go"), msg(2, 4, { text: "Partly" })]);
    const streaming = applyLive(base, { ...EMPTY_LIVE, startedAt: T0, text: "Partly written" }, T0 + 5000);
    expect(timeOf(streaming, true)).toBeNull();
    expect(answerTime(base, null, true)).toBeNull();
  });

  it("has no duration when the turn did not open with a message", () => {
    const [turn] = buildTurns([msg(1, 10, { text: "A run of its own.", run_id: "x" })]);
    expect(timeOf(turn)).toEqual({ at: T0 + 10_000, took: null });
  });
});

describe("a message's time", () => {
  afterEach(() => setLang("en"));
  const now = Date.parse("2026-11-05T15:00:00");

  it("reads as an age while recent, then as a clock, then as a date", () => {
    setLang("en");
    expect(stamp(now - 20_000, now)).toBe("now");
    expect(stamp(now - 5 * 60_000, now)).toBe("5m");
    expect(stamp(now - 3 * 3600_000, now)).toBe("3h");
    // Past a few hours, earlier today: the clock.
    expect(stamp(Date.parse("2026-11-05T08:10:00"), now)).toMatch(/0?8:10/);
    // Another day: the date with the time.
    const older = stamp(Date.parse("2026-11-01T08:10:00"), now);
    expect(older).toMatch(/Nov/);
    expect(older).toMatch(/0?8:10/);
  });

  it("is said in the reader's language", () => {
    setLang("ru");
    expect(stamp(now - 20_000, now)).toBe("сейчас");
    expect(stamp(now - 5 * 60_000, now)).toBe("5 м");
    expect(stamp(now - 3 * 3600_000, now)).toBe("3 ч");
  });

  it("reads a time ahead of the browser's clock as now", () => {
    setLang("en");
    expect(stamp(now + 40_000, now)).toBe("now");
    expect(stamp("not a date", now)).toBe("");
  });

  it("has the whole date and time for its tooltip", () => {
    setLang("en");
    const whole = fullTime(Date.parse("2026-11-05T08:10:03"));
    expect(whole).toMatch(/2026/);
    expect(whole).toMatch(/Thursday/);
    expect(whole).toMatch(/0?8:10:03/);
  });
});

describe("the minute clock", () => {
  afterEach(() => vi.useRealTimers());

  it("is one timer for every reader, ticking on the minute, and stops with the last reader", () => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.parse("2026-11-05T15:00:30Z"));
    const a = vi.fn();
    const b = vi.fn();
    const offA = subscribeMinute(a);
    const offB = subscribeMinute(b);
    expect(vi.getTimerCount()).toBe(1);
    vi.advanceTimersByTime(30_000);
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
    expect(minuteNow()).toBe(Date.parse("2026-11-05T15:01:00Z"));
    vi.advanceTimersByTime(60_000);
    expect(a).toHaveBeenCalledTimes(2);
    offA();
    offB();
    expect(vi.getTimerCount()).toBe(0);
  });
});
