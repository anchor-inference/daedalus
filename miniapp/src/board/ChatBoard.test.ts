// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { boardNow, boardTally } from "./ChatBoard";
import type { BoardTask } from "./tasks";

const task = (id: string, status: BoardTask["status"], extra: Partial<BoardTask> = {}): BoardTask => ({
  id, title: `Card ${id}`, status, priority: 3, acceptance: "", checklist: [], depends_on: [], session_id: "s1", origin_session_id: "s1",
  notes: "", created_at: "2026-11-20T10:00:00Z", updated_at: "2026-11-24T10:00:00Z", ...extra,
});

beforeEach(() => setLang("en"));

describe("how much of a chat's board is done", () => {
  it("counts done and dropped cards as finished, of every card", () => {
    expect(boardTally([task("a", "done"), task("b", "dropped"), task("c", "doing"), task("d", "todo"), task("e", "review")])).toEqual({ done: 2, all: 5, open: 3, percent: 40 });
  });
  it("is empty, not undefined, for a chat that has none and before the listing is read", () => {
    expect(boardTally([])).toEqual({ done: 0, all: 0, open: 0, percent: 0 });
    expect(boardTally(undefined)).toEqual({ done: 0, all: 0, open: 0, percent: 0 });
  });
});

describe("what the Overview's Board row says the chat is on", () => {
  it("names the card in work first, then one in review, then the next to do", () => {
    expect(boardNow([task("a", "todo"), task("b", "review"), task("c", "doing")])).toBe("now: Card c");
    expect(boardNow([task("a", "todo"), task("b", "review")])).toBe("now: Card b");
    expect(boardNow([task("a", "done"), task("b", "todo")])).toBe("now: Card b");
  });
  it("takes the most urgent of the cards in work", () => {
    expect(boardNow([task("a", "doing", { priority: 4 }), task("b", "doing", { priority: 1 })])).toBe("now: Card b");
  });
  it("says everything is finished once nothing is open", () => {
    expect(boardNow([task("a", "done"), task("b", "dropped")])).toBe("everything is finished");
  });
});
