// What a terminal's status line says, as plain facts: the agent and its model, the folder and its
// branch, how full the agent's context is, how much of its prompt the cache served, what it wrote and
// what it cost, whether it waits on someone, and for a plain shell the last command and how it ended.
//
// Every fact is optional and comes from one place: the host's listing (`agent`, `git`, `last_command`)
// and, when a pane shows the terminal, the live state (its directory and its last command arrive there
// first). Nothing is guessed. A meter without the window the CLI reported would draw a share of a number
// nobody knows, so it is not drawn; a missing count is a missing segment, never "0" or "undefined".

import { t } from "../i18n";
import type { TerminalAgentUsage, TerminalView } from "../api";
import { usd } from "../format";
import type { TerminalState } from "./instance";

export type StatusFacts = {
  agent: { kind: string; label: string; model: string | null; effort: string | null; detected: boolean } | null;
  path: { short: string; full: string } | null;
  branch: { name: string; dirty: boolean; detached: boolean } | null;
  context: { used: number; window: number | null; pct: number | null } | null;
  cachePct: number | null;
  output: number | null;
  cost: { usd: number; approx: boolean } | null;
  waiting: { why: string | null } | null;
  command: { text: string; exit: number | null; running: boolean } | null;
};

/** Token counts the way a CLI's own status line writes them: 950, 8.2k, 249k, 1.2M. Latin `k` and `M`
 *  in both languages, as Claude Code and Codex print them: "11 тыс/249 тыс" does not fit beside a
 *  meter in a 300-pixel pane, and these are the units the reader sees in the terminal above it. */
export function shortTokens(n: number): string {
  if (n < 1000) return String(Math.round(n));
  if (n < 10_000) return t("term.status.tok.k", { n: (n / 1000).toFixed(1).replace(/\.0$/, "") });
  if (n < 1_000_000) return t("term.status.tok.k", { n: Math.round(n / 1000) });
  return t("term.status.tok.m", { n: (n / 1_000_000).toFixed(1).replace(/\.0$/, "") });
}

/** `~/work/bakery`, and only the last two folders of a deeper path (`…/src/lib`): the end of a path is
 *  the part that tells two panes apart. The whole path is in the tooltip. */
export function shortPath(path: string, home: string | null | undefined): string {
  let p = path;
  if (home && home !== "/" && (p === home || p.startsWith(home.endsWith("/") ? home : `${home}/`))) p = `~${p.slice(home.replace(/\/$/, "").length)}`;
  const sep = p.includes("\\") && !p.includes("/") ? "\\" : "/";
  const parts = p.split(sep).filter(Boolean);
  const folders = parts.length - (parts[0] === "~" ? 1 : 0);
  if (folders <= 3) return p || sep;
  return `…${sep}${parts.slice(-2).join(sep)}`;
}

function positive(n: number | null | undefined): number | null {
  return typeof n === "number" && Number.isFinite(n) && n > 0 ? n : null;
}

function contextOf(usage: TerminalAgentUsage | null | undefined): StatusFacts["context"] {
  const used = positive(usage?.context_tokens);
  if (used === null) return null;
  const window = positive(usage?.context_window);
  return { used, window, pct: window ? Math.min(100, Math.round((used / window) * 100)) : null };
}

/** The share of the prompt the provider's cache served: cached ÷ the whole prompt (which counts the
 *  cached part too). Absent without both counts. */
function cacheOf(usage: TerminalAgentUsage | null | undefined): number | null {
  const cached = positive(usage?.cache_read_tokens);
  const input = positive(usage?.input_tokens);
  if (cached === null || input === null) return null;
  return Math.min(100, Math.round((cached / input) * 100));
}

