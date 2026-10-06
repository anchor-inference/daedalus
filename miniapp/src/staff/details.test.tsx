// @vitest-environment jsdom
// The Details tab beside a command-line member, and the orchestrators' Details: what they show from
// what the host recorded, and that a number the CLI did not report is said to be missing, never drawn.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionDetail, StaffSessionView, StaffTurn } from "../api";
import { SessionDetails, type DetailsActions } from "../details";
import { DICT, setLang } from "../i18n";
import type { Staff } from "../team/team";
import { contextFill, conversationCounts, hasSpend, keptWords, sessionAge, windowName } from "./details";
import { StaffDetails } from "./StaffDetails";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const NOW = Date.parse("2026-09-27T12:00:00Z");

const MEMBER: Staff = {
  id: "st1", project_id: "p1", name: "Ada", color: "blue", role: "", harness: "claude", agent: "", model: "opus", effort: "high", permission_mode: "default",
  env: "", default_folder_id: null, isolation: "worktree" as Staff["isolation"], instructions: "", notes: "", one_off: false, created_by: "orchestrator",
  created_at: "2026-09-27T09:00:00Z", archived_at: null, live: null, status: "working", sessions: 1,
};

function view(usage: StaffSessionView["usage"], harness = "claude"): StaffSessionView {
  return {
    staff: { id: "st1", name: "Ada", harness, project_id: "p1" },
    session: { id: "ss1", status: "working", waiting_for: "", status_at: "2026-09-27T11:50:00Z", started_at: "2026-09-27T10:00:00Z", terminal_id: "t1", task_id: null, branch: "staff/ada", worktree_path: null, pause_requested: false, cli_session_id: "c1", last_signal_at: "2026-09-27T11:55:00Z" },
    launch: { harness, model: "opus[1m]", effort: "medium", agent: "", permission_mode: "default", env: null, version: "2.1.300", launch_id: "l1", companion_terminal_id: null, worktree: null, branch: "staff/ada", task_id: null },
    health: null,
    usage,
  };
}

const TURNS: StaffTurn[] = [
  { index: 0, role: "orchestrator", text: "[orchestrator] go", tools: [], started_at: "2026-09-27T10:00:00Z", ended_at: "", usage: null },
  { index: 1, role: "assistant", text: "done", tools: [], started_at: "", ended_at: "", usage: null },
  { index: 2, role: "user", text: "and more", tools: [], started_at: "2026-09-27T11:40:00Z", ended_at: "", usage: null },
  { index: 3, role: "assistant", text: "ok", tools: [], started_at: "", ended_at: "", usage: null },
];

describe("the values", () => {
  it("draws the context's share only against a window the CLI reported", () => {
    expect(contextFill(null)).toBeNull();
    expect(contextFill({ context_tokens: 0 })).toBeNull();
    expect(contextFill({ context_tokens: 386_000, context_window: null })).toEqual({ tokens: 386_000, window: null, pct: null });
    expect(contextFill({ context_tokens: 386_000, context_window: 1_000_000 })).toEqual({ tokens: 386_000, window: 1_000_000, pct: 39 });
  });

  it("names the windows as the CLIs do", () => {
    setLang("en");
    expect(windowName(300).key).toBe("staff.details.window.5h");
    expect(windowName(10080).key).toBe("staff.details.window.week");
    expect(windowName(2880)).toEqual({ key: "staff.details.window.days", n: 2 });
    expect(windowName(120)).toEqual({ key: "staff.details.window.hours", n: 2 });
    expect(windowName(0).key).toBe("staff.details.window.other");
  });

  it("counts who spoke, how long the session has run, and whether any spend was read", () => {
    expect(conversationCounts(TURNS)).toEqual({ operator: 1, orchestrator: 1, replies: 2 });
    expect(sessionAge(view(null), NOW)).toBe(2 * 3600 * 1000);
    expect(sessionAge(null, NOW)).toBeNull();
    expect(hasSpend({ input_tokens: 0, output_tokens: 0 })).toBe(false);
    expect(hasSpend({ input_tokens: 10 })).toBe(true);
  });
});

