// The pieces the Details tab is drawn from. The tab used to be one style repeated: a 12 px label and
// rows of grey "key value" text, so a raw byte count looked as important as the context's fill and
// nothing could be found by eye. Each section is now a card with an icon and a title, opens on its
// one headline number, puts the rest in quiet facts or a "More" fold, and draws a quantity as a bar
// with its label and percent. These parts hold that grammar so every section speaks it the same way.

import { useState } from "react";
import { readLayout, writeLayout } from "./layout";
import { absTime, middleTruncate, relTimeLong, untilShort } from "./format";
import { Icon, type IconName } from "./icons";
import { copyText } from "./ui/components";
import { t } from "./i18n";
import type { Tone } from "./detailsmodel";

/** `closed`: a section that starts shut until the operator opens it once (then the choice is kept);
 *  for one whose content costs a request, which only an opened section should make. */
export function Section({ ids, id, label, icon, children, className, aside, closed }: { ids: string; id: string; label: string; icon?: IconName; children: React.ReactNode; className?: string; aside?: React.ReactNode; closed?: boolean }) {
  const [open, setOpen] = useState(() => (readLayout(`details.${id}`) ?? (closed ? "closed" : "open")) !== "closed");
  return (
    <details className={`dt-section ${className ?? ""}`} id={`${ids}-info-${id}`} open={open} onToggle={(e) => { const next = e.currentTarget.open; setOpen(next); writeLayout(`details.${id}`, next ? "open" : "closed"); }}>
      <summary className="dt-label" onClick={(e) => {
        // Persist at the click, before a navigation can discard the native toggle event's task.
        e.preventDefault();
        setOpen(!open);
        writeLayout(`details.${id}`, open ? "closed" : "open");
      }}>
        {icon && <span className="dt-icon" aria-hidden><Icon name={icon} size={14} /></span>}
        <span className="dt-title">{label}</span>
        {aside && <span className="dt-aside">{aside}</span>}
        <span className="dt-chevron" aria-hidden><Icon name="chevron" size={14} /></span>
      </summary>
      <div className="dt-content">{children}</div>
    </details>
  );
}

/** The section's one number worth reading first, large, with what it is measured against beside it. */
export function Headline({ value, tone, caption, sub, side, title, data }: { value: React.ReactNode; tone?: Tone | null; caption?: React.ReactNode; sub?: React.ReactNode; side?: React.ReactNode; title?: string; data?: Record<string, string> }) {
  return (
    <div className="dt-headline" title={title} {...dataAttributes(data)}>
      <b className={`dt-big ${tone ?? ""}`}>{value}</b>
      {(caption || sub) && <span className="dt-headline-text">
        {caption && <span className="dt-caption">{caption}</span>}
        {sub && <span className="dt-hint">{sub}</span>}
      </span>}
      {side && <span className="dt-headline-side">{side}</span>}
    </div>
  );
}

/** A row of tiles, each a label over a value with an optional line under it. */
export function Stats({ children }: { children: React.ReactNode }) {
  return <div className="dt-stats">{children}</div>;
}

export function Stat({ label, value, sub, title }: { label: string; value: React.ReactNode; sub?: React.ReactNode; title?: string }) {
  return (
    <div className="dt-stat" title={title}>
      <span className="dt-stat-label">{label}</span>
      <b className="dt-stat-value">{value}</b>
      {sub && <span className="dt-stat-sub">{sub}</span>}
    </div>
  );
}

/** A labelled bar: what it measures, its percent, and a quiet line under it (a reset, a limit). */
export function Meter({ label, percent, tone, detail, title, data }: { label: React.ReactNode; percent: number; tone: Tone; detail?: React.ReactNode; title?: string; data?: Record<string, string> }) {
  const width = Math.min(100, Math.max(0, percent));
  return (
    <div className="dt-meter" data-tone={tone} title={title} {...dataAttributes(data)}>
      <div className="dt-meter-line">
        <span className="dt-meter-label">{label}</span>
        <b className="dt-meter-value">{Math.round(percent)}%</b>
      </div>
      <div className="dt-meter-bar" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(width)}><i style={{ width: `${width}%` }} /></div>
      {detail && <div className="dt-hint">{detail}</div>}
    </div>
  );
}

