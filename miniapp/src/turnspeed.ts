// How fast a turn generated, as the row under its answer says it. The host works the figure out of the
// run's calls in `usage_events` (daedalus/stores/provider_speed.py, `turn_speed`); this keeps what each
// read brought and says it in words.

import type { RunSpeed } from "./api";
import { fmtMs, fmtRate, int } from "./format";
import { t } from "./i18n";

/**
 * The speeds held so far with a read's added. A read carries the runs of the page it read, and a poll
 * reads one message, so the earlier runs are kept rather than replaced. The same object comes back when
 * nothing changed: the transcript's speed labels read it from a context, and a new object on every poll
 * re-rendered every one of them.
 */
export function mergeSpeeds(held: Record<string, RunSpeed>, fresh: Record<string, RunSpeed> | null | undefined): Record<string, RunSpeed> {
  if (!fresh) return held;
  let out = held;
  for (const [run, speed] of Object.entries(fresh)) {
    const old = held[run];
    if (old && old.tokens === speed.tokens && old.ms === speed.ms && old.model === speed.model) continue;
    if (out === held) out = { ...held };
    out[run] = speed;
  }
  return out;
}

/** "≈ 87 tok/s", and the hover's output, generation time and model. */
export function speedWords(speed: RunSpeed): { label: string; title: string } {
  return {
    label: t("turn.speed", { n: fmtRate(speed.rate) }),
    title: t("turn.speed.title", { tokens: int(speed.tokens), time: fmtMs(speed.ms), model: speed.model }),
  };
}
