import { describe, expect, it } from "vitest";
import { agentSource, checkSummary, offeredDraft, offeredToSend, refusedStaff, rowState, signIn, updateCount, versionMark } from "./model";

const row = (over: Partial<Parameters<typeof rowState>[0]> = {}) => ({ operation: null, installed: true, installable: false, logged_in: "yes" as const, can_sign_in: true, update_available: false, ...over });

describe("a row's one button", () => {
  it("follows what matters first", () => {
    expect(rowState(row({ operation: { kind: "update", started_at: "", terminal_id: null, target: "2.1.290" }, update_available: true }))).toBe("busy");
    expect(rowState(row({ installed: false, installable: true }))).toBe("install");
    expect(rowState(row({ installed: false, installable: false }))).toBe("blocked");
    expect(rowState(row({ logged_in: "no", update_available: true }))).toBe("signin");
    expect(rowState(row({ logged_in: "no", can_sign_in: false, update_available: true }))).toBe("update");
    expect(rowState(row({ update_available: true }))).toBe("update");
    expect(rowState(row())).toBe("current");
  });

  it("counts the updates Update all would run", () => {
    expect(updateCount([
      row({ update_available: true }), row({ update_available: true, operation: { kind: "update", started_at: "", terminal_id: null, target: "" } }),
      row({ installed: false, update_available: true }), row(),
    ])).toBe(1);
  });
});

describe("what a row says", () => {
  it("names the account a CLI is signed in with, when it said", () => {
    expect(signIn({ installed: true, logged_in: "yes", login_detail: "claude.ai · max" })).toEqual({ key: "harness.signin.as", detail: "claude.ai · max", tone: "ok" });
    expect(signIn({ installed: true, logged_in: "yes", login_detail: "" }).key).toBe("harness.signin.yes");
    expect(signIn({ installed: true, logged_in: "no", login_detail: "" }).tone).toBe("warn");
    expect(signIn({ installed: false, logged_in: "unknown", login_detail: "" }).key).toBe("harness.signin.absent");
  });

  it("marks a version the adapter was not tested with until a self-check passes on it", () => {
    expect(versionMark({ installed: true, supported: true, version_guard: "" })).toBe("");
    expect(versionMark({ installed: true, supported: true, version_guard: "unverified" })).toBe("unverified");
    expect(versionMark({ installed: true, supported: true, version_guard: "verified" })).toBe("verified");
    expect(versionMark({ installed: true, supported: false, version_guard: "unverified" })).toBe("unsupported");
    expect(versionMark({ installed: false, supported: false, version_guard: "" })).toBe("");
  });

  it("sums up the last self-check, naming the step that failed and counting the skipped ones", () => {
    expect(checkSummary({})).toEqual({ key: "harness.check.never", step: "", skipped: 0 });
    expect(checkSummary({ ok: true, steps: [{ name: "version", ok: true }, { name: "session", ok: true, skipped: true }] })).toEqual({ key: "harness.check.passed.skipped", step: "", skipped: 1 });
    expect(checkSummary({ ok: false, steps: [{ name: "version", ok: true }, { name: "ready", ok: false, detail: "no SessionStart" }] })).toEqual({ key: "harness.check.failed", step: "ready", skipped: 0 });
  });

  it("names who a refused update waits for", () => {
    expect(refusedStaff({ detail: "…", staff: [{ name: "Ira", project: "Bakery 2.0" }, { name: "Max" }, "junk", { project: "no name" }] })).toEqual(["Ira (Bakery 2.0)", "Max"]);
    expect(refusedStaff(undefined)).toEqual([]);
    expect([agentSource("project"), agentSource("builtin"), agentSource(undefined)]).toEqual(["harness.agent.project", "harness.agent.builtin", "harness.agent.user"]);
  });
});

describe("the models a row offers", () => {
  const all = ["opus", "claude-opus-5-5", "claude-opus-4-8", "claude-sonnet-5-5"];
  it("starts from nothing ticked when there is no choice, from the choice otherwise", () => {
    expect(offeredDraft({ models: all, all_models: all, models_chosen: false })).toEqual([]);
    expect(offeredDraft({ models: ["claude-opus-5-5", "gone-model"], all_models: all, models_chosen: true })).toEqual(["claude-opus-5-5"]);
  });
  it("sends the ticked ones in the CLI's order, and no choice for all or none", () => {
    expect(offeredToSend(["claude-sonnet-5-5", "claude-opus-5-5"], all)).toEqual(["claude-opus-5-5", "claude-sonnet-5-5"]);
    expect(offeredToSend([...all], all)).toBeNull();
    expect(offeredToSend([], all)).toBeNull();
  });
});
