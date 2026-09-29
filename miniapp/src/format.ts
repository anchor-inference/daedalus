// How numbers, times and schedules read in the app: the reader's language, locale and time zone,
// never seconds, never an ISO timestamp. Every screen formats through here, so a language changed
// in Settings changes every date and every count on the page with it.

import { locale, num, plural, t } from "./i18n";

/** "now", "35m", "3h", "2d" — the short form for a row's trailing time. */
export function relTime(iso: string | number | Date | null | undefined, now = Date.now()): string {
  const at = toMs(iso);
  if (at === null) return "";
  const delta = Math.round((now - at) / 1000);
  const abs = Math.abs(delta);
  const ahead = (s: string) => (delta < 0 ? t("fmt.in", { t: s }) : s);
  if (abs < 60) return delta < 0 ? t("fmt.soon") : t("fmt.now");
  if (abs < 3600) return ahead(t("fmt.min", { n: Math.floor(abs / 60) }));
  if (abs < 86400) return ahead(t("fmt.hour", { n: Math.floor(abs / 3600) }));
  if (abs < 30 * 86400) return ahead(t("fmt.day", { n: Math.floor(abs / 86400) }));
  return absDate(at);
}

/** "just now", "35 minutes ago", "3 hours ago", "2 days ago", or "in 2 hours" for a future moment. */
export function relTimeLong(iso: string | number | Date | null | undefined, now = Date.now()): string {
  const at = toMs(iso);
  if (at === null) return "";
  const delta = Math.round((now - at) / 1000);
  const abs = Math.abs(delta);
  const wrap = (s: string) => t(delta < 0 ? "fmt.in" : "fmt.ago", { t: s });
  if (abs < 60) return delta < 0 ? t("fmt.moment") : t("fmt.justnow");
  if (abs < 3600) return wrap(plural("fmt.minutes", Math.floor(abs / 60)));
  if (abs < 86400) return wrap(plural("fmt.hours", Math.floor(abs / 3600)));
  if (abs < 30 * 86400) return wrap(plural("fmt.days", Math.floor(abs / 86400)));
  return absDate(at);
}

/** "in 12m", "in 2h", "in 3d" — how long until a reset or a wake-up; "now" when it is past. */
export function untilShort(iso: string | number | Date | null | undefined, now = Date.now()): string {
  const at = toMs(iso);
  if (at === null) return "";
  const mins = Math.round((at - now) / 60000);
  if (mins <= 0) return t("fmt.now");
  if (mins < 60) return t("fmt.in", { t: t("fmt.min", { n: mins }) });
  if (mins < 48 * 60) return t("fmt.in", { t: t("fmt.hour", { n: Math.round(mins / 60) }) });
  return t("fmt.in", { t: t("fmt.day", { n: Math.round(mins / 1440) }) });
}

/** "11 Sep, 15:20" in the reader's locale and zone; the year only when it is not this one. */
export function absTime(iso: string | number | Date | null | undefined): string {
  const at = toMs(iso);
  if (at === null) return "";
  const d = new Date(at);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleString(locale(), { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", ...(sameYear ? {} : { year: "numeric" }) });
}

/** "11 Sep" or "11 Sep 2025". */
export function absDate(iso: string | number | Date | null | undefined): string {
  const at = toMs(iso);
  if (at === null) return "";
  const d = new Date(at);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(locale(), { day: "numeric", month: "short", ...(sameYear ? {} : { year: "numeric" }) });
}

/** "15:20" in the reader's zone. */
export function clock(iso: string | number | Date | null | undefined): string {
  const at = toMs(iso);
  if (at === null) return "";
  return new Date(at).toLocaleTimeString(locale(), { hour: "2-digit", minute: "2-digit" });
}

/** A date and a time together, short: "11/09, 15:20". */
export function shortDateTime(iso: string | number | Date | null | undefined): string {
  const at = toMs(iso);
  if (at === null) return "";
  return new Date(at).toLocaleString(locale(), { dateStyle: "short", timeStyle: "short" });
}

/** "today", "yesterday", or the date. */
export function dayLabel(iso: string | number | Date | null | undefined): string {
  const at = toMs(iso);
  if (at === null) return "";
  const d = new Date(at);
  const today = new Date();
  const yday = new Date(today.getTime() - 86400000);
  const same = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (same(d, today)) return t("fmt.today");
  if (same(d, yday)) return t("fmt.yesterday");
  return absDate(at);
}

/** "4s", "2m 14s", "1h 44m", "2d 3h" — a duration in milliseconds as people read it. */
export function duration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return t("fmt.dur.s", { n: s });
  const m = Math.floor(s / 60);
  if (m < 60) return t("fmt.dur.m", { n: m }) + (s % 60 ? ` ${t("fmt.dur.s", { n: s % 60 })}` : "");
  const h = Math.floor(m / 60);
  if (h < 48) return t("fmt.dur.h", { n: h }) + (m % 60 ? ` ${t("fmt.dur.m", { n: m % 60 })}` : "");
  const d = Math.floor(h / 24);
  return t("fmt.dur.d", { n: d }) + (h % 24 ? ` ${t("fmt.dur.h", { n: h % 24 })}` : "");
}

/** Compact token counts: 71.1M, 28k, 950. */
export function tokens(n: number | null | undefined): string {
  const v = n ?? 0;
  if (v >= 1_000_000) return t("fmt.tok.m", { n: (v / 1_000_000).toFixed(1) });
  if (v >= 10_000) return t("fmt.tok.k", { n: Math.round(v / 1000) });
  return num(v);
}

/** A host's refusal as the start of a sentence. The host words its details in lower case, to be read
 *  after a colon in a log ("the registry did not answer"); shown alone under a heading they read as a
 *  cut-off line. Only a plain word is raised: "npm: not found" or "ffmpeg_x missing" name something
 *  whose spelling matters and stay as they are. */
export function asSentence(text: string): string {
  return /^[a-z]+ /.test(text) ? text[0].toUpperCase() + text.slice(1) : text;
}

/** "$3.90", "< $0.01", "$0", "free" for a missing price. */
export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return t("fmt.free");
  if (value === 0) return "$0";
  if (value < 0.01) return "< $0.01";
  return `$${value.toFixed(2)}`;
}

