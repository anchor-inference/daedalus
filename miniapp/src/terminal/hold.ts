// Who is typing into a terminal, as the pane's foot says it: an agent writes there and the operator
// may take the keyboard, the operator's own shell, an agent that waits for an answer, or a session
// that ended. Pure, so the rules are tested without a browser.

import type { TerminalView } from "../api";
import type { KeyboardOwner } from "./protocol";
import { paneState } from "./status";

export type Hold = "agent" | "held" | "ended" | "yours";

type Facts = Pick<TerminalView, "status" | "owner" | "created_by" | "activity" | "live" | "exit_code" | "exit_signal">;

/**
 * Whose keyboard the terminal is. A session's or a staff member's terminal is written to by an agent;
 * the operator's own (free, or a project's) is theirs. An agent that waits on the operator is `held`:
 * what the operator types does not reach it until the request is answered, so taking the keyboard
 * would promise something the terminal cannot do.
 */
export function holdOf(row: Facts | null | undefined, exited = false): Hold {
  if (!row) return "yours";
  if (row.status !== "running" || exited) return "ended";
  const agent = row.owner.kind === "session" || row.owner.kind === "staff" || (row.created_by ?? "").startsWith("agent:");
  if (!agent) return "yours";
  return paneState(row) === "waiting" && row.owner.kind === "staff" ? "held" : "agent";
}

/** The keyboard's holder: the live connection's word when a pane shows the terminal, else the listing's. */
export function keyboardOf(row: Pick<TerminalView, "live"> | null | undefined, live?: KeyboardOwner | null): KeyboardOwner {
  return live ?? row?.live?.keyboard.owner ?? "auto";
}