export function statusFacts(row: TerminalView | null | undefined, state?: TerminalState | null, home?: string | null): StatusFacts {
  const agent = row?.agent ?? null;
  const usage = agent?.usage ?? null;
  const cwd = state?.cwd || row?.cwd || "";
  const git = row?.git ?? null;
  const cost = typeof usage?.cost_usd === "number" && usage.cost_usd > 0 ? { usd: usage.cost_usd, approx: usage.source !== "metered" } : null;
  const waitingNow = agent?.state === "waiting" || row?.activity?.status === "permission" || row?.activity?.status === "question";
  const live = state?.commands.last ?? null;
  const shellCommand = live ? { text: live.command, exit: live.exitCode, running: live.result === "running" } : row?.last_command ? { text: row.last_command.command, exit: row.last_command.exit_code, running: false } : null;
  return {
    agent: agent ? { kind: agent.kind, label: agent.model || agent.kind, model: agent.model || null, effort: agent.effort || null, detected: agent.source === "detected" } : null,
    path: cwd ? { short: shortPath(cwd, home), full: cwd } : null,
    branch: git?.branch ? { name: git.branch, dirty: !!git.dirty, detached: !!git.detached } : null,
    context: contextOf(usage),
    cachePct: cacheOf(usage),
    output: positive(usage?.output_tokens),
    cost,
    waiting: waitingNow ? { why: agent?.waiting_for || row?.activity?.label || null } : null,
    // A staff member's terminal is its CLI, not a shell; the command line is the shell's fact.
    command: agent?.source === "staff" || !shellCommand?.text ? null : shellCommand,
  };
}

/** Whether there is anything to draw: an empty footer is hidden by the workspace's CSS. */
export function hasFacts(facts: StatusFacts): boolean {
  return !!(facts.agent || facts.path || facts.branch || facts.command || facts.waiting);
}

export function costText(cost: NonNullable<StatusFacts["cost"]>): string {
  return cost.approx ? t("term.status.cost.approx", { usd: usd(cost.usd) }) : usd(cost.usd);
}

export function contextText(context: NonNullable<StatusFacts["context"]>): string {
  return context.window ? `${shortTokens(context.used)}/${shortTokens(context.window)}` : shortTokens(context.used);
}

/** The tooltip: every fact in full, one per line, in the reader's language. */
export function statusTooltip(facts: StatusFacts): string {
  const lines: string[] = [];
  if (facts.waiting) lines.push(facts.waiting.why ? t("term.status.tip.waitingFor", { why: facts.waiting.why }) : t("term.status.waiting"));
  if (facts.agent) {
    lines.push(t(facts.agent.detected ? "term.status.tip.detected" : "term.status.tip.agent", { kind: facts.agent.kind }));
    if (facts.agent.model) lines.push(t("term.status.tip.model", { model: facts.agent.model }));
    if (facts.agent.effort) lines.push(t("term.status.tip.effort", { effort: facts.agent.effort }));
  }
  if (facts.path) lines.push(t("term.status.tip.folder", { path: facts.path.full }));
  if (facts.branch) lines.push(t(facts.branch.dirty ? "term.status.tip.branchDirty" : facts.branch.detached ? "term.status.tip.detached" : "term.status.tip.branch", { branch: facts.branch.name }));
  if (facts.context) {
    lines.push(facts.context.window
      ? t("term.status.tip.context", { used: facts.context.used.toLocaleString(), window: facts.context.window.toLocaleString(), pct: facts.context.pct ?? 0 })
      : t("term.status.tip.contextNoWindow", { used: facts.context.used.toLocaleString() }));
  }
  if (facts.cachePct !== null) lines.push(t("term.status.tip.cache", { pct: facts.cachePct }));
  if (facts.output !== null) lines.push(t("term.status.tip.output", { n: facts.output.toLocaleString() }));
  if (facts.cost) lines.push(t(facts.cost.approx ? "term.status.tip.costApprox" : "term.status.tip.cost", { usd: usd(facts.cost.usd) }));
  if (facts.command) {
    lines.push(facts.command.running
      ? t("term.status.tip.running", { command: facts.command.text })
      : t("term.status.tip.last", { command: facts.command.text, code: facts.command.exit ?? "?" }));
  }
  return lines.join("\n");
}
