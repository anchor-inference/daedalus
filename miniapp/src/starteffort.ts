// The effort a new chat starts with, picked on the start page before its session exists: shown over
// the model's own until the operator picks, then sent with the model in the session's first change.

import type { Preset } from "./api";

/** Off, or thinking at a level. */
export type Effort = { thinking: false } | { thinking: true; effort: string };

/** What the model's own effort is, before anything is picked: the preset's. */
export function presetEffort(preset: Preset | undefined): Effort {
  return preset?.thinking ? { thinking: true, effort: preset.reasoning_effort || "medium" } : { thinking: false };
}

/** The effort a picked value names: "off", or a level. */
export function effortOf(value: string): Effort {
  return value === "off" ? { thinking: false } : { thinking: true, effort: value };
}

/** The body the session's model route takes for an effort. */
export function effortBody(effort: Effort): { thinking: boolean; reasoning_effort?: string } {
  return effort.thinking ? { thinking: true, reasoning_effort: effort.effort } : { thinking: false };
}
