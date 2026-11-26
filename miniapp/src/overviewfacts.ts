// What sets one chat apart from any other, for the Overview: every line of Details asked whether it
// holds the default, and only the ones that do not become a row — a site the chat serves, a loop that
// runs, a spend cap half used, a server switched on, a brief. A chat with nothing particular has no
// block at all. Each test is a small pure function so what counts as "the default" is written down
// once, here, and pinned by overviewfacts.test.ts; the Overview draws what these return and Details
// stays the place where each one is changed.

import type { LoopView, Schedule, ServiceView, SessionDetail, SessionToolGroup, SubagentModelsView } from "./api";
import type { IconName } from "./icons";
import { plural, t } from "./i18n";
import { clock, usd } from "./format";
import { quotaTone } from "./detailsmodel";

/** Where a row leads: the page of Details its line opens, or a part of the composer for what waits
 *  there (an approval, the queue); none for a fact that has no page (a private chat). */
export type DifferenceTarget = { details: string } | { composer: "approval" | "queue" } | null;

/** One way the chat differs. `rank` orders the block: what needs the operator first, then what is
 *  running, then what is set. */
export type Difference = { id: string; icon: IconName; name: string; value: string; tone?: "attn" | "bad" | "live"; rank: 0 | 1 | 2; target: DifferenceTarget };

const NEEDS_YOU = 0;
const RUNNING = 1;
const SET = 2;

/** An approval the policy asked for and the operator has not answered. Default: none. */
export function approvalsDiff(waiting: number): Difference | null {
  if (waiting <= 0) return null;
  return { id: "approval", icon: "shield", name: t("panel.ov.diff.approval"), value: plural("panel.ov.diff.approval.n", waiting), tone: "attn", rank: NEEDS_YOU, target: { composer: "approval" } };
}

/** Messages held for the running agent. Default: none. */
export function queuedDiff(queued: number): Difference | null {
  if (queued <= 0) return null;
  return { id: "queue", icon: "send", name: t("panel.ov.diff.queue"), value: plural("panel.ov.diff.queue.n", queued), rank: RUNNING, target: { composer: "queue" } };
}

/** A service's address as a reader would type it: the host and port of its link, or its port alone. */
export function serviceAddress(s: Pick<ServiceView, "url" | "port" | "address">): string {
  if (s.url) {
    try {
      const at = new URL(s.url);
      return at.host + (at.pathname !== "/" ? at.pathname : "");
    } catch {
      return s.url;
    }
  }
  if (s.address) return s.address;
  return s.port ? `:${s.port}` : "";
}

/** The services the chat started. Default: none running and none that died. One that died is the
 *  operator's to look at; running ones say where they answer. */
export function servicesDiff(services: ServiceView[] | undefined): Difference | null {
  const running = (services ?? []).filter((s) => s.status === "running");
  const dead = (services ?? []).filter((s) => s.status === "dead");
  if (running.length === 0 && dead.length === 0) return null;
  if (dead.length > 0) {
    return { id: "services", icon: "globe", name: t("session.services"), value: [plural("panel.ov.diff.services.dead", dead.length, { name: dead[0].name }), running.length ? t("session.services.running", { n: running.length }) : ""].filter(Boolean).join(" · "), tone: "bad", rank: NEEDS_YOU, target: { details: "services" } };
  }
  const first = running[0];
  const where = serviceAddress(first);
  const value = running.length === 1 ? [first.name, where].filter(Boolean).join(" · ") : `${t("session.services.running", { n: running.length })} · ${[first.name, where].filter(Boolean).join(" ")}`;
  return { id: "services", icon: "globe", name: t("session.services"), value, tone: "live", rank: RUNNING, target: { details: "services" } };
}

/** The chat's loop. Default: none, or one that has stopped or finished (nothing will run). Active:
 *  how often and when next; paused: that it is held. */
export function loopDiff(loop: LoopView | null | undefined): Difference | null {
  if (!loop || loop.status === "stopped" || loop.status === "done") return null;
  if (loop.status === "paused") return { id: "loop", icon: "loop", name: t("session.loop"), value: t("panel.ov.diff.loop.paused"), tone: "attn", rank: SET, target: { details: "loop" } };
  const pace = loop.mode === "dynamic" ? t("loop.selfpaced") : t("panel.ov.diff.loop.every", { n: Math.max(1, Math.round((loop.interval_seconds ?? 0) / 60)) });
  return { id: "loop", icon: "loop", name: t("session.loop"), value: pace + (loop.next_run_at ? t("details.prop.next", { when: clock(loop.next_run_at) }) : ""), tone: "live", rank: RUNNING, target: { details: "loop" } };
}

