// @vitest-environment jsdom
// The agent's language: what its row says while translating and while not (the menu itself is held
// by the access chip's test), and a translated answer that arrives.

import { describe, expect, it } from "vitest";
import { agentLangLabel } from "./agentlang";
import { reconcile, buildTurns } from "./turns";
import type { MessageView } from "./api";

describe("the label", () => {
  it("names the pair while translating and the operator's language alone otherwise", () => {
    expect(agentLangLabel({ agent: "en", operator: "ru", active: true, explicit: true })).toBe("EN ⇄ RU");
    expect(agentLangLabel({ agent: "", operator: "ru", active: false, explicit: false })).toBe("RU");
    // The same language on both sides translates nothing, so it reads as off.
    expect(agentLangLabel({ agent: "ru", operator: "ru", active: false, explicit: true })).toBe("RU");
  });
});

describe("a translation that arrives", () => {
  const answer = (state: "pending" | "done", text: string): MessageView => ({
    role: "assistant", seq: 2, run_id: "r1", text, thinking: "", tool_calls: [], tool_results: [], created_at: "2026-11-17T10:00:01Z",
    translation: state === "done" ? { direction: "in", state, from: "en", to: "ru", alternate: "Done." } : { direction: "in", state, from: "en", to: "ru" },
  });
  const user: MessageView = { role: "user", seq: 1, run_id: "r1", origin: "operator", text: "Сделай", thinking: "", tool_calls: [], tool_results: [], created_at: "2026-11-17T10:00:00Z" };

  it("replaces the pending row even when the text happens to be the same", () => {
    const merged = reconcile([user, answer("pending", "Done.")], [user, answer("done", "Done.")]);
    expect(merged.messages[1].translation?.state).toBe("done");
    const before = buildTurns([user, answer("pending", "Done.")], []);
    const after = buildTurns([user, answer("done", "Done.")], before);
    expect(after[0]).not.toBe(before[0]);
    expect(after[0].answerTranslation?.state).toBe("done");
  });
});
