// The step editor's rules, in plain functions: which recorded steps it offers, what each starts as,
// the parameters the operator's words name, and the request that saves them as a skill. The host checks
// all of it again; these keep the editor from offering what the host would refuse.

import type { BrowserSkillStep, BrowserStep } from "../api";

export type SignIn = "operator" | "saved" | "secrets";

/** One recorded step in the editor: kept or deleted, and what the operator changed in it. */
export type SkillRow = { n: number; keep: boolean; text: string; option: string; to: string; signin: SignIn; secrets: string[] };

const PARAM = /\{([a-z][a-z0-9_]{0,39})\}/g;
const SECRET_REF = /^«secret:[a-z][a-z0-9_]{0,47}»$/;

/** The steps the editor lists: a switch of tabs is left out, since a tab's id means nothing on the next run. */
export function editableSteps(steps: BrowserStep[]): BrowserStep[] {
  return steps.filter((s) => s.action !== "tab");
}

/** What a step starts as: kept (a scroll too: a page that loads more as it scrolls needs it), with the
 *  value typed where the recording kept one and the recording's blank where it did not. */
export function initialRow(step: BrowserStep): SkillRow {
  const blank = `{${step.slot || "text"}}`;
  return {
    n: step.n,
    keep: true,
    text: step.action === "type" ? (step.value ?? blank) : "",
    option: step.action === "select" ? (step.option || `{${step.slot || "option"}}`) : "",
    to: step.to ?? "",
    signin: "operator",
    secrets: (step.fields ?? []).map(() => ""),
  };
}

/** The parameters the kept steps name, in the order they first appear. */
export function paramsOf(rows: SkillRow[], steps: BrowserStep[]): string[] {
  const byN = new Map(steps.map((s) => [s.n, s]));
  const seen: string[] = [];
  for (const row of rows) {
    const step = byN.get(row.n);
    if (!row.keep || !step) continue;
    const words = step.action === "type" ? row.text : step.action === "select" ? row.option : step.action === "navigate" || step.action === "arrive" ? row.to : "";
    for (const m of words.matchAll(PARAM)) if (!seen.includes(m[1])) seen.push(m[1]);
  }
  return seen;
}

/** A skill's directory name from what the operator called it. */
export function skillSlug(name: string): string {
  return name.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
}

export function isSecretRef(text: string): boolean {
  return SECRET_REF.test(text.trim());
}

/** Why the editor's Save is not offered yet, as an i18n key; "" when it is. */
export function blocker(rows: SkillRow[], steps: BrowserStep[], name: string, description: string): string {
  if (!skillSlug(name)) return "browser.skill.need.name";
  const words = description.trim().split(/\s+/).join(" ");
  if (words.length < 20) return "browser.skill.need.description";
  const byN = new Map(steps.map((s) => [s.n, s]));
  const kept = rows.filter((r) => r.keep && byN.has(r.n));
  if (!kept.some((r) => ["click", "double_click", "right_click", "check", "uncheck", "type", "select", "press", "handoff"].includes(byN.get(r.n)!.action))) return "browser.skill.need.step";
  for (const row of kept) {
    const step = byN.get(row.n)!;
    if (step.action === "type" && !row.text.trim()) return "browser.skill.need.text";
    if (step.action === "handoff" && row.signin === "secrets" && (!row.secrets.length || !row.secrets.every(isSecretRef))) return "browser.skill.need.secret";
  }
  return "";
}

/** The request that saves the kept steps: each by its number, with only what the editor lets change. */
export function skillRequest(rows: SkillRow[], steps: BrowserStep[], name: string, description: string): { name: string; description: string; steps: BrowserSkillStep[] } {
  const byN = new Map(steps.map((s) => [s.n, s]));
  const out: BrowserSkillStep[] = [];
  for (const row of rows) {
    const step = byN.get(row.n);
    if (!row.keep || !step) continue;
    const edit: BrowserSkillStep = { n: row.n };
    if (step.action === "type") edit.text = row.text;
    if (step.action === "select") edit.option = row.option;
    if ((step.action === "navigate" || step.action === "arrive") && step.to && row.to && row.to !== step.to) edit.to = row.to;
    if (step.action === "handoff") {
      edit.signin = row.signin;
      if (row.signin === "secrets") edit.secrets = row.secrets.map((s) => s.trim());
    }
    out.push(edit);
  }
  return { name: skillSlug(name), description: description.trim().split(/\s+/).join(" "), steps: out };
}