/** The chat's schedules. Default: none switched on. */
export function schedulesDiff(schedules: Schedule[]): Difference | null {
  const on = schedules.filter((s) => s.enabled);
  if (on.length === 0) return null;
  const next = on.map((s) => s.next_run_at).filter((at): at is string => !!at).sort()[0];
  return { id: "cron", icon: "calendar", name: t("session.cron"), value: t("session.cron.on", { n: on.length }) + (next ? t("details.prop.next", { when: clock(next) }) : ""), rank: RUNNING, target: { details: "cron" } };
}

export type McpView = { enabled: string[]; servers: { name: string; error: string | null; state: string }[] };

/** The chat's MCP servers. Default: none switched on, which is how the host starts every chat (a
 *  server is switched on per chat). A server switched on and failing needs the operator. */
export function mcpDiff(mcp: McpView | null | undefined): Difference | null {
  if (!mcp || mcp.servers.length === 0) return null;
  const on = mcp.servers.filter((s) => mcp.enabled.includes(s.name));
  if (on.length === 0) return null;
  const failing = on.filter((s) => s.error || s.state === "auth_required" || s.state === "unavailable");
  const value = t("details.prop.of", { n: on.length, of: mcp.servers.length }) + ` · ${on.map((s) => s.name).join(", ")}`;
  return failing.length
    ? { id: "mcp", icon: "plug", name: t("session.mcp"), value: t("panel.ov.diff.mcp.failing", { name: failing[0].name }), tone: "attn", rank: NEEDS_YOU, target: { details: "mcp" } }
    : { id: "mcp", icon: "plug", name: t("session.mcp"), value, rank: SET, target: { details: "mcp" } };
}

/** What stands allowed in this chat ("allow similar", a server for a while). Default: nothing. */
export function grantsDiff(standing: readonly unknown[] | null | undefined): Difference | null {
  if (!standing || standing.length === 0) return null;
  return { id: "grants", icon: "shield", name: t("grant.section"), value: plural("panel.ov.diff.grants.n", standing.length), rank: SET, target: { details: "grants" } };
}

/** The spend cap. Default: none. Set: how much of it is spent, in the attention colour from 80 % and
 *  needing the operator once reached (the chat stops there). */
export function capDiff(cap: number | null | undefined, spent: number | null | undefined): Difference | null {
  if (cap == null) return null;
  const share = cap > 0 ? Math.round((100 * (spent ?? 0)) / cap) : 100;
  const tone = quotaTone(share);
  return { id: "spend", icon: "flag", name: t("session.cap"), value: t("panel.ov.diff.cap", { spent: usd(spent ?? 0), cap: usd(cap), n: share }), tone: tone === "ok" ? undefined : tone, rank: tone === "bad" ? NEEDS_YOU : SET, target: { details: "spend" } };
}

/** The models the chat's helpers may run on. Default: as in Settings (`inherited`). */
export function helpersDiff(view: SubagentModelsView | null | undefined): Difference | null {
  if (!view || view.inherited) return null;
  const value = view.mode === "chosen" ? plural("subm.mode.chosen.n", view.presets.length) : t(`subm.mode.${view.mode}`);
  return { id: "helpers", icon: "spawn", name: t("subm.title"), value, rank: SET, target: { details: "helpers" } };
}

/** The model that looks at pictures. Default: the global one (no preset of the chat's own). */
export function visionDiff(detail: Pick<SessionDetail, "vision_preset" | "vision_label">): Difference | null {
  if (!detail.vision_preset) return null;
  return { id: "vision", icon: "eye", name: t("details.vision"), value: detail.vision_label || detail.vision_preset, rank: SET, target: { details: "vision" } };
}

/** The tool groups. Default: every group where Settings or the model puts it; a group this chat
 *  placed itself is the difference. */
export function toolGroupsDiff(groups: SessionToolGroup[] | null | undefined): Difference | null {
  const own = (groups ?? []).filter((g) => g.source === "session");
  if (own.length === 0) return null;
  return { id: "toolgroups", icon: "skill", name: t("session.toolgroups"), value: t("panel.ov.diff.toolgroups", { names: own.map((g) => g.name).join(", ") }), rank: SET, target: { details: "toolgroups" } };
}

/** Single tools switched off. Default: all on. */
export function toolsDiff(off: string[] | undefined): Difference | null {
  if (!off || off.length === 0) return null;
  return { id: "tools", icon: "wrench", name: t("session.tools"), value: `${t("details.prop.off", { n: off.length })} · ${off.join(", ")}`, rank: SET, target: { details: "tools" } };
}

