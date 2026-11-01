// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import type { ConnectionState } from "./connection";
import type { TerminalView } from "../api";
import type { TerminalState } from "./instance";
import { connectionText, messageTargets, paneState, withMessage } from "./status";

const EVERY: ConnectionState[] = [
  { kind: "connecting" },
  { kind: "live" },
  { kind: "reconnecting", attempt: 1, delayMs: 0 },
  { kind: "reconnecting", attempt: 3, delayMs: 4000 },
  { kind: "exited", code: 0, signal: null },
  { kind: "exited", code: null, signal: "SIGKILL" },
  { kind: "unavailable", reason: "gone" },
  { kind: "unavailable", reason: "environment" },
  { kind: "unavailable", reason: "origin" },
  { kind: "unavailable", reason: "auth" },
  { kind: "proxy-blocked" },
];

afterEach(() => setLang("en"));

describe("connectionText", () => {
  it("has words for every state, in both languages", () => {
    for (const lang of ["en", "ru"] as const) {
      setLang(lang);
      for (const state of EVERY) expect(connectionText(state), JSON.stringify(state)).not.toMatch(/^\[|undefined/);
    }
  });

  it("says when the next attempt is, and how a process ended", () => {
    setLang("en");
    expect(connectionText({ kind: "reconnecting", attempt: 3, delayMs: 4000 })).toBe("Connection lost · reconnecting in 4 s");
    expect(connectionText({ kind: "exited", code: 2, signal: null })).toBe("Process ended · code 2");
    expect(connectionText({ kind: "exited", code: null, signal: "SIGKILL" })).toBe("Process ended · signal SIGKILL");
    setLang("ru");
    expect(connectionText({ kind: "proxy-blocked" })).toMatch(/обратным прокси/);
  });
});

describe("paneState", () => {
  const row = (fields: Partial<TerminalView> = {}) => ({ status: "running", exit_code: null, exit_signal: null, activity: null, live: null, ...fields }) as TerminalView;
  const marks = (result: "running" | "ok" | "failed" | null) => ({ commands: { last: result ? { n: 1, command: "x", result, exitCode: null } : null, ended: false, prompts: 0, active: true }, unseen: false, bell: false, exit: null }) as unknown as TerminalState;

  it("says waiting for a staff member's permission or question, and for anything with an Answer", () => {
    expect(paneState(row({ activity: { status: "permission" } }))).toBe("waiting");
    expect(paneState(row({ activity: { status: "question" } }))).toBe("waiting");
    expect(paneState(row({ activity: { label: "waiting for permission", level: "warn", action: { label: "Answer", path: "/app/inbox" } } }))).toBe("waiting");
  });

  it("follows the agent's own status before the screen's", () => {
    expect(paneState(row({ activity: { status: "working" } }), marks(null))).toBe("working");
    expect(paneState(row({ activity: { status: "turn_done_unseen" } }))).toBe("done");
    expect(paneState(row({ activity: { status: "error", level: "bad" } }))).toBe("failed");
    expect(paneState(row({ activity: { status: "idle", level: "idle" } }), marks("running"))).toBe("idle");
  });

  it("reads a shell from its running command or its busy flag", () => {
    expect(paneState(row(), marks("running"))).toBe("working");
    expect(paneState(row({ live: { clients: 1, busy: true, keyboard: { owner: "auto", until: null }, size_owner: "you", alt_screen: false } }))).toBe("working");
    expect(paneState(row(), marks("ok"))).toBe("idle");
    expect(paneState(row(), marks("failed"))).toBe("failed");
    expect(paneState(row(), { ...marks(null), unseen: true })).toBe("done");
  });

  it("tells a clean end from a failure, and a lost terminal from both", () => {
    expect(paneState(row({ status: "exited", exit_code: 0 }))).toBe("ended");
    expect(paneState(row({ status: "exited", exit_code: 2 }))).toBe("failed");
    expect(paneState(row({ status: "exited", exit_signal: "SIGKILL" }))).toBe("failed");
    expect(paneState(row({ status: "lost" }))).toBe("ended");
    expect(paneState(row(), { ...marks(null), exit: { code: 1, signal: null } })).toBe("failed");
  });

  it("is idle for a row nobody knows anything about", () => {
    expect(paneState(null)).toBe("idle");
  });
});

describe("a message from another agent", () => {
  const rows = [
    { id: "t-ira", owner: { kind: "staff" as const, id: "st-ira" }, status: "running" as const },
    { id: "t-old", owner: { kind: "staff" as const, id: "st-ira" }, status: "exited" as const },
    { id: "t-bob", owner: { kind: "staff" as const, id: "st-bob" }, status: "running" as const },
    { id: "t-sh", owner: { kind: "free" as const, id: null }, status: "running" as const },
  ];

  it("lights the running terminals of the member it reached, and nothing for one that never went", () => {
    expect(messageTargets({ to_staff_id: "st-ira", state: "sent" }, rows)).toEqual(["t-ira"]);
    expect(messageTargets({ to_staff_id: "st-ira", state: "refused" }, rows)).toEqual([]);
    expect(messageTargets({ to_staff_id: "st-ira", state: "failed" }, rows)).toEqual([]);
    // The orchestrator, and a payload from before the host named the receiver by id.
    expect(messageTargets({ to: "orchestrator", state: "sent" }, rows)).toEqual([]);
    expect(messageTargets(null, rows)).toEqual([]);
  });

  it("says message for the moment it lasts, except over a request that waits on the operator", () => {
    expect(withMessage("working", true)).toBe("message");
    expect(withMessage("idle", true)).toBe("message");
    expect(withMessage("waiting", true)).toBe("waiting");
    expect(withMessage("working", false)).toBe("working");
  });
});
