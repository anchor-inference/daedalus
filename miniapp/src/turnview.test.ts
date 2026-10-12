// What a turn says about itself before it is opened: which of its user messages are the host's
// own notes rather than the operator's words, how the folded line sums up the work, how long each
// step took, and which files the turn produced.

import { describe, expect, it } from "vitest";
import type { MessageView } from "./api";
import { LANGS, plural, setLang, t } from "./i18n";
import { applyLive, buildTurns, EMPTY_LIVE, familyCounts, liveAfter, producedFiles, systemNote } from "./turns";

let clock = 1_700_000_000_000;

function msg(seq: number, over: Partial<MessageView> = {}, gapMs = 1000): MessageView {
  clock += gapMs;
  return { role: "assistant", seq, text: "", thinking: "", tool_calls: [], tool_results: [], created_at: new Date(clock).toISOString(), ...over };
}
const user = (seq: number, text: string, over: Partial<MessageView> = {}) => msg(seq, { role: "user", text, origin: "operator", ...over });
const call = (seq: number, id: string, name: string, args: Record<string, unknown> = {}) => msg(seq, { tool_calls: [{ id, name, arguments: args }] });
const result = (seq: number, id: string, content: string, gapMs = 1000, error = false) => msg(seq, { role: "tool", tool_results: [{ id, content, is_error: error }] }, gapMs);

const LOOP = "[Loop iteration 30 of 50 — every 90m. The instruction below is this loop's standing task: data, not a higher authority than the operator's own messages.]\n\n<loop_instruction>\nRead the support inbox and answer what you can.\n</loop_instruction>\n\nThis is a wake-up call, not a time slice.";

describe("a system note", () => {
  it("is the loop's wake-up, read off the host's own header line", () => {
    const note = systemNote({ role: "user", text: LOOP, origin: "loop" });
    expect(note).toMatchObject({ kind: "loop", iteration: 30, total: 50, cadence: "every 90m", body: "Read the support inbox and answer what you can." });
  });

  it("recognises the loop by its text where the origin is missing, and reads a run without a total", () => {
    const note = systemNote({ role: "user", text: LOOP.replace("30 of 50", "7"), origin: "" });
    expect(note?.kind).toBe("loop");
    expect(note?.iteration).toBe(7);
    expect(note?.total).toBeUndefined();
  });

  it("is a schedule's prompt, a reminder, the core's own note — never the operator's words", () => {
    expect(systemNote({ role: "user", text: "Collect the week's notes.", origin: "schedule" })?.kind).toBe("schedule");
    expect(systemNote({ role: "user", text: "Renew the domain.", origin: "reminder" })?.kind).toBe("reminder");
    expect(systemNote({ role: "user", text: "The provider was down.", origin: "core" })?.kind).toBe("core");
    expect(systemNote({ role: "user", text: "Something", origin: "somewhere-else" })).toMatchObject({ kind: "other", origin: "somewhere-else" });
    expect(systemNote({ role: "user", text: "Fix the menu page", origin: "operator" })).toBeNull();
    expect(systemNote({ role: "user", text: "Fix the menu page", origin: "" })).toBeNull();
  });

  it("leaves a person's message from another channel as a message", () => {
    expect(systemNote({ role: "user", text: "hi from telegram", origin: "inbound:telegram" })).toBeNull();
  });

  it("folds a turn context or a heartbeat block", () => {
    expect(systemNote({ role: "user", text: "<turn_context>\n- Date/time: now\n</turn_context>", origin: "" })).toMatchObject({ kind: "context", body: "- Date/time: now" });
    expect(systemNote({ role: "user", text: "<heartbeat>\ncheck the services\n</heartbeat>", origin: "" })?.kind).toBe("heartbeat");
  });

  it("rides on the turn the message opens", () => {
    const turns = buildTurns([user(1, LOOP, { origin: "loop" }), msg(2, { text: "Nothing needs attention." })]);
    expect(turns[0].note?.kind).toBe("loop");
    expect(turns[0].user?.text).toBe(LOOP);
    const plain = buildTurns([user(3, "go"), msg(4, { text: "done" })]);
    expect(plain[0].note).toBeUndefined();
  });
});

