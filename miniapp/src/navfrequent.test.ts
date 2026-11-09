// The launcher's Frequent tiles: pinned first and fixed, then what was opened most this month, then
// the shipped fallback; and nothing the operator hid ever comes back through them.

import { describe, expect, it } from "vitest";
import { EMPTY, MAX_IDS, WINDOW_DAYS, type FrequentState, dayOf, frequent, parse, prune, recordVisit, score, togglePin } from "./navfrequent";

const DAY = 86_400_000;
const NOW = Date.UTC(2026, 10, 10, 12);
const visit = (state: FrequentState, id: string, times: number, daysAgo = 0) => {
  let next = state;
  for (let i = 0; i < times; i += 1) next = recordVisit(next, id, NOW - daysAgo * DAY);
  return next;
};
const ALL = ["inbox", "board", "usage", "memory", "changes", "services", "health", "page:dosing"];

describe("learning from use", () => {
  it("counts each visit on its day", () => {
    const state = visit(visit(EMPTY, "usage", 2), "usage", 1, 3);
    expect(state.visits.usage).toEqual({ [String(dayOf(NOW))]: 2, [String(dayOf(NOW) - 3)]: 1 });
  });

  it("orders by use, a recent visit outweighing an old one", () => {
    let state = visit(EMPTY, "memory", 3, 25);
    state = visit(state, "usage", 2, 1);
    state = visit(state, "health", 1);
    // memory: 3 × (1 − 25/30) = 0.5; usage: 2 × (1 − 1/30) ≈ 1.93; health: 1.
    expect(frequent(state, ALL, NOW, 4)).toEqual(["usage", "health", "memory"]);
  });

  it("forgets what is older than the window", () => {
    const state = visit(EMPTY, "memory", 9, WINDOW_DAYS);
    expect(score(state.visits.memory, NOW)).toBe(0);
    expect(prune(state, NOW).visits.memory).toBeUndefined();
    expect(frequent(state, ALL, NOW, 4)).toEqual([]);
  });

  it("breaks a tie by the later visit, then by the catalogue's order", () => {
    let state = visit(EMPTY, "health", 1);
    state = visit(state, "board", 1);
    expect(frequent(state, ALL, NOW, 4)).toEqual(["board", "health"]);
  });

  it("keeps the history bounded, dropping the least used first", () => {
    let state = EMPTY;
    for (let i = 0; i <= MAX_IDS; i += 1) state = visit(state, `page:p${i}`, i === 0 ? 1 : 2);
    expect(Object.keys(state.visits)).toHaveLength(MAX_IDS);
    expect(state.visits["page:p0"]).toBeUndefined();
  });
});

describe("pins", () => {
  it("come first, in the order they were pinned, and stay put whatever is used", () => {
    let state = togglePin(togglePin(EMPTY, "services"), "inbox");
    state = visit(state, "usage", 20);
    expect(frequent(state, ALL, NOW, 4)).toEqual(["services", "inbox", "usage"]);
  });

  it("toggle off and leave the others where they were", () => {
    const state = togglePin(togglePin(togglePin(EMPTY, "a"), "b"), "a");
    expect(state.pins).toEqual(["b"]);
  });

  it("past the limit are kept but not drawn", () => {
    const state = ["inbox", "board", "usage", "memory", "health"].reduce(togglePin, EMPTY);
    expect(frequent(state, ALL, NOW, 4)).toEqual(["inbox", "board", "usage", "memory"]);
    expect(frequent(togglePin(state, "board"), ALL, NOW, 4)).toEqual(["inbox", "usage", "memory", "health"]);
  });
});

describe("what may be offered", () => {
  it("is only what is shown: a hidden item never returns through a pin or its history", () => {
    let state = togglePin(EMPTY, "board");
    state = visit(state, "usage", 5);
    const shown = ALL.filter((id) => id !== "board" && id !== "usage");
    expect(frequent(state, shown, NOW, 4, ["memory", "board", "health"])).toEqual(["memory", "health"]);
  });

  it("fills the rest from the fallback while the history is thin", () => {
    const state = visit(EMPTY, "inbox", 1);
    expect(frequent(state, ALL, NOW, 4, ["usage", "inbox", "memory", "changes", "services"])).toEqual(["inbox", "usage", "memory", "changes"]);
  });
});

describe("what was stored", () => {
  it("survives a round trip", () => {
    const state = togglePin(visit(EMPTY, "usage", 2), "memory");
    expect(parse(JSON.stringify(state))).toEqual(state);
  });

  it("is read defensively", () => {
    expect(parse(null)).toEqual(EMPTY);
    expect(parse("not json")).toEqual(EMPTY);
    expect(parse("[1,2]")).toEqual(EMPTY);
    expect(parse(JSON.stringify({ visits: { usage: { "20000": "x", abc: 2, "20001": 3 }, bad: 4 }, pins: ["a", 1, "a", ""] }))).toEqual({ visits: { usage: { "20001": 3 } }, pins: ["a"] });
  });
});