/** The brief. Default: none. */
export function briefDiff(brief: string | null | undefined): Difference | null {
  const text = (brief ?? "").trim();
  if (!text) return null;
  return { id: "brief", icon: "journal", name: t("session.brief"), value: `«${text.replace(/\s+/g, " ")}»`, rank: SET, target: { details: "brief" } };
}

/** Where it works. Default: in the container, in its own or its project's folder. On the host, or
 *  with its folder being carried between the two, is the difference. */
export function placeDiff(env: SessionDetail["env"], workspace: string, moving: boolean): Difference | null {
  if (moving) return { id: "workspace", icon: "folder", name: t("panel.ov.diff.place"), value: t("panel.ov.diff.place.moving"), tone: "live", rank: RUNNING, target: { details: "workspace" } };
  if (env !== "host") return null;
  const parts = (workspace || "").split("/").filter(Boolean);
  return { id: "workspace", icon: "folder", name: t("panel.ov.diff.place"), value: t("panel.ov.diff.place.host", { path: parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : workspace }), rank: SET, target: { details: "workspace" } };
}

/** An imported chat. Default: started here. */
export function originDiff(imported: SessionDetail["imported"]): Difference | null {
  if (!imported) return null;
  return { id: "origin", icon: "fork", name: t("imp.origin"), value: imported.harness_name ?? imported.harness, rank: SET, target: { details: "origin" } };
}

/** A private chat: never remembered, learned from or found. Default: an ordinary chat. It was fixed
 *  when the chat was made, so there is no page to open. */
export function privateDiff(isPrivate: boolean | undefined): Difference | null {
  return isPrivate ? { id: "private", icon: "eyeoff", name: t("panel.ov.diff.private"), value: t("panel.ov.diff.private.value"), rank: SET, target: null } : null;
}

/** Autopilot. Default: off. On, it answers the agent's questions and continues for the operator; held
 *  off by the setting for the whole app, it says so. Switched in the composer, not in Details. */
export function autopilotDiff(autopilot: SessionDetail["autopilot"]): Difference | null {
  if (!autopilot?.on) return null;
  return { id: "autopilot", icon: "ffwd", name: t("composer.autopilot"), value: autopilot.enabled ? t("panel.ov.diff.autopilot", { used: autopilot.used, allowed: autopilot.allowed }) : t("composer.autopilot.disabled"), tone: autopilot.enabled ? "live" : "attn", rank: SET, target: null };
}

/** YAGNI. Default: off. Switched in the composer. */
export function yagniDiff(yagni: boolean | undefined): Difference | null {
  return yagni ? { id: "yagni", icon: "bulb", name: t("composer.yagni"), value: t("panel.ov.diff.yagni"), rank: SET, target: null } : null;
}

export type DifferenceInput = {
  detail: SessionDetail;
  /** A project's orchestrator or the main chat: no loop and no tool switches of its own, as in Details. */
  conductor: boolean;
  schedules: Schedule[];
  mcp: McpView | null | undefined;
  standing: readonly unknown[] | null | undefined;
  helpers: SubagentModelsView | null | undefined;
  toolGroups: SessionToolGroup[] | null | undefined;
  approvals: number;
  queued: number;
  moving: boolean;
};

/** Every difference, in the order the block draws them: what needs the operator, what runs, what is set. */
export function differences(input: DifferenceInput): Difference[] {
  const { detail } = input;
  const rows = [
    approvalsDiff(input.approvals),
    servicesDiff(detail.services),
    mcpDiff(input.mcp),
    capDiff(detail.usd_cap, detail.usage.usd),
    queuedDiff(input.queued),
    input.conductor ? null : loopDiff(detail.loop),
    schedulesDiff(input.schedules),
    placeDiff(detail.env, detail.workspace, input.moving),
    autopilotDiff(detail.autopilot),
    yagniDiff(detail.yagni),
    privateDiff(detail.private),
    grantsDiff(input.standing),
    helpersDiff(input.helpers),
    visionDiff(detail),
    toolGroupsDiff(input.toolGroups),
    input.conductor ? null : toolsDiff(detail.tools_off),
    briefDiff(detail.brief),
    originDiff(detail.imported),
  ].filter((row): row is Difference => row !== null);
  // A stable sort: within a rank the rows keep the order above.
  return rows.map((row, i) => ({ row, i })).sort((a, b) => a.row.rank - b.row.rank || a.i - b.i).map(({ row }) => row);
}

/** The block shows this many rows and says how many more Details holds; past about eight the
 *  Overview stops being something read at a glance. */
export const SHOWN_DIFFERENCES = 8;
