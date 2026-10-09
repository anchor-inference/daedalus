// Which model answered, as the screen works it out. The turn reads it off the message the host
// stamped; the live turn reads it off the event that said the run had moved. Both have to agree,
// and both have to fall silent again the moment the configured model takes the run back — a note
// that outlives what it describes is worse than no note, because the reader trusts it.

import { describe, expect, it } from "vitest";
import type { MessageView } from "./api";
import { DICT, LANGS, t } from "./i18n";
import { applyLive, buildTurns, EMPTY_LIVE, liveAfter } from "./turns";

let clock = 1_700_000_000_000;

function msg(seq: number, over: Partial<MessageView> = {}): MessageView {
  clock += 1000;
  return { role: "assistant", seq, text: "", thinking: "", tool_calls: [], tool_results: [], created_at: new Date(clock).toISOString(), ...over };
}

const user = (seq: number, text: string) => msg(seq, { role: "user", text });

describe("the turn names the model that wrote it", () => {
  it("carries the stamp and the fallback onto the turn", () => {
    const turns = buildTurns([user(1, "go"), msg(2, { text: "done", model: "flash", fallback: { from: "opus-5", to: "flash", reason: "rate_limit" } })]);
    expect(turns[0].model).toBe("flash");
    expect(turns[0].fallback).toEqual({ from: "opus-5", to: "flash", reason: "rate_limit" });
  });

  it("says nothing about a turn the configured model wrote", () => {
    const turns = buildTurns([user(1, "go"), msg(2, { text: "done", model: "opus-5" })]);
    expect(turns[0].model).toBe("opus-5");
    expect(turns[0].fallback).toBeNull();
  });

  it("is named by the message that finished it, not the one that started it", () => {
    const turns = buildTurns([
      user(1, "go"),
      msg(2, { text: "thinking about it", model: "opus-5" }),
      msg(3, { text: "done", model: "flash", fallback: { from: "opus-5", to: "flash", reason: "outage" } }),
    ]);
    expect(turns[0].model).toBe("flash");
    expect(turns[0].fallback?.reason).toBe("outage");
  });

  it("rebuilds the turn when only the model changed", () => {
    const history = [user(1, "go"), msg(2, { text: "done", model: "opus-5" })];
    const before = buildTurns(history);
    const moved = [history[0], { ...history[1], model: "flash", fallback: { from: "opus-5", to: "flash", reason: "outage" } }];
    const after = buildTurns(moved, before);
    expect(after[0]).not.toBe(before[0]);
    expect(after[0].fallback?.to).toBe("flash");
  });
});

describe("the live turn follows the event stream", () => {
  it("takes the model and the fallback off model_changed", () => {
    const state = liveAfter(EMPTY_LIVE, "model_changed", { to: "flash", configured: "opus-5", reason: "rate_limit", fallback: true });
    expect(state.model).toBe("flash");
    expect(state.fallback).toEqual({ from: "opus-5", to: "flash", reason: "rate_limit" });
  });

  it("clears the note when the configured model answers again", () => {
    const fell = liveAfter(EMPTY_LIVE, "model_changed", { to: "flash", configured: "opus-5", reason: "outage", fallback: true });
    const back = liveAfter(fell, "model_changed", { to: "opus-5", configured: "opus-5", reason: "chain_step", fallback: false });
    expect(back.model).toBe("opus-5");
    expect(back.fallback).toBeNull();
  });

  it("leaves the state alone for an event that says nothing about the model", () => {
    const state = liveAfter(EMPTY_LIVE, "hook_fired", {});
    expect(state).toBe(EMPTY_LIVE);
  });

  it("puts the live model on the turn the screen draws", () => {
    const base = buildTurns([user(1, "go")])[0];
    const live = liveAfter({ ...EMPTY_LIVE, text: "half an answer" }, "model_changed", { to: "flash", configured: "opus-5", reason: "outage", fallback: true });
    const turn = applyLive(base, live, Date.now());
    expect(turn.model).toBe("flash");
    expect(turn.fallback?.from).toBe("opus-5");
  });
});

describe("what the chip says", () => {
  it("has a word for every reason the host can send", () => {
    for (const reason of ["outage", "stalled", "rate_limit", "chain_step", "live_override"]) {
      expect(DICT[`session.model.reason.${reason}`]).toBeDefined();
      for (const lang of LANGS) expect(DICT[`session.model.reason.${reason}`][lang]).not.toBe("");
    }
  });

  it("names both models in the line above the answer", () => {
    const line = t("session.model.fallback.turn", { to: "flash", from: "opus-5" });
    expect(line).toContain("flash");
    expect(line).toContain("opus-5");
  });

  it("names the configured model behind the stand-in's name in the pill", () => {
    const label = t("session.model.fallback", { from: "opus-5" });
    expect(label).toBe("← opus-5");
  });
});
