// What the views show: by calendar, by type, by group, by tag and by status, with the done ones hidden
// on request, and how tightly the list is drawn. Remembered on this device; the calendars' own
// switches in the side column stay what they were (shown or hidden for every device), and these
// narrow further.

import type { CSSProperties } from "react";
import { t } from "../i18n";
import { Icon } from "../icons";
import { Sheet } from "../ui/dialogs";
import { STATUSES, type EventStatus, type Group, type TypeDef } from "./blocks";
import { TypeIcon, statusLabel } from "./structured";
import type { CalendarRow, Item } from "./types";
import { calendarName } from "./words";

export type Filters = { calendars: string[]; types: string[]; groups: string[]; tags: string[]; statuses: EventStatus[]; hideDone: boolean };
export const NO_FILTERS: Filters = { calendars: [], types: [], groups: [], tags: [], statuses: [], hideDone: false };
const STORE = "daedalus.calendar.filters";

export function storedFilters(): Filters {
  try {
    const read = JSON.parse(localStorage.getItem(STORE) || "null") as Partial<Filters> | null;
    return read ? { ...NO_FILTERS, ...read } : NO_FILTERS;
  } catch { return NO_FILTERS; }
}

export function storeFilters(filters: Filters): void {
  try { localStorage.setItem(STORE, JSON.stringify(filters)); } catch { /* the filter lasts the visit */ }
}

/** How many kinds of filter are on: the badge on the filter button. */
export function filterCount(f: Filters): number {
  return [f.calendars.length, f.types.length, f.groups.length, f.tags.length, f.statuses.length, f.hideDone ? 1 : 0].filter(Boolean).length;
}

/** Whether an item passes. An empty list means "any"; a task passes the event-only filters, since a
 *  task has no type, group or tag to fail them by, and is hidden with the done ones when it is done. */
export function passes(item: Item, f: Filters): boolean {
  const event = item.event;
  if (f.hideDone && (item.done || event?.status === "done" || event?.status === "skipped" || event?.status === "cancelled")) return false;
  if (!event) return true;
  if (f.calendars.length && !f.calendars.includes(event.calendar_id)) return false;
  if (f.types.length && !f.types.includes(event.type ?? "")) return false;
  if (f.groups.length && !f.groups.includes(event.group_id ?? "")) return false;
  if (f.tags.length && !(event.tags ?? []).some((tag) => f.tags.includes(tag))) return false;
  if (f.statuses.length && !f.statuses.includes(event.status ?? "planned")) return false;
  return true;
}

type Props = {
  filters: Filters;
  calendars: CalendarRow[];
  types: TypeDef[];
  groups: Group[];
  tags: string[];
  dense: boolean;
  onDense: (dense: boolean) => void;
  onChange: (filters: Filters) => void;
  onClose: () => void;
};

export function FilterSheet({ filters, calendars, types, groups, tags, dense, onDense, onChange, onClose }: Props) {
  const flip = <K extends "calendars" | "types" | "groups" | "tags" | "statuses">(key: K, value: Filters[K][number]) => {
    const list = filters[key] as string[];
    onChange({ ...filters, [key]: list.includes(value as string) ? list.filter((x) => x !== value) : [...list, value] });
  };
  const chip = (on: boolean, label: string, onClick: () => void, lead?: React.ReactNode, color?: string) => (
    <button key={label} type="button" className={`cal-fchip ${on ? "on" : ""}`} aria-pressed={on} onClick={onClick} style={color ? ({ "--c": color } as CSSProperties) : undefined}>
      {lead}{label}
    </button>
  );
  return (
    <Sheet title={t("cal.filters")} onClose={onClose} size="narrow" className="cal-filters">
      <div className="cal-filter-body">
        <label className="cal-switch-row cal-filter-done">
          <input type="checkbox" checked={filters.hideDone} onChange={(e) => onChange({ ...filters, hideDone: e.target.checked })} />
          <span>{t("cal.filters.hide.done")}</span>
        </label>
        <div className="cal-filter-group">
          <div className="cal-section-label">{t("cal.filters.density")}</div>
          <div className="segmented inline" role="radiogroup" aria-label={t("cal.filters.density")}>
            <button type="button" role="radio" aria-checked={!dense} className={!dense ? "on" : ""} onClick={() => onDense(false)}>{t("cal.density.normal")}</button>
            <button type="button" role="radio" aria-checked={dense} className={dense ? "on" : ""} onClick={() => onDense(true)}>{t("cal.density.compact")}</button>
          </div>
        </div>
        {types.length > 0 && (
          <div className="cal-filter-group">
            <div className="cal-section-label">{t("cal.filters.types")}</div>
            <div className="cal-fchips">{types.map((type) => chip(filters.types.includes(type.id), type.name, () => flip("types", type.id), <TypeIcon icon={type.icon} color={type.color} size={13} />, type.color))}</div>
          </div>
        )}
        {groups.filter((g) => !g.archived).length > 0 && (
          <div className="cal-filter-group">
            <div className="cal-section-label">{t("cal.filters.groups")}</div>
            <div className="cal-fchips">{groups.filter((g) => !g.archived).map((g) => chip(filters.groups.includes(g.id), g.name, () => flip("groups", g.id), <TypeIcon icon={g.icon} color={g.color} size={13} />, g.color))}</div>
          </div>
        )}
        <div className="cal-filter-group">
          <div className="cal-section-label">{t("cal.filters.status")}</div>
          <div className="cal-fchips">{STATUSES.map((s) => chip(filters.statuses.includes(s), statusLabel(s), () => flip("statuses", s)))}</div>
        </div>
        {tags.length > 0 && (
          <div className="cal-filter-group">
            <div className="cal-section-label">{t("cal.filters.tags")}</div>
            <div className="cal-fchips">{tags.map((tag) => chip(filters.tags.includes(tag), `#${tag}`, () => flip("tags", tag)))}</div>
          </div>
        )}
        <div className="cal-filter-group">
          <div className="cal-section-label">{t("cal.filters.calendars")}</div>
          <div className="cal-fchips">{calendars.map((c) => chip(filters.calendars.includes(c.id), calendarName(c), () => flip("calendars", c.id), <span className="cal-dot" style={{ "--c": c.color } as CSSProperties} aria-hidden="true" />, c.color))}</div>
        </div>
      </div>
      <div className="sheet-foot cal-foot">
        <button type="button" className="btn ghost" disabled={filterCount(filters) === 0} onClick={() => onChange({ ...filters, calendars: [], types: [], groups: [], tags: [], statuses: [], hideDone: false })}><Icon name="close" size={14} /> {t("cal.filters.clear")}</button>
        <span className="cal-spacer" />
        <button type="button" className="btn primary" onClick={onClose}>{t("cal.filters.apply")}</button>
      </div>
    </Sheet>
  );
}
