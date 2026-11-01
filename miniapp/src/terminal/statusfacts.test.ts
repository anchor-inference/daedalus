import { afterEach, describe, expect, it } from "vitest";
import type { TerminalView } from "../api";
import { setLang } from "../i18n";
import type { TerminalState } from "./instance";
import { contextText, costText, hasFacts, shortPath, shortTokens, statusFacts, statusTooltip } from "./statusfacts";

afterEach(() => setLang("en"));

function row(extra: Partial<TerminalView> = {}): TerminalView {
  return {
    id: "t1", env: "container", title: "bash", owner: { kind: "free", id: null }, project_id: null, profile: "shell", sandbox: false,
    cwd: "/home/operator/work/bakery", status: "running", exit_code: null, exit_signal: null, created_at: "", exited_at: null,
    last_output_at: null, last_input_at: null, cols: 80, rows: 24, live: null, last_command: null, activity: null, ...extra,
  };
}

const CLAUDE: TerminalView["agent"] = {
  kind: "claude", source: "staff", model: "opus", effort: "high", state: "working", status: "working", waiting_for: null,
  usage: { input_tokens: 40_000, output_tokens: 8_200, cache_read_tokens: 30_800, context_tokens: 11_000, context_window: 249_000, cost_usd: 1.234, source: "subscription" },
};

describe("shortTokens", () => {
  it("writes counts the way a CLI's status line does", () => {
    expect(shortTokens(950)).toBe("950");
    expect(shortTokens(8_200)).toBe("8.2k");
    expect(shortTokens(8_000)).toBe("8k");
    expect(shortTokens(249_000)).toBe("249k");
    expect(shortTokens(1_200_000)).toBe("1.2M");
  });
});

describe("shortPath", () => {
  it("puts the home as ~ and keeps the last two folders of a deep path", () => {
    expect(shortPath("/home/operator/work/bakery", "/home/operator")).toBe("~/work/bakery");
    expect(shortPath("/home/operator", "/home/operator")).toBe("~");
    expect(shortPath("/home/operatorx/a", "/home/operator")).toBe("/home/operatorx/a");
    expect(shortPath("/srv/app/src/lib", null)).toBe("…/src/lib");
    expect(shortPath("/srv/app", null)).toBe("/srv/app");
    expect(shortPath("C:\\Users\\you\\work\\site\\src", null)).toBe("…\\site\\src");
  });
});

describe("statusFacts", () => {
  it("maps a staff member's agent: model, effort, a meter on the CLI's own window, cache share, output and cost", () => {
    const facts = statusFacts(row({ agent: CLAUDE, git: { branch: "staff/ira", detached: false, dirty: true } }), null, "/home/operator");
    expect(facts.agent).toEqual({ kind: "claude", label: "opus", model: "opus", effort: "high", detected: false });
    expect(facts.path).toEqual({ short: "~/work/bakery", full: "/home/operator/work/bakery" });
    expect(facts.branch).toEqual({ name: "staff/ira", dirty: true, detached: false });
    expect(facts.context).toEqual({ used: 11_000, window: 249_000, pct: 4 });
    expect(contextText(facts.context!)).toBe("11k/249k");
    expect(facts.cachePct).toBe(77);
    expect(facts.output).toBe(8_200);
    expect(facts.cost).toEqual({ usd: 1.234, approx: true });
    expect(costText(facts.cost!)).toBe("≈$1.23");
    expect(facts.command).toBeNull();
    expect(facts.waiting).toBeNull();
  });

  it("leaves out what the host did not say instead of drawing a zero or a guess", () => {
    const bare = statusFacts(row({ agent: { kind: "codex", source: "staff", usage: { context_tokens: 9_000, input_tokens: 0 } } }));
    expect(bare.agent?.label).toBe("codex");
    expect(bare.agent?.effort).toBeNull();
    // No window from the CLI: the count, and no share of an unknown number.
    expect(bare.context).toEqual({ used: 9_000, window: null, pct: null });
    expect(contextText(bare.context!)).toBe("9k");
    expect(bare.cachePct).toBeNull();
    expect(bare.output).toBeNull();
    expect(bare.cost).toBeNull();
    expect(bare.branch).toBeNull();
    expect(statusTooltip(bare)).not.toMatch(/undefined|null|NaN/);
  });

  it("says a staff member waits, with what for, from the agent or the card's activity", () => {
    const facts = statusFacts(row({ agent: { ...CLAUDE!, state: "waiting", waiting_for: "bash: npm install grammy" } }));
    expect(facts.waiting).toEqual({ why: "bash: npm install grammy" });
    const fromActivity = statusFacts(row({ activity: { status: "permission", label: "needs a yes", level: "warn" } }));
    expect(fromActivity.waiting).toEqual({ why: "needs a yes" });
  });

  it("gives a plain shell its last command, the live one first", () => {
    const listed = statusFacts(row({ last_command: { command: "npm test", exit_code: 1, at: "" } }));
    expect(listed.command).toEqual({ text: "npm test", exit: 1, running: false });
    expect(listed.agent).toBeNull();
    const state = { cwd: "/srv/app", commands: { last: { n: 3, command: "make", result: "running", exitCode: null }, ended: false, prompts: 1, active: true } } as unknown as TerminalState;
    const live = statusFacts(row({ last_command: { command: "npm test", exit_code: 1, at: "" } }), state);
    expect(live.command).toEqual({ text: "make", exit: null, running: true });
    expect(live.path?.full).toBe("/srv/app");
  });

  it("names an agent typed at a shell's prompt and keeps the shell's command beside it", () => {
    const facts = statusFacts(row({ agent: { kind: "claude", source: "detected", model: "sonnet", state: null, usage: null }, last_command: { command: "ls", exit_code: 0, at: "" } }));
    expect(facts.agent).toEqual({ kind: "claude", label: "sonnet", model: "sonnet", effort: null, detected: true });
    expect(facts.command?.text).toBe("ls");
  });

  it("has nothing to draw for a terminal the host knows nothing about", () => {
    expect(hasFacts(statusFacts(row({ cwd: "" })))).toBe(false);
    expect(hasFacts(statusFacts(null))).toBe(false);
  });

  it("writes the tooltip in full, in both languages", () => {
    const facts = statusFacts(row({ agent: CLAUDE, git: { branch: "main", detached: false, dirty: false } }), null, "/home/operator");
    expect(statusTooltip(facts)).toContain("Context: 11,000 of 249,000 tokens (4%)");
    expect(statusTooltip(facts)).toContain("Folder: /home/operator/work/bakery");
    setLang("ru");
    const ru = statusTooltip(facts);
    expect(ru).toContain("Ветка: main");
    expect(ru).toContain("Модель: opus");
    expect(ru).not.toMatch(/\[term\./);
  });
});
