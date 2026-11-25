// The configured models as rows of a selection list: which provider, the model's own id, its context
// window, what it costs or that it is paid for by a plan, and what it can do. One place makes these,
// so the helpers' list in Details, the fallback chain and every single-model setting describe a model
// the same way, and a model gains a fact (an account, say) for all of them at once.

import type { ModelHealth, Preset, PriceView, ProviderConf } from "./api";
import { tokens } from "./format";
import { t } from "./i18n";
import { ProviderMark, providerName } from "./ui/provider-mark";
import { SelectionSingle, type SelectionGroup, type SelectionItem } from "./ui/selection";

export type ModelCatalogue = {
  presets: Record<string, Preset>;
  providers?: Record<string, ProviderConf>;
  prices?: Record<string, PriceView>;
  model_health?: Record<string, ModelHealth>;
};

/** A preset's broken or slow mark as a tag, with the upstream's words or the measured wait as its reason;
 *  nothing for a model that is fine or not known yet. */
export function healthAlert(health: ModelHealth | undefined): { word: string; tone: "bad" | "warn"; why?: string } | undefined {
  if (health?.state === "broken") return { word: t("modelhealth.broken"), tone: "bad", why: health.detail || undefined };
  if (health?.state === "slow") {
    const ms = health.median_ms ?? health.check?.ms;
    return { word: t("modelhealth.slow"), tone: "warn", why: ms ? t("modelhealth.slow.why", { s: (ms / 1000).toFixed(1) }) : undefined };
  }
  return undefined;
}

/** "$1.25 / $10" per million tokens in and out; whole dollars lose their cents. */
function rate(value: number): string {
  return `$${Number.isInteger(value) ? value : value.toFixed(value < 1 ? 3 : 2).replace(/0+$/, "")}`;
}

/** What a model costs, in a few characters: a plan, free, its two rates, or nothing where nothing knows. */
export function priceFact(preset: Preset, price: PriceView | undefined, provider: ProviderConf | undefined): string {
  if (price?.plan || provider?.billing === "subscription") return t("modelitem.plan");
  if (preset.free_only || (price && price.input === 0 && price.output === 0)) return t("modelitem.free");
  if (price && price.input != null && price.output != null) return t("modelitem.rates", { input: rate(price.input), output: rate(price.output) });
  return "";
}

/** One model as a row: its label, provider and id under it, and its facts. */
export function modelItem(id: string, preset: Preset, catalogue: Omit<ModelCatalogue, "presets">): SelectionItem {
  // A preset written by hand can lack its provider or model; the row then falls back to the id
  // rather than taking the whole settings page down with it.
  const providerId = preset.provider ?? "";
  const conf = catalogue.providers?.[providerId];
  const facts = [
    preset.context_window ? t("modelitem.window", { n: tokens(preset.context_window) }) : "",
    priceFact(preset, catalogue.prices?.[id], conf),
    preset.images ? t("modelitem.vision") : "",
    preset.thinking ? t("modelitem.thinking") : "",
  ].filter(Boolean);
  const provider = providerId ? providerName(providerId, conf) : "";
  const model = preset.model ?? "";
  return {
    id,
    label: preset.label || (providerId && model ? `${providerId}/${model}` : id),
    detail: [provider, model].filter(Boolean).join(" · ") || undefined,
    facts,
    alert: healthAlert(catalogue.model_health?.[id]),
    group: providerId || undefined,
    keywords: `${providerId} ${provider} ${model}`,
  };
}

/** Every configured model as rows, under one group per provider headed by its mark and name. */
export function modelItems(catalogue: ModelCatalogue, keep?: (id: string, preset: Preset) => boolean): { items: SelectionItem[]; groups: SelectionGroup[] } {
  const items: SelectionItem[] = [];
  const groups = new Map<string, SelectionGroup>();
  for (const [id, preset] of Object.entries(catalogue.presets)) {
    if (keep && !keep(id, preset)) continue;
    items.push(modelItem(id, preset, catalogue));
    const providerId = preset.provider ?? "";
    if (providerId && !groups.has(providerId)) {
      const conf = catalogue.providers?.[providerId];
      const name = providerName(providerId, conf);
      groups.set(providerId, { id: providerId, label: name, mark: <ProviderMark id={providerId} kind={conf?.kind} name={name} className="sel-mark" /> });
    }
  }
  return { items, groups: [...groups.values()] };
}

/**
 * One configured model, chosen in place: the model in use as its full row, the searchable list a
 * "Change" away. It stands where a plain dropdown named each model by its label and nothing else.
 * `defaultOption` is the empty choice ("the session's own model", "automatic"); `keep` narrows the
 * list (only the models that see images, say); `tag` adds a word of the setting's own to a row, such
 * as which model "strongest" means.
 */
export function ModelPicker({ label, catalogue, value, onChange, defaultOption, keep, tag, disabled, id }: {
  label: string;
  catalogue: ModelCatalogue;
  value: string;
  onChange: (next: string) => void;
  defaultOption?: { label: string; detail?: string };
  keep?: (id: string, preset: Preset) => boolean;
  tag?: (id: string) => string | undefined;
  disabled?: boolean;
  id?: string;
}) {
  const { items, groups } = modelItems(catalogue, keep);
  const tagged = tag ? items.map((item) => { const word = tag(item.id); return word ? { ...item, facts: [word, ...(item.facts ?? [])] } : item; }) : items;
  return <SelectionSingle id={id} label={label} items={tagged} groups={groups} value={value} onChange={onChange} defaultOption={defaultOption} disabled={disabled} placeholder={t("modelitem.search")} />;
}