export function int(value: number | null | undefined): string {
  return num(value ?? 0);
}

/** Bytes as people read them: 950 B, 12 KB, 1.4 MB. */
export function bytes(n: number): string {
  if (n < 1024) return t("fmt.bytes.b", { n });
  if (n < 1024 * 1024) return t("fmt.bytes.kb", { n: Math.round(n / 1024) });
  return t("fmt.bytes.mb", { n: (n / (1024 * 1024)).toFixed(1) });
}

/**
 * A downloadable's size, coarser than `bytes`: a speech model is "170 MB", never "170.3 MB". Both
 * speech pickers show one, and they showed it through a copy of this each, with the unit written
 * into the code where no language could reach it.
 */
export function modelSize(n: number): string {
  if (n >= 1 << 30) return t("fmt.bytes.gb", { n: (n / (1 << 30)).toFixed(1) });
  return t("fmt.bytes.mb", { n: Math.round(n / (1 << 20)) });
}

/** Keeps both ends of a long path or id: "/srv/works…/round8_probe.py". */
export function middleTruncate(s: string, max = 32): string {
  if (s.length <= max) return s;
  const tail = Math.max(8, Math.floor(max * 0.6));
  const head = Math.max(4, max - tail - 1);
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}

/** A subscription plan id as a person says it: default_claude_max_20x → "Max 20×", plus → "Plus". */
export function planName(id: string | null | undefined): string {
  if (!id) return "";
  const cleaned = id.replace(/^default_/, "").replace(/^(claude|chatgpt|codex|grok|opencode)_/, "");
  return cleaned
    .split(/[_-]+/)
    .filter(Boolean)
    .map((w) => (/^\d+x$/i.test(w) ? `${w.slice(0, -1)}×` : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}

/** The model name as the header shows it: "deepseek/deepseek-v4-flash" → "deepseek-v4-flash", cut at `max`. */
export function shortModel(name: string | null | undefined, max = 24): string {
  if (!name) return t("session.model.none");
  const short = name.split("/").pop()!.replace(/\s*\(.*\)$/, "");
  return short.length > max ? `${short.slice(0, max - 1)}…` : short;
}

/** A command as the timeline shows it: without the `cd <workspace> &&` prefix and on one line. */
export function commandPreview(command: string, workspace?: string): string {
  let c = command.trim();
  if (workspace) {
    const root = workspace.replace(/\/+$/, "");
    c = c.replace(new RegExp(`^cd\\s+${escapeRe(root)}/?\\s*(&&|;)\\s*`), "");
  }
  c = c.replace(/^cd\s+\S+\s*(&&|;)\s*/, "");
  return c.split("\n")[0];
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Markdown reduced to plain words for a one-line preview. */
export function plainPreview(md: string, max = 120): string {
  const text = md
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/[*_~]{1,3}([^*_~]+)[*_~]{1,3}/g, "$1")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/^\s*\d+\.\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

// ── schedules ────────────────────────────────────────────────────────────────────────────

/** Sunday first, the way a cron field counts. */
const dow = (d: number) => t(`fmt.dow.${d}`);

export type CronParts = { minute: string; hour: string; dom: string; month: string; dow: string };

export function parseCron(cron: string): CronParts | null {
  const f = cron.trim().split(/\s+/);
  if (f.length !== 5) return null;
  return { minute: f[0], hour: f[1], dom: f[2], month: f[3], dow: f[4] };
}

/** The reader's zone offset in minutes east of UTC at `at`. */
export function zoneOffsetMinutes(at = new Date()): number {
  return -at.getTimezoneOffset();
}

function fmtClock(h: number, m: number): string {
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function dowList(spec: string): number[] | null {
  const out = new Set<number>();
  for (const part of spec.split(",")) {
    const m = /^(\d)(?:-(\d))?$/.exec(part.trim());
    if (!m) return null;
    const a = Number(m[1]) % 7;
    const b = m[2] === undefined ? a : Number(m[2]) % 7;
    if (b >= a) for (let i = a; i <= b; i++) out.add(i);
    else {
      for (let i = a; i <= 6; i++) out.add(i);
      for (let i = 0; i <= b; i++) out.add(i);
    }
  }
  return [...out].sort();
}

/** The days a cron line names: a word where there is one, the list of days otherwise. */
function dowLabel(days: number[]): { text: string; everyDay: boolean } {
  const key = days.join(",");
  if (key === "1,2,3,4,5") return { text: t("fmt.cron.weekdays"), everyDay: false };
  if (key === "0,6") return { text: t("fmt.cron.weekends"), everyDay: false };
  if (key === "0,1,2,3,4,5,6") return { text: "", everyDay: true };
  return { text: days.map(dow).join(", "), everyDay: false };
}

/**
 * A cron line (UTC) as a sentence in the reader's zone: "Every day at 09:00", "Weekdays at 09:00",
 * "Mon, Wed, Fri at 14:30", "Every 2 hours", "Every 15 min". Anything else comes back as the
 * cron line itself with a "(UTC)" mark, so nothing is ever mistranslated.
 */
export function describeCron(cron: string, offsetMinutes = zoneOffsetMinutes()): string {
  const p = parseCron(cron);
  if (!p) return cron;
  const fixedMinute = /^\d+$/.test(p.minute);
  const fixedHour = /^\d+$/.test(p.hour);
  if (p.dom === "*" && p.month === "*") {
    if (/^\*\/(\d+)$/.test(p.minute) && p.hour === "*" && p.dow === "*") return plural("fmt.cron.everyminutes", Number(/^\*\/(\d+)$/.exec(p.minute)![1]));
    if (fixedMinute && /^\*\/(\d+)$/.test(p.hour) && p.dow === "*") {
      const n = Number(/^\*\/(\d+)$/.exec(p.hour)![1]);
      return n === 1 ? t("fmt.cron.everyhour") : plural("fmt.cron.everyhours", n);
    }
    if (fixedMinute && p.hour === "*" && p.dow === "*") return t("fmt.cron.everyhour");
    if (fixedMinute && fixedHour) {
      // Shift the UTC clock into the reader's zone; a shift across midnight moves the days too.
      let total = Number(p.hour) * 60 + Number(p.minute) + offsetMinutes;
      let dayShift = 0;
      while (total < 0) {
        total += 1440;
        dayShift -= 1;
      }
      while (total >= 1440) {
        total -= 1440;
        dayShift += 1;
      }
      const clockText = fmtClock(Math.floor(total / 60), total % 60);
      if (p.dow === "*") return t("fmt.cron.daily", { time: clockText });
      const days = dowList(p.dow);
      if (days) {
        const shifted = days.map((d) => (((d + dayShift) % 7) + 7) % 7).sort();
        const label = dowLabel(shifted);
        return label.everyDay ? t("fmt.cron.daily", { time: clockText }) : t("fmt.cron.on", { days: label.text, time: clockText });
      }
    }
  }
  return t("fmt.cron.utc", { cron });
}

/** One line for a schedule row: the cadence, or the moment of a one-off. */
export function describeSchedule(s: { cron: string | null; run_at: string | null }): string {
  if (s.cron) return describeCron(s.cron);
  if (s.run_at) return t("fmt.once", { when: absTime(s.run_at) });
  return "";
}

/** A local wall-clock time and a set of weekdays as a UTC cron line. */
export function cronFor(kind: "daily" | "weekdays" | "weekly" | "hours", hour: number, minute: number, days: number[] = [], everyHours = 1, offsetMinutes = zoneOffsetMinutes()): string {
  if (kind === "hours") return `${minute} */${Math.max(1, everyHours)} * * *`;
  let total = hour * 60 + minute - offsetMinutes;
  let dayShift = 0;
  while (total < 0) {
    total += 1440;
    dayShift -= 1;
  }
  while (total >= 1440) {
    total -= 1440;
    dayShift += 1;
  }
  const h = Math.floor(total / 60);
  const m = total % 60;
  const shift = (d: number) => (((d + dayShift) % 7) + 7) % 7;
  if (kind === "daily") return `${m} ${h} * * *`;
  const list = (kind === "weekdays" ? [1, 2, 3, 4, 5] : days).map(shift).sort((a, b) => a - b);
  return `${m} ${h} * * ${list.length ? list.join(",") : "*"}`;
}

function toMs(v: string | number | Date | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : v.getTime();
  if (typeof v === "number") return v < 1e12 ? v * 1000 : v;
  const t = new Date(v).getTime();
  return Number.isNaN(t) ? null : t;
}
