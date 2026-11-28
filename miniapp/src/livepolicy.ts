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
/** Who picks within a role's set: the visitor (the model picker on their screen), the agent (a helper's
 *  model, named when it starts one) or nobody (the role has one value). */
export type Chooser = "visitor" | "agent" | "none";
/** `current.allowed` is the set the session uses now for a role the agent chooses in (its list of helper
 *  models); a role the visitor chooses in has none, since outside a link the visitor does not exist. */
export type ShareRole = { id: string; label: string; pickable: boolean; chooser: Chooser; current: RoleValue & { allowed?: AllowedEntry[] }; presets: RolePreset[] };
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

/** `locked`: the role runs on the value and nothing else. `set`: on one of `allowed`, which in the form
 *  always holds the value's own preset (see `withOwn`), so that preset can be given efforts and accounts
 *  of its own like any other member. */
export type RoleForm = RoleValue & { mode: "locked" | "set"; allowed: AllowedEntry[] };
export type LiveForm = {
  roles: Record<string, RoleForm>;
  /** As typed; empty is no limit. */
  budgetUsd: string;
  budgetPct: string;
  terminals: Terminals;
};

const valueOf = (role: ShareRole): RoleValue => ({ preset: role.current.preset, effort: role.current.effort, account: role.current.account });

/** The entry that lets the value and nothing more: its own effort and account. */
export function ownEntry(value: RoleValue): AllowedEntry {
  return { preset: value.preset, accounts: value.account ? [value.account] : [], efforts: value.effort ? [value.effort] : [] };
}

/** A list of efforts or accounts that names some keeps the value's among them: the value is always
 *  allowed, and a chip row that let it be pressed off would show a set the host does not keep. */
function keeping(list: string[], own: string): string[] {
  return list.length && own && !list.includes(own) ? [own, ...list] : list;
}

/** A role in a set, with the value's own preset first in it and carrying the value's effort and account.
 *  The operator's wish this serves most is "this model, but low, medium or high", which a set that could
 *  not hold the value's own preset had no way to say. */
export function withOwn(form: RoleForm): RoleForm {
  if (form.mode !== "set" || !form.preset) return form;
  const found = form.allowed.find((entry) => entry.preset === form.preset);
  const own = found ? { ...found, efforts: keeping(found.efforts, form.effort), accounts: keeping(found.accounts, form.account) } : ownEntry(form);
  return { ...form, allowed: [own, ...form.allowed.filter((entry) => entry.preset !== form.preset)] };
}

/** Whether an entry of the value's own preset lets nothing beyond the value: then the policy leaves it
 *  out, and a set of only that entry is the same as "only this". */
export function onlyValue(entry: AllowedEntry, value: RoleValue, preset: RolePreset | undefined): boolean {
  if (entry.preset !== value.preset) return false;
  const one = (list: string[], own: string, offered: number) => (list.length ? list.every((item) => item === own) : offered === 0);
  // A value with no account named is not held to one: the router already spreads it over every account,
  // which is all "any" adds.
  return one(entry.efforts, value.effort, preset?.efforts.length ?? 0) && (one(entry.accounts, value.account, preset?.accounts.length ?? 0) || (!value.account && !entry.accounts.length));
}

/** The value changed (its preset, effort or account): the old value's entry leaves the set when it was
 *  there only as the value, so the old effort is not left allowed behind the operator's back, and the new
 *  value joins, keeping what the operator had already set for its preset. */
export function revalue(form: RoleForm, next: RoleValue, role: ShareRole): RoleForm {
  const old = role.presets.find((item) => item.id === form.preset);
  const allowed = form.allowed.filter((entry) => !(entry.preset === form.preset && onlyValue(entry, form, old)));
  return withOwn({ ...form, ...next, allowed });
}

/** The set a role's policy keeps: none when no one chooses in it or it is "only this", and without the
 *  value's own entry when that entry adds nothing to the value. */
export function keptSet(role: ShareRole, form: RoleForm | undefined): AllowedEntry[] {
  if (!form || role.chooser === "none" || form.mode !== "set") return [];
  const preset = role.presets.find((item) => item.id === form.preset);
  return withOwn(form).allowed
    .filter((entry) => entry.preset && !onlyValue(entry, form, preset))
    .map((entry) => ({ preset: entry.preset, accounts: [...entry.accounts], efforts: [...entry.efforts] }));
}

