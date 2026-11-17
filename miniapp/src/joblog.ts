// A job's log as the Jobs tab follows it: the pieces the host hands over, joined into lines the way a
// terminal would have left them on screen, and only the newest of them kept. Kept apart from the view
// so the joining, the cap and the catching-up rules are tested without a browser.

import type { JobLogPiece } from "./api";

/** The lines kept for drawing. A build that prints a million lines is followed at its end; drawing all
 *  of them made the tab a second's work per piece and the whole page slow with it. */
export const JOB_LOG_LINES = 4000;
/** How much one read asks for. */
export const JOB_LOG_PIECE = 256 * 1024;
/** How often a running job's log is asked for what is new. A quiet job answers with nothing. */
export const JOB_LOG_POLL_MS = 1000;
/** How far behind the reader may fall before it skips to the tail rather than read its way there:
 *  more than this is more lines than are kept, so reading it would only be thrown away. */
export const JOB_LOG_BEHIND = 2 * 1024 * 1024;
/** An unfinished line is held raw so the rest of it can still come; a progress bar that redraws one line
 *  for an hour never finishes it, and only its last drawing is kept past this many characters. */
const PARTIAL_MAX = 8192;

export type JobLog = {
  /** The finished lines, oldest first, cleaned. */
  lines: string[];
  /** The line being written, raw: completed by the next piece, or drawn as it stands. */
  partial: string;
  /** Lines dropped off the top to keep the cap, and whether the view starts after the log's own start. */
  dropped: number;
  cut: boolean;
  /** The byte to ask from next; negative asks for the tail. */
  next: number;
};

export const EMPTY_JOB_LOG: JobLog = { lines: [], partial: "", dropped: 0, cut: false, next: -1 };

// Colours, cursor moves and window titles: what a terminal acts on and does not print.
const ESCAPES = /\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]/g;

/** A raw line as a terminal leaves it: escapes gone, and of the drawings a carriage return separates,
 *  the last one with anything in it (a progress bar's final state, not its hundred redraws). */
export function screenLine(raw: string): string {
  const plain = raw.replace(ESCAPES, "");
  if (!plain.includes("\r")) return plain;
  const drawings = plain.split("\r").filter(Boolean);
  return drawings.length ? drawings[drawings.length - 1] : "";
}

/** A long unfinished line cut down to its last drawing, which is all that is shown of it. */
function shortPartial(partial: string): string {
  if (partial.length <= PARTIAL_MAX) return partial;
  const body = partial.endsWith("\r") ? partial.slice(0, -1) : partial;
  const cr = body.lastIndexOf("\r");
  return cr >= 0 ? partial.slice(cr) : partial.slice(-PARTIAL_MAX);
}

/** The log with one more piece taken in. */
export function takePiece(log: JobLog, piece: JobLogPiece, cap = JOB_LOG_LINES): JobLog {
  if (piece.lines_only) {
    const lines = piece.text.split("\n").map(screenLine);
    const over = Math.max(0, lines.length - cap);
    return { lines: lines.slice(over), partial: "", dropped: 0, cut: true, next: piece.next };
  }
  const base = piece.restart ? { ...EMPTY_JOB_LOG, cut: piece.start > 0 } : log;
  let text = base.partial + piece.text;
  // A tail read starts somewhere inside a line; the half of it would read as a line of its own.
  if (piece.restart && piece.start > 0) {
    const newline = text.indexOf("\n");
    if (newline >= 0) text = text.slice(newline + 1);
  }
  const parts = text.split("\n");
  const partial = shortPartial(parts.pop() ?? "");
  const lines = parts.length ? base.lines.concat(parts.map(screenLine)) : base.lines;
  const over = Math.max(0, lines.length - cap);
  return { lines: over ? lines.slice(over) : lines, partial, dropped: base.dropped + over, cut: base.cut, next: piece.next };
}

/** Where the next read starts: on from the last, or the tail again when it has fallen too far behind. */
export function nextOffset(piece: JobLogPiece): number {
  if (piece.lines_only) return -1;
  return piece.size - piece.next > JOB_LOG_BEHIND ? -1 : piece.next;
}

/** Whether there is more to read now, without waiting: the reader is behind what the log holds. */
export function behind(piece: JobLogPiece): boolean {
  return !piece.lines_only && piece.next < piece.size;
}

/** Whether to go on asking: the job runs, or it has ended with bytes not read yet. */
export function stillFollowing(piece: JobLogPiece): boolean {
  return piece.running || behind(piece);
}

/** The lines to draw: the finished ones, then the one being written. */
export function shownLines(log: JobLog): string[] {
  if (!log.partial) return log.lines;
  const last = screenLine(log.partial);
  return last ? [...log.lines, last] : log.lines;
}
