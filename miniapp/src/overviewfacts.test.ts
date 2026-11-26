import { beforeEach, describe, expect, it } from "vitest";
import type { LoopView, Schedule, ServiceView, SessionDetail, SessionToolGroup, SubagentModelsView } from "./api";
import { setLang } from "./i18n";
import {
  SHOWN_DIFFERENCES, approvalsDiff, autopilotDiff, briefDiff, capDiff, differences, grantsDiff, helpersDiff, loopDiff, mcpDiff, originDiff,
  placeDiff, privateDiff, queuedDiff, schedulesDiff, serviceAddress, servicesDiff, toolGroupsDiff, toolsDiff, visionDiff, yagniDiff,
} from "./overviewfacts";

beforeEach(() => setLang("en"));

const service = (over: Partial<ServiceView> = {}): ServiceView => ({ name: "site", command: "npm run dev", cwd: "/w", port: 5173, url: "http://127.0.0.1:5173", pid: 1, status: "running", restart: false, note: null, started_at: "2026-11-26T10:00:00Z", stopped_at: null, ...over });
const loop = (over: Partial<LoopView> = {}): LoopView => ({ mode: "interval", interval_seconds: 600, status: "active", run_count: 1, max_runs: null, next_run_at: null, last_run_at: null, last_reason: null, stop_reason: null, pause_note: null, instruction: "check", ...over });
const schedule = (enabled: boolean, next: string | null = null) => ({ enabled, next_run_at: next }) as unknown as Schedule;
const helpers = (over: Partial<SubagentModelsView> = {}) => ({ mode: "all", presets: [], inherited: true, from_parent: false, allowed: [], default: { mode: "all", presets: [] }, session_preset: null, options: [], ...over }) as SubagentModelsView;
const group = (source: SessionToolGroup["source"], name = "board"): SessionToolGroup => ({ name, description: "", tools: 3, load: "auto", source, state: "advertised", pending: false });

describe("each line of Details at its default draws nothing", () => {
  it("holds for every test", () => {
    expect([
      approvalsDiff(0), queuedDiff(0), servicesDiff([]), servicesDiff([service({ status: "stopped" })]), loopDiff(null), loopDiff(loop({ status: "stopped" })), loopDiff(loop({ status: "done" })),
      schedulesDiff([]), schedulesDiff([schedule(false)]), mcpDiff(null), mcpDiff({ enabled: [], servers: [{ name: "github", error: null, state: "disabled" }] }), grantsDiff([]), grantsDiff(null),
      capDiff(null, 3), helpersDiff(helpers()), helpersDiff(null), visionDiff({ vision_preset: "", vision_label: "" }), toolGroupsDiff([group("default"), group("settings"), group("model")]),
      toolsDiff([]), briefDiff(""), briefDiff("   "), placeDiff("container", "/w/a", false), placeDiff(undefined, "/w/a", false), originDiff(null), privateDiff(false), autopilotDiff(null),
      autopilotDiff({ on: false, enabled: true, eligible: true, used: 0, allowed: 5 }), yagniDiff(false),
    ]).toEqual(Array(28).fill(null));
  });
});

