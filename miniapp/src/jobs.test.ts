// @vitest-environment jsdom
// What the Jobs list says under a task: how it stands or ended, how long, and what to warn about.

import { beforeEach, describe, expect, it } from "vitest";
import type { TaskView } from "./api";
import { setLang } from "./i18n";
import { taskSub, tasksAside } from "./jobs";

const NOW = Date.parse("2026-09-11T12:00:00Z");

function task(over: Partial<TaskView>): TaskView {
  return {
    id: "job-1", owner_session_id: "s", parent_run_id: null, kind: "job", state: "running", outcome: null, exit_code: null,
    title: "make test", command: "make test", where: null, started_at: "2026-09-11T11:48:00Z", last_activity_at: "2026-09-11T11:59:00Z",
    ended_at: null, flag: null, reported: false, progress: null, child_session_id: null, result_ref: null, stop_supported: true, ...over,
  };
}

describe("taskSub", () => {
  beforeEach(() => setLang("en"));
  it("says how long a running job has run", () => {
    expect(taskSub(task({}), NOW)).toEqual({ text: "running · 12m", flag: "", service: false });
  });
  it("carries the warning of a quiet or overdue job", () => {
    expect(taskSub(task({ flag: "quiet" }), NOW).flag).toBe("possibly stuck");
    expect(taskSub(task({ flag: "overdue" }), NOW).flag).toBe("over time");
  });
  it("calls a running server a service", () => {
    expect(taskSub(task({ kind: "service" }), NOW)).toMatchObject({ text: "service · 12m", service: true });
  });
  it("names how an ended job ended, with its time between start and end", () => {
    const ended = { ended_at: "2026-09-11T11:50:30Z" };
    expect(taskSub(task({ ...ended, state: "done", outcome: "succeeded", exit_code: 0 }), NOW).text).toBe("succeeded · 2m 30s");
    expect(taskSub(task({ ...ended, state: "failed", outcome: "failed", exit_code: 3 }), NOW).text).toBe("failed · exit 3 · 2m 30s");
    expect(taskSub(task({ ...ended, state: "cancelled", outcome: "killed" }), NOW).text).toBe("killed · 2m 30s");
    expect(taskSub(task({ ...ended, state: "lost", outcome: "lost" }), NOW).text).toBe("lost · 2m 30s");
    expect(taskSub(task({ ...ended, state: "failed", outcome: "timed_out" }), NOW).text).toBe("timed out · 2m 30s");
  });
  it("falls back on the state when the host sent no outcome", () => {
    expect(taskSub(task({ state: "lost", ended_at: "2026-09-11T11:49:00Z" }), NOW).text).toBe("lost · 1m");
  });
  it("speaks Russian", () => {
    setLang("ru");
    expect(taskSub(task({ flag: "quiet" }), NOW).flag).toBe("возможно, зависла");
  });
});

describe("tasksAside", () => {
  beforeEach(() => setLang("en"));
  it("counts what runs against the total, and only the total when nothing runs", () => {
    expect(tasksAside([task({}), task({ id: "b", state: "done", outcome: "succeeded" })])).toBe("1 running · 2");
    expect(tasksAside([task({ state: "done", outcome: "succeeded" })])).toBe("1");
  });
});
