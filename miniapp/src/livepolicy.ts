// What a live link lets its holder do, as the window edits it and as the host stores it.
//
// The window keeps a form that is easy to edit (a mode per role, numbers as typed text) and turns it into
// the host's policy only when it saves. Keeping the two apart is what lets "lock everything to the current
// values" and "back to a set" be plain assignments instead of surgery on nested lists, and lets a half-typed
// budget ("2,") stay on screen without being sent.

import { t, plural } from "./i18n";

export type Billing = "subscription" | "metered";
export type RoleAccount = { id: string; name: string };
export type RolePreset = {
  id: string;
  label: string;
  provider: string;
  provider_name: string;
  model: string;
  billing: Billing;
  efforts: string[];
  accounts: RoleAccount[];
};
export type RoleValue = { preset: string; effort: string; account: string };
export type ShareRole = { id: string; label: string; pickable: boolean; current: RoleValue; presets: RolePreset[] };
export type Terminals = "all" | "container" | "none";
/** `withheld` is what the agent loses for the whole conversation while any link is live. */
export type LiveOptions = { roles: ShareRole[]; place: "container" | "host"; terminals: Terminals[]; withheld?: { id: string; label: string; why: string }[] };

export type AllowedEntry = { preset: string; accounts: string[]; efforts: string[] };
export type RolePolicy = RoleValue & { allowed: AllowedEntry[] };
export type LivePolicy = {
  roles?: Record<string, RolePolicy>;
  budget_usd?: number | null;
  budget_weekly_pct?: number | null;
  terminals?: Terminals;
};

/** `locked`: the visitor gets the value and nothing else. `set`: the value or one of `allowed`. */
export type RoleForm = RoleValue & { mode: "locked" | "set"; allowed: AllowedEntry[] };
export type LiveForm = {
  roles: Record<string, RoleForm>;
  /** As typed; empty is no limit. */
  budgetUsd: string;
  budgetPct: string;
  terminals: Terminals;
};

/** A form for a new link: every role locked to what the session uses now, the safest start. */
export function freshForm(options: LiveOptions): LiveForm {
  const roles: Record<string, RoleForm> = {};
  for (const role of options.roles) roles[role.id] = { ...role.current, mode: "locked", allowed: [] };
  const terminals: Terminals = options.terminals.includes("none") ? "none" : options.terminals[0] ?? "none";
  return { roles, budgetUsd: "", budgetPct: "", terminals };
}

/** The form an existing link's policy opens as. A role the policy leaves out is shown at the session's
 *  current value and locked, as a new link's would be: saving then writes it down. */
export function formOf(policy: LivePolicy | undefined, options: LiveOptions): LiveForm {
  const form = freshForm(options);
  for (const [id, kept] of Object.entries(policy?.roles ?? {})) {
    if (!form.roles[id]) continue;
    form.roles[id] = { preset: kept.preset, effort: kept.effort ?? "", account: kept.account ?? "", mode: kept.allowed?.length ? "set" : "locked", allowed: kept.allowed ?? [] };
  }
  form.budgetUsd = policy?.budget_usd != null ? String(policy.budget_usd) : "";
  form.budgetPct = policy?.budget_weekly_pct != null ? String(policy.budget_weekly_pct) : "";
  if (policy?.terminals && options.terminals.includes(policy.terminals)) form.terminals = policy.terminals;
  return form;
}

/** A typed amount: a comma is a decimal point (Russian keyboards type one), empty is no limit, and
 *  anything else that is not a positive number is `NaN`, which the window shows as an error. */
export function amount(text: string): number | null {
  const clean = text.trim().replace(",", ".");
  if (!clean) return null;
  const value = Number(clean);
  return Number.isFinite(value) && value > 0 ? value : NaN;
}

/** The host's policy from the form. A role that cannot be picked by the visitor never carries a set,
 *  whatever the form held before the options said so. */
export function buildPolicy(form: LiveForm, options: LiveOptions): LivePolicy {
  const roles: Record<string, RolePolicy> = {};
  for (const role of options.roles) {
    const kept = form.roles[role.id];
    if (!kept) continue;
    const allowed = role.pickable && kept.mode === "set" ? kept.allowed.filter((entry) => entry.preset).map((entry) => ({ preset: entry.preset, accounts: [...entry.accounts], efforts: [...entry.efforts] })) : [];
    roles[role.id] = { preset: kept.preset, effort: kept.effort, account: kept.account, allowed };
  }
  const usd = amount(form.budgetUsd);
  const pct = amount(form.budgetPct);
  return { roles, budget_usd: usd === null || Number.isNaN(usd) ? null : usd, budget_weekly_pct: pct === null || Number.isNaN(pct) ? null : pct, terminals: form.terminals };
}