describe("a line away from its default", () => {
  it("names a running service with where it answers, and puts one that died first", () => {
    expect(servicesDiff([service()])).toMatchObject({ value: "site · 127.0.0.1:5173", tone: "live", rank: 1, target: { details: "services" } });
    expect(servicesDiff([service(), service({ name: "api", url: null, port: 8000 })])?.value).toBe("2 running · site 127.0.0.1:5173");
    expect(servicesDiff([service({ name: "api", status: "dead" })])).toMatchObject({ value: "api stopped unexpectedly", tone: "bad", rank: 0 });
    expect(serviceAddress({ url: null, port: 8000, address: null })).toBe(":8000");
  });

  it("says how often a loop runs and when next, and that a paused one is held", () => {
    expect(loopDiff(loop())?.value).toBe("every 10 min");
    expect(loopDiff(loop({ mode: "dynamic", interval_seconds: null }))?.value).toBe("self-paced");
    expect(loopDiff(loop({ next_run_at: "2026-11-26T10:30:00Z" }))?.value).toMatch(/^every 10 min · next /);
    expect(loopDiff(loop({ status: "paused" }))).toMatchObject({ value: "paused", tone: "attn", rank: 2 });
  });

  it("counts the schedules switched on", () => {
    expect(schedulesDiff([schedule(true), schedule(false), schedule(true)])?.value).toBe("2 on");
  });

  it("counts the MCP servers switched on, and puts a failing one first", () => {
    const servers = [{ name: "github", error: null, state: "ready" }, { name: "sentry", error: null, state: "disabled" }, { name: "linear", error: null, state: "disabled" }];
    expect(mcpDiff({ enabled: ["github"], servers })).toMatchObject({ value: "1 of 3 on · github", rank: 2, target: { details: "mcp" } });
    expect(mcpDiff({ enabled: ["github"], servers: [{ ...servers[0], state: "auth_required" }, servers[1]] })).toMatchObject({ value: "github is on and failing", tone: "attn", rank: 0 });
    expect(mcpDiff({ enabled: ["gone"], servers })).toBeNull();
  });

  it("says how much of a spend cap is used, in the attention colour near it and first once reached", () => {
    expect(capDiff(4, 2)).toMatchObject({ value: "$2.00 of $4.00 · 50%", rank: 2 });
    expect(capDiff(4, 3.4)).toMatchObject({ tone: "attn", rank: 2 });
    expect(capDiff(4, 4)).toMatchObject({ tone: "bad", rank: 0 });
    expect(capDiff(0, 0)).toMatchObject({ tone: "bad" });
  });

  it("reads the helpers' own models, the chat's vision model, its tool groups and tools", () => {
    expect(helpersDiff(helpers({ inherited: false, mode: "chosen", presets: ["a", "b"] }))?.value).toBe("2 models chosen");
    expect(helpersDiff(helpers({ inherited: false, mode: "session" }))?.value).toBe("Only the session’s model");
    expect(visionDiff({ vision_preset: "gpt.luna", vision_label: "GPT-5.6 Luna" })?.value).toBe("GPT-5.6 Luna");
    expect(toolGroupsDiff([group("session", "board"), group("default", "web")])?.value).toBe("set here: board");
    expect(toolsDiff(["Exec", "WebFetch"])?.value).toBe("2 off · Exec, WebFetch");
  });

  it("quotes a brief, and names a host folder, a folder on the move, an import, a private chat and the switches", () => {
    expect(briefDiff("Static  HTML,\nno frameworks")?.value).toBe("«Static HTML, no frameworks»");
    expect(placeDiff("host", "/home/someone/work/bakery", false)?.value).toBe("on the host · …/work/bakery");
    expect(placeDiff("container", "/w", true)).toMatchObject({ tone: "live", rank: 1 });
    expect(originDiff({ harness: "claude", harness_name: "Claude Code" } as SessionDetail["imported"])?.value).toBe("Claude Code");
    expect(privateDiff(true)).toMatchObject({ target: null });
    expect(autopilotDiff({ on: true, enabled: true, eligible: true, used: 2, allowed: 5 })?.value).toBe("on · 2 of 5 turns");
    expect(autopilotDiff({ on: true, enabled: false, eligible: true, used: 0, allowed: 5 })).toMatchObject({ tone: "attn" });
    expect(yagniDiff(true)).toMatchObject({ target: null });
    expect(approvalsDiff(2)).toMatchObject({ value: "2 actions wait for you", rank: 0, target: { composer: "approval" } });
    expect(queuedDiff(1)).toMatchObject({ value: "1 message for the end of the turn", target: { composer: "queue" } });
    expect(grantsDiff([{}, {}])?.value).toBe("2 standing grants");
  });

  it("speaks Russian too", () => {
    setLang("ru");
    expect(capDiff(4, 2)?.value).toBe("$2.00 из $4.00 · 50%");
    expect(approvalsDiff(5)?.value).toBe("5 действий ждут вас");
    expect(servicesDiff([service({ status: "dead" })])?.value).toBe("site упал");
  });
});

describe("differences", () => {
  const detail = (over: Partial<SessionDetail> = {}) => ({ services: [], loop: null, usd_cap: null, usage: { usd: 0 }, env: "container", workspace: "/w", tools_off: [], brief: "", vision_preset: "", imported: null, private: false, autopilot: null, yagni: false, ...over }) as unknown as SessionDetail;
  const none = { conductor: false, schedules: [], mcp: null, standing: [], helpers: null, toolGroups: [], approvals: 0, queued: 0, moving: false };

  it("is empty for a chat with everything at its default", () => {
    expect(differences({ ...none, detail: detail() })).toEqual([]);
  });

  it("puts what needs the operator first, then what runs, then what is set", () => {
    const rows = differences({ ...none, approvals: 1, detail: detail({ brief: "b", usd_cap: 4, usage: { usd: 2 }, services: [service()], loop: loop() }) });
    expect(rows.map((row) => row.id)).toEqual(["approval", "services", "loop", "spend", "brief"]);
  });

  it("leaves the loop and the tool switches to Details for an orchestrator, as Details does", () => {
    expect(differences({ ...none, conductor: true, detail: detail({ loop: loop(), tools_off: ["Exec"] }) })).toEqual([]);
  });

  it("can list more rows than the block shows", () => {
    const rows = differences({ ...none, approvals: 1, queued: 1, moving: true, schedules: [schedule(true)], standing: [{}], helpers: helpers({ inherited: false }), toolGroups: [group("session")],
      mcp: { enabled: ["a"], servers: [{ name: "a", error: null, state: "ready" }] },
      detail: detail({ brief: "b", usd_cap: 4, services: [service()], loop: loop(), tools_off: ["Exec"], vision_preset: "v", private: true, yagni: true }) });
    expect(rows.length).toBeGreaterThan(SHOWN_DIFFERENCES);
  });
});
