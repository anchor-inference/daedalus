// What the Details tab beside a command-line member says, as values: how full its context was, the
// spend and the rate-limit windows its CLI reported, how the conversation is made up, how long the
// session has run. Everything here is read from what the host recorded; a number the CLI did not
// report stays null, and the tab says "not reported" rather than drawing a guess.

import type { KeptLine, StaffSessionView, StaffTurn, StaffUsage } from "../api";

/** The context's fill at the end of the last turn. The share needs the window, which only some CLIs
 *  report (Codex does, Claude Code's transcript does not): without it the tokens stand alone. */
export function contextFill(usage: StaffUsage | null | undefined): { tokens: number; window: number | null; pct: number | null } | null {
  const tokens = usage?.context_tokens;
  if (typeof tokens !== "number" || tokens <= 0) return null;
  const window = typeof usage?.context_window === "number" && usage.context_window > 0 ? usage.context_window : null;
  return { tokens, window, pct: window ? Math.min(100, Math.round((100 * tokens) / window)) : null };
}

/** A rate-limit window's name from its length: the five hours and the week read as the CLI's own
 *  screens name them; any other length as its hours or days. */
export function windowName(minutes: number): { key: string; n?: number } {
  if (minutes === 300) return { key: "staff.details.window.5h" };
  if (minutes === 10080) return { key: "staff.details.window.week" };
  if (minutes > 0 && minutes % 1440 === 0) return { key: "staff.details.window.days", n: minutes / 1440 };
  if (minutes > 0) return { key: "staff.details.window.hours", n: Math.max(1, Math.round(minutes / 60)) };
  return { key: "staff.details.window.other" };
}

/** Who spoke how often in the transcript: the operator's prompts, the orchestrator's, the replies. */
export function conversationCounts(turns: Pick<StaffTurn, "role">[]): { operator: number; orchestrator: number; replies: number } {
  let operator = 0;
  let orchestrator = 0;
  let replies = 0;
  for (const turn of turns) {
    if (turn.role === "user") operator += 1;
    else if (turn.role === "orchestrator") orchestrator += 1;
    else if (turn.role === "assistant") replies += 1;
  }
  return { operator, orchestrator, replies };
}

/** How long the live session has run, in milliseconds; null without one or without a start. */
export function sessionAge(view: StaffSessionView | null | undefined, now = Date.now()): number | null {
  const started = view?.session?.started_at ? Date.parse(view.session.started_at) : NaN;
  return Number.isFinite(started) ? Math.max(0, now - started) : null;
}

/** Whether the recorded spend says anything at all: a snapshot of zeros is a turn not yet read. */
export function hasSpend(usage: StaffUsage | null | undefined): boolean {
  return !!usage && ((usage.input_tokens ?? 0) > 0 || (usage.output_tokens ?? 0) > 0 || (usage.cost_usd ?? 0) > 0);
}

/** A "how it is kept" line as dictionary keys: the setting's label, the sentence and the kind of
 *  keeping. A reason the app has no words for falls back to the kind alone, so a code added on the
 *  host reads as "kept by the host" rather than as a raw key. */
export function keptWords(line: KeptLine, known: (key: string) => boolean): { label: string; text: string | null; kind: string } {
  const text = `staff.kept.${line.setting}.${line.kept}.${line.reason}`;
  return { label: `staff.kept.setting.${line.setting}`, text: known(text) ? text : null, kind: `staff.kept.kind.${line.kept}` };
}
