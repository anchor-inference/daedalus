// What the app knows about a model before it is a preset: what an endpoint published about it, and
// the rules for turning that into the thing that is saved.
//
// It sits apart from the screen that shows it because these are the rules a mistake here is
// expensive in: a price, a window or a modality recorded against the wrong model is wrong in every
// figure read from it afterwards — every spend total, every cap, every usage chart — and nothing in
// the app ever says so. They are worth testing on their own.

import type { Preset } from "./api";

/** One model as an endpoint described it. Only the id is ever certain. */
export type ModelEntry = {
  id: string;
  name?: string;
  context_length?: number;
  max_output_tokens?: number;
  input_modalities?: string[];
  images?: boolean;
  reasoning?: boolean;
  pricing?: { input?: number; output?: number; cache_hit?: number };
};

/** Efforts a preset or a session may ask for. Hosted vendors map these onto their own names. */
export const REASONING_EFFORTS = ["low", "medium", "high", "xhigh"] as const;
export type ReasoningEffort = (typeof REASONING_EFFORTS)[number];

/** Index on the effort slider; an unknown or empty value sits on medium, the preset default. */
export function effortIndex(effort: string | undefined): number {
  const i = REASONING_EFFORTS.indexOf(effort as ReasoningEffort);
  return i >= 0 ? i : 1;
}

/** A preset with nothing of any model in it: the conservative answer, not an empty one. */
export const BLANK: Preset = { provider: "", model: "", label: "", thinking: true, reasoning_effort: "medium", images: false, context_window: 128000, max_output_tokens: 32000 };

/** What the endpoint said about the model that was picked, and what the form was filled in with from it. */
export type Picked = { preset: Preset; pricing: ModelEntry["pricing"] | null };

export type PricingDraft = {
  model: string;
  input: string;
  output: string;
  cache_hit: string;
  input_limit: string;
  limit_source: string;
};

export type PricingEntry = { input: number; output: number; cache_hit?: number; input_limit?: number; limit_source?: string };

/** An edited rate or ceiling is useful only for the exact model it describes. */
export function pricingFromDraft(model: string, draft: PricingDraft | null): { entry: PricingEntry | null; error: "rates" | "ceiling" | "source" | null } {
  if (!draft || draft.model !== model) return { entry: null, error: null };
  const fields = [draft.input, draft.output, draft.cache_hit, draft.input_limit, draft.limit_source];
  if (fields.every((value) => !value.trim())) return { entry: null, error: null };
  const rate = (value: string) => value.trim() !== "" && Number.isFinite(Number(value)) && Number(value) >= 0;
  if (!rate(draft.input) || !rate(draft.output) || (draft.cache_hit.trim() && !rate(draft.cache_hit)))
    return { entry: null, error: "rates" };
  const entry: PricingEntry = { input: Number(draft.input), output: Number(draft.output) };
  if (draft.cache_hit.trim()) entry.cache_hit = Number(draft.cache_hit);
  if (draft.input_limit.trim() || draft.limit_source.trim()) {
    const limit = Number(draft.input_limit);
    if (!draft.input_limit.trim() || !Number.isSafeInteger(limit) || limit <= 0) return { entry: null, error: "ceiling" };
    if (!draft.limit_source.trim()) return { entry: null, error: "source" };
    entry.input_limit = limit;
    entry.limit_source = draft.limit_source.trim();
  }
  return { entry, error: null };
}

/** Fill the editable form from one model entry returned by the provider lookup API. */
export function prefilled(entry: ModelEntry, current: Preset): Preset {
  return {
    ...current,
    model: entry.id,
    label: entry.name && entry.name !== entry.id ? entry.name : current.label,
    images: entry.images ?? current.images,
    thinking: entry.reasoning ?? current.thinking,
    // The history a run may hold is capped below a very large hosted window on purpose: every turn
    // pays for the context it carries. llama.cpp windows at or below that cap are preserved exactly.
    context_window: entry.context_length ? Math.min(entry.context_length, 400000) : current.context_window,
    max_output_tokens: entry.max_output_tokens ? Math.min(entry.max_output_tokens, 64000) : current.max_output_tokens,
  };
}

/**
 * Whether runs on a preset hold the tool groups on demand back: its own switch, else the model's
 * known behaviour as the host reported it, else on — the host's own default for a model it knows
 * nothing about.
 */
export function onDemandGroups(preset: Pick<Preset, "on_demand_tool_groups">, byModel: boolean | undefined): boolean {
  if (preset.on_demand_tool_groups === true || preset.on_demand_tool_groups === false) return preset.on_demand_tool_groups;
  return byModel ?? true;
}

/** The three positions of the switch, in the order they are offered: the model's own first. */
export const ON_DEMAND_CHOICES: readonly (boolean | null)[] = [null, true, false];

/** A model id turned into a preset id: the same rule the server accepts (letters, digits, . _ -). */
export function presetIdFor(provider: string, model: string): string {
  const slug = model.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^[.-]+|[.-]+$/g, "");
  return `${provider}.${slug || "model"}`;
}

/**
 * The form, for a model id typed by hand.
 *
 * Everything the form holds came from one catalogue entry, so for any other id it describes the
 * wrong model. Typing a different id therefore leaves nothing of the pick standing; typing the
 * picked id back changes nothing.
 */
export function retyped(value: string, picked: Picked): Picked {
  if (value.trim() === picked.preset.model) return picked;
  return { preset: { ...BLANK, provider: picked.preset.provider }, pricing: null };
}

/** The price to record against `model`, which is the endpoint's price only while it is that model's. */
export function priceFor(model: string, picked: Picked): ModelEntry["pricing"] | null {
  if (!model || model !== picked.preset.model) return null;
  const p = picked.pricing;
  return p?.input !== undefined && p?.output !== undefined ? p : null;
}

/**
 * The preset a project orchestrator runs unless its project names one: the one chosen in Settings,
 * or, while none is (or the chosen one was removed), the one the host judges strongest. The select
 * shows that one chosen, so what it shows is what runs.
 */
export function orchestratorPreset(presets: Record<string, Preset>, chosen: string | undefined, strongest: string | undefined): string {
  if (chosen && chosen in presets) return chosen;
  if (strongest && strongest in presets) return strongest;
  return Object.keys(presets)[0] ?? "";
}