/** Which ways of paying the models a visitor can reach use: it decides which limit is the one that bites. */
export function billingsInUse(form: LiveForm, options: LiveOptions): Set<Billing> {
  const found = new Set<Billing>();
  for (const role of options.roles) {
    const kept = form.roles[role.id];
    if (!kept) continue;
    const ids = [kept.preset, ...(role.pickable && kept.mode === "set" ? kept.allowed.map((entry) => entry.preset) : [])];
    for (const id of ids) {
      const preset = role.presets.find((item) => item.id === id);
      if (preset) found.add(preset.billing);
    }
  }
  return found;
}

/** Every role back to the session's current value, locked: the one-press safe state. */
export function lockAll(form: LiveForm, options: LiveOptions): LiveForm {
  const roles = { ...form.roles };
  for (const role of options.roles) roles[role.id] = { ...role.current, mode: "locked", allowed: [] };
  return { ...form, roles };
}

/** Turns one preset in or out of a role's set. */
export function toggleAllowed(allowed: AllowedEntry[], preset: string, on: boolean): AllowedEntry[] {
  const has = allowed.some((entry) => entry.preset === preset);
  if (on && !has) return [...allowed, { preset, accounts: [], efforts: [] }];
  if (!on && has) return allowed.filter((entry) => entry.preset !== preset);
  return allowed;
}

/** One effort or account of an allowed preset in or out; an empty list means "any of them". */
export function toggleIn(list: string[], value: string, on: boolean): string[] {
  return on ? (list.includes(value) ? list : [...list, value]) : list.filter((item) => item !== value);
}

/** What the holder of the link will be able to do, in one or two sentences built from the settings. */
export function summarize(form: LiveForm, options: LiveOptions, password: boolean): string {
  const parts: string[] = [];
  parts.push(t(password ? "live.sum.password" : "live.sum.nopassword"));
  const main = options.roles.find((role) => role.id === "main") ?? options.roles[0];
  const open = options.roles.filter((role) => role.pickable && form.roles[role.id]?.mode === "set" && form.roles[role.id].allowed.length > 0);
  if (main && form.roles[main.id]) {
    const kept = form.roles[main.id];
    const label = main.presets.find((item) => item.id === kept.preset)?.label ?? kept.preset;
    parts.push(main.pickable && kept.mode === "set" && kept.allowed.length
      ? t("live.sum.main.set", { model: label, n: String(kept.allowed.length) })
      : t("live.sum.main.locked", { model: label }));
  }
  const others = open.filter((role) => role.id !== main?.id).length;
  parts.push(others ? plural("live.sum.others.open", others) : t("live.sum.others.locked"));
  const usd = amount(form.budgetUsd);
  const pct = amount(form.budgetPct);
  const limits = [usd && !Number.isNaN(usd) ? t("live.sum.usd", { n: formatUsd(usd) }) : "", pct && !Number.isNaN(pct) ? t("live.sum.pct", { n: formatPct(pct) }) : ""].filter(Boolean);
  parts.push(limits.length ? t("live.sum.limits", { limits: limits.join(t("live.sum.and")) }) : t("live.sum.nolimits"));
  parts.push(t(`live.sum.term.${form.terminals}`));
  if (options.withheld?.length) parts.push(plural("live.sum.withheld", options.withheld.length));
  return parts.join(" ");
}

export function formatUsd(value: number): string {
  return `$${value < 10 ? value.toFixed(2) : value.toFixed(value % 1 ? 2 : 0)}`;
}

export function formatPct(value: number): string {
  return `${Number(value.toFixed(value < 10 ? 1 : 0))}%`;
}

/** A password nobody has to think up: four groups of letters and digits without the ones that look alike. */
export function generatePassword(random: (n: number) => number = (n) => crypto.getRandomValues(new Uint32Array(1))[0] % n): string {
  const letters = "abcdefghjkmnpqrstuvwxyz23456789";
  const group = () => Array.from({ length: 4 }, () => letters[random(letters.length)]).join("");
  return [group(), group(), group(), group()].join("-");
}

/** The field a host's refusal names, mapped to the section of the window that shows it. */
export function fieldSection(field: string): "access" | "models" | "limits" | "terminals" | "" {
  if (/^(label|password|expires_hours|clear_password)/.test(field)) return "access";
  if (/^policy\.roles/.test(field)) return "models";
  if (/^policy\.budget/.test(field)) return "limits";
  if (/^policy\.terminals/.test(field)) return "terminals";
  return "";
}
