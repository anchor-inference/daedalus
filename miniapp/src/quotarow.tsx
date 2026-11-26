// One subscription window as one line: its name in words, a meter, how much of it is used and when
// it starts again. The Usage tool, the provider's page in Details and the panel's Overview all draw a
// window with this, so the three can never disagree about a name, a colour or a reset. The Overview
// once had its own copy, a 36 px meter behind a name cut to a few letters ("Gemi… ▬ 6%"), and the
// Usage tool a three-line block per window; neither said when a window resets without a hover.

import { absDate, absTime } from "./format";
import { Icon } from "./icons";
import { plural, t } from "./i18n";
import { quotaTone } from "./detailsmodel";
import "./quotarow.css";

/** A subscription window as the host reports it: its name ("5h", "weekly", a model's own), how much
 *  of it is used, and when it starts again (seconds since the epoch or an ISO date). */
export type QuotaWindow = { name: string; used_percent: number; resets_at?: number | string | null };

/** The window's name in words. The host names the time windows by their length ("5h", "7d", "90m")
 *  or their period ("weekly"), which read as code beside a full model name; anything else (a model's
 *  own window, "Gemini 4 Argon (high)") is already a name and is kept whole. */
export function windowLabel(name: string): string {
  const length = /^(\d+)\s*([mhd])$/i.exec(name.trim());
  if (length) {
    const n = Number(length[1]);
    const unit = length[2].toLowerCase();
    return plural(unit === "m" ? "fmt.minutes" : unit === "h" ? "fmt.hours" : "fmt.days", n);
  }
  const period = name.trim().toLowerCase();
  if (period === "daily" || period === "weekly" || period === "monthly") return t(`quota.window.${period}`);
  return name;
}

/** When a window starts again: the host sends seconds since the epoch or an ISO date. */
export function resetDate(at: number | string | null | undefined): Date | null {
  if (at === null || at === undefined || at === "") return null;
  const date = new Date(typeof at === "number" && at < 1e12 ? at * 1000 : at);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** The reset in words for the line: "in 2h 14m" up to two days ahead, the date after that (a weekly
 *  window's "in 4d" says less than "30 Nov"); `title` is the full sentence with the time. Null when the
 *  host gave no reset. */
export function resetWords(at: number | string | null | undefined, now = Date.now()): { text: string; title: string } | null {
  const date = resetDate(at);
  if (!date) return null;
  const minutes = Math.round((date.getTime() - now) / 60000);
  const text = minutes <= 0 ? t("fmt.now")
    : minutes < 48 * 60 ? t("fmt.in", { t: [minutes >= 60 ? t("fmt.dur.h", { n: Math.floor(minutes / 60) }) : "", minutes % 60 ? t("fmt.dur.m", { n: minutes % 60 }) : ""].filter(Boolean).join(" ") })
    : absDate(date);
  return { text, title: t("quota.resets.title", { when: text, at: absTime(date) }) };
}

/** The windows, one line each, in the order the host gives them. */
export function QuotaRows({ windows, now }: { windows: QuotaWindow[]; now?: number }) {
  return (
    <div className="qrows" role="list">
      {windows.map((w) => <QuotaRow key={w.name} window={w} now={now} />)}
    </div>
  );
}

/** One window: the name, then the meter, the share and the reset at the line's end, in the attention
 *  colour from 80 % and the danger colour at the limit, as the app judges a quota. A name too long to
 *  share its line puts the figures on a line under it rather than being cut or folded into a column. */
export function QuotaRow({ window: w, now }: { window: QuotaWindow; now?: number }) {
  const percent = Math.round(w.used_percent);
  const tone = quotaTone(w.used_percent);
  const label = windowLabel(w.name);
  const reset = resetWords(w.resets_at, now);
  return (
    <div className={`qrow ${tone === "ok" ? "" : tone}`} role="listitem" data-quota={w.name} data-tone={tone} title={[`${label}: ${percent}%`, reset?.title].filter(Boolean).join(" · ")}>
      <span className="qrow-name">{label}</span>
      <span className="qrow-figures">
        <span className="qrow-meter" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.max(0, Math.min(100, percent))}>
          <i style={{ width: `${Math.max(0, Math.min(100, w.used_percent))}%` }} />
        </span>
        <span className="qrow-pct num">{percent}%</span>
        <span className="qrow-reset">{reset && <><Icon name="reload" size={12} />{reset.text}</>}</span>
      </span>
    </div>
  );
}