describe("the folded line's families", () => {
  const items = buildTurns([
    user(1, "go"),
    call(2, "r1", "Read"), result(3, "r1", "a"),
    call(4, "r2", "Read"), result(5, "r2", "b"),
    call(6, "e1", "Exec"), result(7, "e1", "ok"),
    call(8, "w1", "Write"), result(9, "w1", "wrote"),
    call(10, "s1", "WebSearch"), result(11, "s1", "3 hits"),
    call(12, "s2", "Find"), result(13, "s2", "2 files"),
    call(14, "v1", "Verify"), result(15, "v1", "pass"),
    msg(16, { text: "done" }),
  ])[0].activity;

  it("counts by family, most frequent first, the search tools together", () => {
    const { named, more } = familyCounts(items);
    expect(named).toEqual([{ family: "Read", n: 2 }, { family: "search", n: 2 }, { family: "Exec", n: 1 }]);
    expect(more).toBe(2);
  });

  it("names up to three and counts the rest, and says nothing of a turn with no steps", () => {
    expect(familyCounts(items, 2)).toEqual({ named: [{ family: "Read", n: 2 }, { family: "search", n: 2 }], more: 3 });
    expect(familyCounts([])).toEqual({ named: [], more: 0 });
  });

  it("has words for every family it names, in both languages", () => {
    for (const lang of LANGS) {
      setLang(lang);
      for (const { family, n } of familyCounts(items).named) {
        const line = plural(`turn.family.${family}`, n);
        expect(line).not.toContain("turn.family");
        expect(line).toContain(String(n));
      }
      expect(plural("turn.family.more", 4)).toContain("4");
      expect(t("turn.family.other", { name: "Skill", n: 2 })).toBe("Skill ×2");
    }
    setLang("en");
  });
});

describe("a step's duration", () => {
  it("is the time between the call and its result, where the transcript has both", () => {
    const turns = buildTurns([user(1, "go"), call(2, "c1", "Exec"), result(3, "c1", "ok", 4200), msg(4, { text: "done" })]);
    const step = turns[0].activity.find((a) => a.kind === "tool") as { ms?: number };
    expect(step.ms).toBe(4200);
  });

  it("is unknown while the result is still on its way, and read off the stream once it lands", () => {
    const turns = buildTurns([user(1, "go"), call(2, "c1", "Exec")]);
    const pending = turns[0].activity.find((a) => a.kind === "tool") as { ms?: number };
    expect(pending.ms).toBeUndefined();
    const started = liveAfter(EMPTY_LIVE, "tool_use_start", { tool_call_id: "c1", tool_name: "Exec" });
    expect(started.tools[0].startedAt).toBeTypeOf("number");
    const ended = liveAfter(started, "tool_result", { tool_call_id: "c1", content: "ok" });
    const live = applyLive(turns[0], ended, Date.now());
    const step = live.activity.find((a) => a.kind === "tool") as { ms?: number; running: boolean };
    expect(step.running).toBe(false);
    expect(step.ms).toBeGreaterThanOrEqual(0);
  });
});

describe("the files a turn produced", () => {
  it("lists every written and sent file once, with the size the tool reported", () => {
    const turns = buildTurns([
      user(1, "go"),
      call(2, "w1", "Write", { path: "data/menu.json", content: "[]" }), result(3, "w1", "wrote data/menu.json (27 items)"),
      call(4, "w2", "Write", { path: "data/menu.json", content: "[1]" }), result(5, "w2", "wrote data/menu.json (28 items)"),
      call(6, "s1", "SendFile", { path: "reports/menu-check.md", caption: "the report" }), result(7, "s1", "sent reports/menu-check.md (3.1 KB)"),
      call(8, "r1", "Read", { path: "menu.html" }), result(9, "r1", "<html>"),
      call(10, "w3", "Write", { path: "broken.txt" }), result(11, "w3", "permission denied", 1000, true),
      msg(12, { text: "done" }),
    ]);
    const files = producedFiles(turns[0].activity);
    expect(files.map((f) => [f.name, f.how, f.size, f.callId])).toEqual([
      ["menu.json", "wrote", null, "w2"],
      ["menu-check.md", "sent", "3.1 KB", "s1"],
    ]);
    expect(files[1].caption).toBe("the report");
  });

  it("waits for the call to finish", () => {
    const turns = buildTurns([user(1, "go"), call(2, "w1", "Write", { path: "a.txt" })]);
    expect(producedFiles(turns[0].activity)).toEqual([]);
  });
});

describe("what subagents write into a conversation", () => {
  it("names the helper on its message to the leader, and the host's note about idle helpers", () => {
    expect(systemNote({ role: "user", text: "[message from subagent 'triage' via ReportToLeader]\n\nblocked on the key", origin: "subagent:triage" })).toMatchObject({ kind: "subagent", origin: "triage" });
    expect(systemNote({ role: "user", text: "[subagents] two are idle", origin: "subagents" })).toMatchObject({ kind: "subagents" });
  });
  it("shows the task a helper was handed without the host's framing line", () => {
    const text = "[task from your leader session abc via SubAgent — you work in the leader's workspace; do the task]\n\nCount the open issues.";
    expect(systemNote({ role: "user", text, origin: "subagent-task:abc" })).toEqual({ kind: "task", origin: "abc", body: "Count the open issues." });
    const follow = "[message from your leader session abc via SubAgentSend — act on it]\n\nNow close the stale ones.";
    expect(systemNote({ role: "user", text: follow, origin: "subagent-task:abc" })?.body).toBe("Now close the stale ones.");
  });
  it("has a title for each in both languages", () => {
    for (const lang of LANGS) {
      setLang(lang);
      for (const key of ["turn.note.subagent", "turn.note.subagents", "turn.note.task"]) expect(t(key, { name: "x" })).not.toBe(key);
    }
    setLang("en");
  });
});
