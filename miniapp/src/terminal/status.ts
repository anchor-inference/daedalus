// A terminal connection's state in words, for the strip over the terminal, and what a terminal is
// doing as one word for its dot.

import { t } from "../i18n";
import type { TerminalView } from "../api";
import type { ConnectionState } from "./connection";
import type { TerminalState } from "./instance";

export function connectionText(state: ConnectionState): string {
  switch (state.kind) {
    case "connecting":
      return t("term.state.connecting");
    case "live":
      return t("term.state.live");
    case "reconnecting":
      return state.delayMs >= 1000 ? t("term.state.reconnectingIn", { n: Math.round(state.delayMs / 1000) }) : t("term.state.reconnecting");
    case "exited":
      return state.code === null && state.signal ? t("term.state.exitedSignal", { signal: state.signal }) : t("term.state.exited", { code: state.code ?? "?" });
    case "proxy-blocked":
      return t("term.state.proxy");
    case "unavailable":
      switch (state.reason) {
        case "gone":
          return t("term.state.gone");
        case "environment":
          return t("term.state.environment");
        case "origin":
          return t("term.state.origin");
        case "auth":
          return t("term.state.auth");
      }
  }
}

/**
 * What a terminal is doing, as one word a pane's dot, a tab and a list row all draw: the same
 * function everywhere, so the tab strip can never say "working" about a pane whose dot says "waiting".
 *
 * - `waiting`: something waits on the operator (a staff member's permission or question).
 * - `working`: an agent is working, or a shell's command is still running.
 * - `done`: an agent finished a turn nobody has read, or output arrived while nobody looked.
 * - `failed`: the agent reported an error, a shell's last command failed, or the program ended badly.
 * - `ended`: the program ended cleanly, or the terminal was lost.
 * - `idle`: none of the above.
 *
 * The host's activity wins over what this page saw on the screen, because it knows about requests
 * the screen cannot show. The live state is optional: a list row of a terminal no pane shows has none.
 */
export type PaneState = "working" | "waiting" | "done" | "failed" | "ended" | "idle";

export function paneState(row: Pick<TerminalView, "status" | "exit_code" | "exit_signal" | "activity" | "live"> | null | undefined, state?: TerminalState | null): PaneState {
  const exit = state?.exit;
  if ((row && row.status !== "running") || exit) {
    const code = exit ? exit.code : row?.exit_code;
    const signal = exit ? exit.signal : row?.exit_signal;
    return row?.status === "lost" ? "ended" : signal || (code ?? 0) !== 0 ? "failed" : "ended";
  }
  const activity = row?.activity;
  if (activity) {
    if (activity.status === "permission" || activity.status === "question" || activity.action?.kind === "answer" || (activity.level === "warn" && activity.action)) return "waiting";
    if (activity.status === "working" || activity.status === "starting") return "working";
    if (activity.status === "turn_done_unseen") return "done";
    if (activity.status === "error" || activity.level === "bad") return "failed";
    if (activity.status) return "idle";
  }
  if (state?.commands.last?.result === "running" || row?.live?.busy) return "working";
  // A shell whose last command failed says so, as the dock's tab does: it is the pane to look at.
  if (state?.commands.last?.result === "failed") return "failed";
  if (state?.unseen || state?.bell) return "done";
  return "idle";
}