/** A role's form from a value and a stored set: "only this" when the set is empty, which is what every
 *  link made before sets could hold the value's own preset still carries. */
function roleForm(value: RoleValue, allowed: AllowedEntry[]): RoleForm {
  return withOwn({ ...value, mode: allowed.length ? "set" : "locked", allowed: allowed.map((entry) => ({ preset: entry.preset, accounts: [...(entry.accounts ?? [])], efforts: [...(entry.efforts ?? [])] })) });
}

/** A form for a new link. A role the visitor would choose in is locked to what the session uses now, the
 *  safest start; a role the agent chooses in opens with the session's present list, the one its helpers
 *  are started from today, so a new link does not quietly take models away from the agent. */
export function freshForm(options: LiveOptions): LiveForm {
  const roles: Record<string, RoleForm> = {};
  for (const role of options.roles) roles[role.id] = roleForm(valueOf(role), role.chooser === "agent" ? role.current.allowed ?? [] : []);
  const terminals: Terminals = options.terminals.includes("none") ? "none" : options.terminals[0] ?? "none";
  return { roles, budgetUsd: "", budgetPct: "", terminals };
}

/** The form an existing link's policy opens as. A role the policy leaves out is shown at the session's
 *  current value and locked, as a new link's would be: saving then writes it down. */
export function formOf(policy: LivePolicy | undefined, options: LiveOptions): LiveForm {
  const form = freshForm(options);
  for (const [id, kept] of Object.entries(policy?.roles ?? {})) {
    if (!form.roles[id]) continue;
    form.roles[id] = roleForm({ preset: kept.preset, effort: kept.effort ?? "", account: kept.account ?? "" }, kept.allowed ?? []);
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

/** The host's policy from the form. A role no one chooses in never carries a set, whatever the form held
 *  before the options said so. */
export function buildPolicy(form: LiveForm, options: LiveOptions): LivePolicy {
  const roles: Record<string, RolePolicy> = {};
  for (const role of options.roles) {
    const kept = form.roles[role.id];
    if (!kept) continue;
    roles[role.id] = { preset: kept.preset, effort: kept.effort, account: kept.account, allowed: keptSet(role, kept) };
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
    const ids = [kept.preset, ...keptSet(role, kept).map((entry) => entry.preset)];
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
  for (const role of options.roles) roles[role.id] = { ...valueOf(role), mode: "locked", allowed: [] };
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
  const presetsOf = (role: ShareRole) => new Set([form.roles[role.id].preset, ...keptSet(role, form.roles[role.id]).map((entry) => entry.preset)]);
  if (main && form.roles[main.id]) {
    const kept = form.roles[main.id];
    const label = main.presets.find((item) => item.id === kept.preset)?.label ?? kept.preset;
    const more = presetsOf(main).size - 1;
    // A set of the value's own preset alone is not "only": the visitor may still change its effort or account.
    parts.push(more > 0 ? t("live.sum.main.set", { model: label, n: String(more) })
      : keptSet(main, kept).length ? t("live.sum.main.tuned", { model: label })
      : t("live.sum.main.locked", { model: label }));
  }
  const others = options.roles.filter((role) => role.id !== main?.id && role.chooser === "visitor" && form.roles[role.id] && keptSet(role, form.roles[role.id]).length > 0).length;
  parts.push(others ? plural("live.sum.others.open", others) : t("live.sum.others.locked"));
  for (const role of options.roles) {
    if (role.chooser !== "agent" || !form.roles[role.id] || !keptSet(role, form.roles[role.id]).length) continue;
    const known = t(`live.role.${role.id}`);
    parts.push(plural("live.sum.agent", presetsOf(role).size, { role: known.startsWith("[") ? role.label : known }));
  }
  const usd = amount(form.budgetUsd);
  const pct = amount(form.budgetPct);
  const limits = [usd && !Number.isNaN(usd) ? formatUsd(usd) : "", pct && !Number.isNaN(pct) ? t("live.sum.pct", { n: formatPct(pct) }) : ""].filter(Boolean);
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
