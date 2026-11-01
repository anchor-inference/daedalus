// The rules of a workspace pane's compose line, the one-line field under an agent's terminal: which
// panes have one, the slash commands each CLI knows, when a paste folds into a chip, and the message
// the pieces make. Pure: the field is `compose.tsx`.

import type { TerminalView } from "../api";

/**
 * A few of each CLI's own slash commands, the ones worth sending from outside the terminal. A short
 * static list on purpose: the CLI is the authority on its commands and answers an unknown one itself,
 * so a hint that is missing costs nothing, and reading each CLI's list at run time would mean a
 * different parser for every release of four programs. pi and Cursor Agent get no hints.
 */
export const SLASH_COMMANDS: Record<string, readonly string[]> = {
  claude: ["compact", "clear", "context", "cost", "model", "review", "memory", "status", "init"],
  codex: ["compact", "new", "model", "approvals", "review", "diff", "status", "init", "mention"],
  grok: ["compact", "clear", "models", "help"],
  opencode: ["compact", "new", "models", "sessions", "share", "undo", "redo", "init", "help"],
};

/** The commands of the CLI that begin with what is typed, or null while the text is not a command
 *  being typed (no slash at the start, or a space already after the name). */
export function slashHints(harness: string | null | undefined, text: string): string[] | null {
  const m = /^\/(\S*)$/.exec(text);
  const all = harness ? SLASH_COMMANDS[harness] : undefined;
  if (!m || !all) return null;
  const typed = m[1].toLowerCase();
  const hits = all.filter((name) => name.startsWith(typed) && name !== typed);
  return hits.length ? hits : null;
}

/** A paste longer than this, or of more than one line, becomes a chip: the field is one line, and a
 *  block of text in it is unreadable and loses its line breaks. */
export const PASTE_FOLD_CHARS = 200;

export function foldsPaste(text: string): boolean {
  return text.includes("\n") || text.length > PASTE_FOLD_CHARS;
}

export function lineCount(text: string): number {
  return text.replace(/\n+$/, "").split("\n").length;
}

/** The most of a terminal selection a reply quotes; the agent can read the rest on its own screen. */
export const QUOTE_MAX = 2000;

/** A selection from the terminal as a quote: each line under "> ", trailing blanks gone, cut at
 *  `QUOTE_MAX`. Empty for a selection of nothing but whitespace. */
export function quoteOf(selection: string): string {
  const text = selection.replace(/\s+$/, "").replace(/^\s*\n/, "");
  if (!text.trim()) return "";
  const cut = text.length > QUOTE_MAX ? `${text.slice(0, QUOTE_MAX)}…` : text;
  return cut.split("\n").map((line) => `> ${line.replace(/\s+$/, "")}`).join("\n");
}

/** The message the line sends: the quote, then what was typed, then each folded paste, a blank line
 *  between them. Empty when there is nothing but whitespace. */
export function composeMessage(parts: { quote: string; typed: string; pastes: readonly string[] }): string {
  const pieces = [parts.quote, parts.typed.trim(), ...parts.pastes.map((p) => p.replace(/\s+$/, ""))].filter((p) => p.trim() !== "");
  return pieces.join("\n\n");
}

/**
 * Whether a pane gets a compose line: a staff member's terminal while it runs. What is written there
 * goes to the member as a staff message, through the delivery worker that never types into an open
 * permission or dialog. A plain shell — even one running a CLI by hand — has the terminal itself, and
 * a message to it would have no member to go to.
 */
export function showsCompose(row: Pick<TerminalView, "owner" | "status"> | null | undefined): boolean {
  return !!row && row.owner.kind === "staff" && !!row.owner.id && row.status === "running";
}
