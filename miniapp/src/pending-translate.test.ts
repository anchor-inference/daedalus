// @vitest-environment jsdom
// The rows of messages on their way: this window's sends and the host's list of messages still with the
// translator, which is all a window opened while one was being translated has to draw it from.
import { describe, expect, it } from "vitest";
import { heldAnswer, pendingRows } from "./screens/Session";

const listed = (id: string, text = "слова") => ({ id, text, from: "ru", to: "en", at: "2026-11-17T12:00:00+00:00" });

describe("pendingRows", () => {
  it("draws a send as translating while its request is out, before the host has said anything", () => {
    expect(pendingRows([{ id: "a", text: "слова", translating: "en", acked: false }], [])).toEqual([{ id: "a", text: "слова", translating: "en" }]);
  });

  it("keeps an acknowledged send translating only while the host lists it", () => {
    const own = [{ id: "a", text: "слова", translating: "en", acked: true }];
    expect(pendingRows(own, [listed("a")])[0].translating).toBe("en");
    expect(pendingRows(own, [])[0].translating).toBe("");
  });

  it("draws the host's messages a reloaded window has no send of its own for", () => {
    expect(pendingRows([], [listed("b", "А на macOS?")])).toEqual([{ id: "b", text: "А на macOS?", translating: "en" }]);
  });

  it("never draws one message twice", () => {
    expect(pendingRows([{ id: "a", text: "слова", translating: "en", acked: true }], [listed("a")])).toHaveLength(1);
  });

  it("leaves answers to the question card and side answers to the Questions tab", () => {
    const host = [{ ...listed("answer:call_1", "Оба"), kind: "answer" as const, question: "call_1" }, { ...listed("side:b1", "оставь"), kind: "side" as const, ask_ids: ["ask-1"] }, { ...listed("m", "А тесты?"), kind: "message" as const }];
    expect(pendingRows([], host).map((row) => row.id)).toEqual(["m"]);
    expect(heldAnswer(host)).toEqual({ text: "Оба", to: "en" });
    expect(heldAnswer([listed("m")])).toBeNull();
  });
});
