// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { memberPath, openPath } from "./phone";
import type { Staff } from "./team";

function member(harness: Staff["harness"], live: Partial<NonNullable<Staff["live"]>> | null): Staff {
  return { id: "st-1", name: "Ira", harness, live: live ? { id: "ss-1", status: "working", waiting_for: "", task_id: null, started_at: "", ended_at: null, branch: null, worktree_path: null, ...live } : null } as unknown as Staff;
}

describe("where a tap on a phone's team row leads", () => {
  it("opens a command-line member with a live terminal on that terminal", () => {
    const ira = member("claude", { terminal_id: "tm-ira" });
    expect(openPath("p1", ira)).toMatch(/\/terminals\/tm-ira$/);
    // The Feed stays reachable from the row's menu.
    expect(memberPath("p1", ira)).toMatch(/\/project\/p1\/staff\/st-1$/);
  });

  it("falls back to the member's own page when its terminal is not running yet", () => {
    expect(openPath("p1", member("codex", {}))).toMatch(/\/project\/p1\/staff\/st-1$/);
    expect(openPath("p1", member("codex", null))).toBeNull();
  });

  it("leaves a Daedalus member on its conversation", () => {
    expect(openPath("p1", member("daedalus", { session_id: "s-9", terminal_id: "tm-x" }))).toMatch(/\/project\/p1\/s\/s-9$/);
  });
});
