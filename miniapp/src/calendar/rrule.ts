// Repeat rules. The editor offers the presets people reach for — every day, every weekday, weekly on
// this day, monthly on this date, yearly — and a custom rule for the rest; underneath, each is an
// RFC 5545 RRULE, which is what the host expands and what Google, Outlook and CalDAV all speak. The
// value is stored without the "RRULE:" prefix; one that arrives with it is read all the same.

import { weekday, type Day } from "./dates";

export type Preset = "none" | "daily" | "weekdays" | "weekly" | "monthly" | "yearly" | "custom";
export type Freq = "DAILY" | "WEEKLY" | "MONTHLY" | "YEARLY";
export const BYDAY = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"] as const;
export type ByDay = (typeof BYDAY)[number];

/** A rule the custom form can show and edit. `until` is a day, inclusive. */
export type Rule = { freq: Freq; interval: number; byday: ByDay[]; until: Day | null; count: number | null };

const WORKDAYS: ByDay[] = ["MO", "TU", "WE", "TH", "FR"];

export function parseRule(text: string): Rule | null {
  const body = text.trim().replace(/^RRULE:/i, "");
  if (!body) return null;
  const fields = new Map(body.split(";").map((part) => part.split("=") as [string, string]).map(([k, v]) => [k.toUpperCase(), v ?? ""]));
  const freq = fields.get("FREQ") as Freq | undefined;
  if (!freq || !["DAILY", "WEEKLY", "MONTHLY", "YEARLY"].includes(freq)) return null;
  const until = fields.get("UNTIL");
  return {
    freq,
    interval: Math.max(1, Number(fields.get("INTERVAL") ?? 1) || 1),
    byday: (fields.get("BYDAY") ?? "").split(",").filter((d): d is ByDay => (BYDAY as readonly string[]).includes(d)),
    until: until ? `${until.slice(0, 4)}-${until.slice(4, 6)}-${until.slice(6, 8)}` : null,
    count: fields.has("COUNT") ? Math.max(1, Number(fields.get("COUNT")) || 1) : null,
  };
}

/** The rule as RRULE text. A weekly rule with no day falls on the start's own day, as RFC 5545 says. */
export function buildRule(rule: Rule): string {
  const parts = [`FREQ=${rule.freq}`];
  if (rule.interval > 1) parts.push(`INTERVAL=${rule.interval}`);
  if (rule.freq === "WEEKLY" && rule.byday.length) parts.push(`BYDAY=${[...rule.byday].sort((a, b) => order(a) - order(b)).join(",")}`);
  if (rule.until) parts.push(`UNTIL=${rule.until.replace(/-/g, "")}T235959Z`);
  else if (rule.count) parts.push(`COUNT=${rule.count}`);
  return parts.join(";");
}

/** Monday first: the order a reader lists weekdays in, whatever day the week starts on. */
function order(day: ByDay): number {
  return (BYDAY.indexOf(day) + 6) % 7;
}

export function dayCode(day: Day): ByDay {
  return BYDAY[weekday(day)];
}

/** The RRULE for a preset, anchored on the event's first day. */
export function presetRule(preset: Preset, start: Day): string {
  switch (preset) {
    case "daily": return "FREQ=DAILY";
    case "weekdays": return "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR";
    case "weekly": return `FREQ=WEEKLY;BYDAY=${dayCode(start)}`;
    case "monthly": return `FREQ=MONTHLY;BYMONTHDAY=${Number(start.slice(8, 10))}`;
    case "yearly": return "FREQ=YEARLY";
    default: return "";
  }
}

/** Which preset a stored rule is, so the editor shows "Weekly on Tuesday" rather than "Custom" for a
 *  rule it wrote itself. Anything with an interval, an end or other days is custom. */
export function presetOf(text: string, start: Day): Preset {
  if (!text.trim()) return "none";
  const rule = parseRule(text);
  if (!rule) return "custom";
  const body = text.trim().replace(/^RRULE:/i, "").toUpperCase();
  for (const preset of ["daily", "weekdays", "weekly", "monthly", "yearly"] as Preset[]) {
    if (presetRule(preset, start) === body) return preset;
  }
  if (rule.interval === 1 && !rule.until && !rule.count) {
    if (rule.freq === "WEEKLY" && rule.byday.length === 1 && rule.byday[0] === dayCode(start)) return "weekly";
    if (rule.freq === "WEEKLY" && rule.byday.length === 5 && WORKDAYS.every((d) => rule.byday.includes(d))) return "weekdays";
    if (rule.freq === "MONTHLY" && !body.includes("BYDAY")) return "monthly";
    if (rule.freq === "YEARLY" && body === "FREQ=YEARLY") return "yearly";
  }
  return "custom";
}

/** The custom form's starting point for a rule (or for a preset being turned into a custom one). */
export function ruleOrDefault(text: string, start: Day): Rule {
  return parseRule(text) ?? { freq: "WEEKLY", interval: 1, byday: [dayCode(start)], until: null, count: null };
}

export type RuleWords = {
  every: (n: number, unit: Freq) => string;
  on: (days: string) => string;
  until: (day: string) => string;
  times: (n: number) => string;
  dayName: (code: ByDay) => string;
};

/** A rule in the reader's words: "Every 2 weeks on Mon, Wed · until 31 Dec". */
export function describeRule(text: string, words: RuleWords, formatDay: (day: Day) => string): string {
  const rule = parseRule(text);
  if (!rule) return "";
  const parts = [words.every(rule.interval, rule.freq)];
  if (rule.freq === "WEEKLY" && rule.byday.length) parts[0] += ` ${words.on([...rule.byday].sort((a, b) => order(a) - order(b)).map(words.dayName).join(", "))}`;
  if (rule.until) parts.push(words.until(formatDay(rule.until)));
  else if (rule.count) parts.push(words.times(rule.count));
  return parts.join(" · ");
}
