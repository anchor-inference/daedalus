// The arithmetic behind the Details tab's pictures: how full a meter is and in which colour, how the
// context's parts split one bar, and what a host's sentence about a dropped original actually said.
// Kept apart from the components so each rule is tested on numbers instead of on drawn pages.

export type Tone = "ok" | "attn" | "bad";

/** A subscription window: green while there is room, amber from 80 %, red once it is spent. The
 *  thresholds are the ones the bars always used; only where they are written down moved. */
export function quotaTone(percent: number): Tone {
  if (percent >= 100) return "bad";
  if (percent >= 80) return "attn";
  return "ok";
}

/** The context window: it is meant to fill, so it stays neutral until 60 % and turns red at 90 %,
 *  where a compaction is near. `null` is the neutral case, painted in the accent. */
export function contextTone(percent: number): Exclude<Tone, "ok"> | null {
  if (percent >= 90) return "bad";
  if (percent >= 60) return "attn";
  return null;
}

export const CONTEXT_PARTS = ["instructions", "tools", "conversation", "attachments", "reserved_response"] as const;
export type ContextPart = (typeof CONTEXT_PARTS)[number];

export type Segment = { key: ContextPart; tokens: number; percent: number };

/**
 * The breakdown as widths on one bar. The bar stands for the window, so the empty end is the room
 * left; the parts are estimated from request bytes while the window is the provider's figure, and
 * when the estimate overshoots the window the bar stands for the parts' sum instead, never past 100.
 * Every part is returned, a zero one with no width, so the legend always lists the same five.
 */
export function contextSegments(breakdown: Partial<Record<ContextPart, number>> | null | undefined, window: number): Segment[] {
  const values = CONTEXT_PARTS.map((key) => ({ key, tokens: Math.max(0, Number(breakdown?.[key]) || 0) }));
  const sum = values.reduce((total, part) => total + part.tokens, 0);
  const whole = Math.max(window > 0 ? window : 0, sum);
  return values.map((part) => ({ ...part, percent: whole > 0 ? (100 * part.tokens) / whole : 0 }));
}

export type OriginalNote = { size: number | null; limit: number } | null;

/**
 * The sizes in the host's reason for not keeping an imported session's original. The host writes it
 * as a sentence with byte counts ("the original is 188057562 bytes, over the 67108864 kept", or
 * "the original is over the 67108864 bytes kept" when it only learns mid-read), and the records
 * already made carry that sentence, so it is read here rather than changed at the source. Any other
 * reason (a daemon that did not answer) is not about size and comes back `null`, to be shown as said.
 */
export function originalNote(reason: string | null | undefined): OriginalNote {
  if (!reason) return null;
  const sized = /^the original is (\d+) bytes, over the (\d+) kept$/.exec(reason.trim());
  if (sized) return { size: Number(sized[1]), limit: Number(sized[2]) };
  const over = /^the original is over the (\d+) bytes kept$/.exec(reason.trim());
  if (over) return { size: null, limit: Number(over[1]) };
  return null;
}
