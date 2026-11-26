// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import type { SessionSummary } from "../api";
import { resumeNote, resumeOrder, resumeState } from "./resume";

let n = 0;
function chat(patch: Partial<SessionSummary>): SessionSummary {
  n += 1;
  return { id: `s${n}`, title: `Chat ${n}`, status: "idle", created_at: "2026-11-01T00:00:00Z", last_message_at: "2026-11-20T00:00:00Z", run_id: null, project_id: "p1", project: "Bakery", ...patch };
}

describe("the chats the start page offers", () => {
  it("reads an idle chat with an unread result as done, and one already read as idle", () => {
    expect(resumeState(chat({ unread_result: true }))).toBe("done");
    expect(resumeState(chat({}))).toBe("idle");
  });

  it("puts what needs the operator first, then failures, work, finished results, loops, and the rest by recency", () => {
    const older = chat({ last_message_at: "2026-11-01T00:00:00Z" });
    const newer = chat({ last_message_at: "2026-11-24T00:00:00Z" });
    const done = chat({ unread_result: true, last_message_at: "2026-11-02T00:00:00Z" });
    const working = chat({ status: "running" });
    const failed = chat({ status: "failed" });
    const waiting = chat({ status: "waiting", last_message_at: "2026-10-01T00:00:00Z" });
    const loop = chat({ metadata: { loop: { status: "active" } as never } });
    const order = resumeOrder([older, newer, done, working, failed, waiting, loop], "", 20).map((row) => row.s.id);
    expect(order).toEqual([waiting, failed, working, done, loop, newer, older].map((s) => s.id));
  });

  it("leaves out helpers and archived chats, narrows to a project, and stops at the limit", () => {
    const keep = chat({ project_id: "p2" });
    const rows = [chat({ metadata: { subagent_of: "x" } }), chat({ archived: true }), keep, chat({ project_id: "p3" })];
    expect(resumeOrder(rows, "p2", 5).map((row) => row.s.id)).toEqual([keep.id]);
    expect(resumeOrder(rows, "", 1)).toHaveLength(1);
  });

  it("says the state in words and keeps the model after it", () => {
    expect(resumeNote(chat({ model: "deepseek/flash" }), "waiting")).toContain("Waiting for your answer");
    expect(resumeNote(chat({}), "idle")).toBe("");
  });
});