describe("the panels", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.restoreAllMocks();
  });

  it("shows a Claude Code member's launch, state and spend, and says what Claude does not report", () => {
    act(() => root.render(<StaffDetails member={MEMBER} view={view({ input_tokens: 120_000, output_tokens: 8_000, cost_usd: 1.25, source: "subscription", context_tokens: 386_000, context_window: null, windows: [], replies: 2, at: "2026-09-27T11:50:00Z" })} turns={TURNS} now={NOW} />));
    const text = host.textContent ?? "";
    expect(text).toContain("Claude Code 2.1.300");
    expect(text).toContain("opus[1m]");
    expect(text).toContain("medium");
    expect(text).toContain("turn 2");
    expect(text).toContain("2 replies · 1 from the orchestrator · 1 from you");
    expect(host.querySelector("[data-context-fill]")!.textContent).toContain("context window size not reported by Claude Code");
    // No meter without a window: 386k of an unknown window is not a share.
    expect(host.querySelector("#staff-st1-info-staff-context .bar")).toBeNull();
    expect(host.querySelector("[data-staff-spend]")!.textContent).toContain("$1.25");
    expect(host.querySelector("[data-no-windows]")!.textContent).toBe("Subscription limits: not reported by Claude Code.");
  });

  it("draws Codex's context meter and its rate-limit windows", () => {
    act(() => root.render(<StaffDetails member={{ ...MEMBER, harness: "codex" }} view={view({ input_tokens: 5_000, output_tokens: 300, cost_usd: null, source: "subscription", context_tokens: 64_600, context_window: 258_400, windows: [{ minutes: 300, used_pct: 12 }, { minutes: 10080, used_pct: 55.4 }], replies: 1, at: "2026-09-27T11:50:00Z" }, "codex")} turns={TURNS} now={NOW} />));
    expect(host.querySelector("#staff-st1-info-staff-context .dt-aside")!.textContent).toBe("25%");
    expect(host.querySelector('[data-window="300"]')!.textContent).toContain("5-hour window12%");
    expect(host.querySelector('[data-window="10080"]')!.textContent).toContain("Weekly55%");
    expect(host.textContent).toContain("it reports tokens, not a cost");
    expect(host.querySelector("[data-no-windows]")).toBeNull();
  });

  it("says nothing was reported rather than zeros, and says so in Russian too", () => {
    act(() => root.render(<StaffDetails member={MEMBER} view={view(null)} turns={[]} now={NOW} />));
    expect([...host.querySelectorAll("[data-not-reported]")].map((n) => n.textContent)).toEqual(["Not reported by Claude Code yet.", "Not reported by Claude Code yet."]);
    act(() => setLang("ru"));
    act(() => root.render(<StaffDetails member={{ ...MEMBER }} view={view(null)} turns={[]} now={NOW} />));
    expect(host.querySelector("[data-not-reported]")!.textContent).toBe("Claude Code пока этого не сообщил.");
  });

  it("without a live session says so and still shows how the member is set up", () => {
    act(() => root.render(<StaffDetails member={{ ...MEMBER, status: "off" }} view={null} turns={[]} now={NOW} />));
    expect(host.textContent).toContain("No live session");
    expect(host.textContent).toContain("opus");
  });

  it("says how each setting is kept, one plain line each, in both languages", () => {
    const kept: StaffSessionView["kept"] = [
      { setting: "folder", kept: "cli", reason: "readonly", mode: "plan" },
      { setting: "asking", kept: "cli", reason: "mode", mode: "plan" },
      { setting: "network", kept: "none", reason: "open", mode: "" },
      { setting: "scope", kept: "prompt", reason: "brief", mode: "" },
    ];
    act(() => root.render(<StaffDetails member={MEMBER} view={{ ...view(null), kept }} turns={[]} now={NOW} />));
    const rows = [...host.querySelectorAll("[data-kept-list] [data-setting]")];
    expect(rows.map((r) => r.getAttribute("data-kept"))).toEqual(["cli", "cli", "none", "prompt"]);
    expect(rows[0].textContent).toBe("WritingWrites nothing: Claude Code starts in its own plan mode.CLI mode");
    expect(rows[3].textContent).toContain("only asked in its prompt");
    act(() => setLang("ru"));
    act(() => root.render(<StaffDetails member={MEMBER} view={{ ...view(null), kept }} turns={[]} now={NOW} />));
    expect(host.querySelector('[data-setting="network"]')!.textContent).toContain("недоступно");
  });

  it("has words for every line the host can send, and falls back to the kind for a new one", () => {
    const known = (key: string) => key in DICT;
    const sent: [string, string, string][] = [
      ["folder", "host", "readonly"], ["folder", "host", "worktree"], ["folder", "host", "shared"], ["folder", "prompt", "remote"],
      ["folder", "cli", "readonly"], ["folder", "none", "readonly_refused"], ["folder", "cli", "sandbox"], ["folder", "none", "anywhere"],
      ["folder", "prompt", "brief_worktree"], ["folder", "prompt", "brief_shared"], ["asking", "host", "policy"], ["asking", "cli", "mode"],
      ["asking", "cli", "rules"], ["asking", "none", "never"], ["asking", "none", "bypass"], ["network", "cli", "off"], ["network", "none", "open"],
      ["scope", "prompt", "brief"],
    ];
    for (const [setting, kept, reason] of sent) {
      expect(keptWords({ setting, kept, reason, mode: "" } as never, known).text).not.toBeNull();
    }
    expect(keptWords({ setting: "folder", kept: "host", reason: "someday", mode: "" }, known)).toEqual({ label: "staff.kept.setting.folder", text: null, kind: "staff.kept.kind.host" });
  });

  it("gives an orchestrator the session's Details less what would break it", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({ items: [], groups: [], enabled: [], servers: [] }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const detail = {
      id: "o1", title: "Orchestrator", status: "idle", run_id: null, workspace: "/w", project: { id: "p1", name: "Menu", settings: { snapshots: false }, folders: [{ id: "f1", path: "/w", label: "w" }] }, pending: null, model: "opus", messages: [],
      context: { tokens: 50_000, window: 200_000, messages: 40, summaries: 2, operator_turns: 3, last_compaction: { at: "2026-09-27T10:00:00Z", reason: "auto" } },
      usage: { c: 12, i: 90_000, o: 4_000, usd: 3.5, c_today: 4, i_today: 20_000, o_today: 1_000, usd_today: 0.75 },
    } as unknown as SessionDetail;
    const on = new Proxy({}, { get: () => () => {} }) as DetailsActions;
    const render = (role: "session" | "orchestrator" | "main") => act(async () => root.render(<SessionDetails ids={`d-${role}`} id="o1" role={role} detail={detail} busy={false} modes={[]} schedules={[]} provider="" providerUsage={null} toast={() => {}} reload={() => {}} on={on} />));
    await render("orchestrator");
    const sections = () => [...host.querySelectorAll(".dt-section")].map((s) => s.id.replace(/^d-\w+-info-/, ""));
    expect(sections()).toEqual(["session", "context", "usage", "mcp", "toolgroups", "brief", "spend", "advanced"]);
    expect(host.querySelector("[data-usage-today]")!.textContent).toContain("$0.75");
    expect(host.querySelector("[data-last-compaction]")!.textContent).toContain("(auto)");
    await render("main");
    expect(sections()).toEqual(["session", "context", "usage", "workspace", "mcp", "toolgroups", "brief", "spend", "advanced"]);
    await render("session");
    expect(sections()).toContain("danger");
    expect(sections()).toContain("loop");
  });
});