/** Secondary facts as a quiet two-column list: the name in grey on the left, the value on the right. */
export function Facts({ children }: { children: React.ReactNode }) {
  return <dl className="dt-facts">{children}</dl>;
}

export function Fact({ label, children, mono, title }: { label: string; children: React.ReactNode; mono?: boolean; title?: string }) {
  return (
    <div className="dt-fact">
      <dt>{label}</dt>
      <dd className={mono ? "mono" : undefined} title={title}>{children}</dd>
    </div>
  );
}

/** The rest of a section, folded under a "More" line. A fold, not a removal: the facts stay in the page. */
export function More({ id, children }: { id?: string; children: React.ReactNode }) {
  const key = id ? `details.more.${id}` : null;
  const [open, setOpen] = useState(() => (key ? readLayout(key) === "open" : false));
  return (
    <details className="dt-more" open={open} onToggle={(e) => { const next = e.currentTarget.open; setOpen(next); if (key) writeLayout(key, next ? "open" : "closed"); }}>
      <summary><span>{t(open ? "details.less" : "details.more")}</span><Icon name="chevron" size={12} /></summary>
      <div className="dt-more-body">{children}</div>
    </details>
  );
}

/** A sentence that explains rather than reports, set apart from the numbers in a soft box. */
export function Note({ tone, children, title, data }: { tone?: Tone; children: React.ReactNode; title?: string; data?: Record<string, string> }) {
  return (
    <p className={`dt-note ${tone ?? ""}`} title={title} {...dataAttributes(data)}>
      <Icon name={tone === "bad" || tone === "attn" ? "alert" : "bulb"} size={14} />
      <span>{children}</span>
    </p>
  );
}

/** A long id or path, shortened in the middle so both ends stay legible, whole in its title, copied on a click. */
export function CopyValue({ value, shown, toast, copied, max = 22, data }: { value: string; shown?: string; toast: (text: string) => void; copied?: string; max?: number; data?: Record<string, string> }) {
  return (
    <button type="button" className="dt-copy mono" title={value} aria-label={t("details.copy.label", { value })} {...dataAttributes(data)}
      onClick={async () => toast((await copyText(value)) ? (copied ?? t("common.copied")) : value)}>
      <span className="truncate">{shown ?? middleTruncate(value, max)}</span>
      <Icon name="copy" size={12} />
    </button>
  );
}

/** A moment as "3 hours ago", the exact date and time on hover. */
export function When({ at }: { at: string | number | null | undefined }) {
  if (!at) return null;
  // A number is the host's epoch seconds, as the subscription windows send their resets.
  const date = new Date(typeof at === "number" && at < 1e12 ? at * 1000 : at);
  if (Number.isNaN(date.getTime())) return null;
  return <time dateTime={date.toISOString()} title={absTime(date)}>{relTimeLong(date)}</time>;
}

/** A moment ahead as "in 4d", the exact date and time on hover. Rounded, not floored: a reset four
 *  days out read "in 3 days" a second after the page loaded. */
export function Until({ at }: { at: string | number | null | undefined }) {
  if (!at) return null;
  const date = new Date(typeof at === "number" && at < 1e12 ? at * 1000 : at);
  if (Number.isNaN(date.getTime())) return null;
  return <time dateTime={date.toISOString()} title={absTime(date)}>{untilShort(date)}</time>;
}

function dataAttributes(data: Record<string, string> | undefined): Record<string, string> {
  if (!data) return {};
  return Object.fromEntries(Object.entries(data).map(([key, value]) => [`data-${key}`, value]));
}
