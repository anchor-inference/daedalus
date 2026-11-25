// The controls the editors share: a time in quarter hours, a calendar with its colour, a colour, a
// searchable time zone and a list of reminders.

import { useMemo, useState, type CSSProperties } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { Combobox, Menu, Popover, Select } from "../ui/primitives";
import { clock, clockLabel, parseClock } from "./dates";
import { PALETTE } from "./data";
import type { CalendarRow } from "./types";
import { calendarName, duration, REMINDER_PRESETS, reminderLabel } from "./words";
import { allZones, offsetLabel } from "./zone";

/** A time of day in 15-minute steps. A time off the grid (09:10 from a synced calendar) is kept as an
 *  extra choice, so opening an event never rewrites its time. With `after`, each choice says how long
 *  the event would last, the way an end time is picked. */
export function TimeSelect({ value, onChange, label, after, id }: { value: number; onChange: (minutes: number) => void; label: string; after?: number; id?: string }) {
  const choices = useMemo(() => {
    const list = Array.from({ length: 96 }, (_, i) => i * 15);
    if (!list.includes(value) && value < 1440) list.push(value);
    if (after !== undefined) list.push(1440);
    return list.sort((a, b) => a - b).filter((m) => after === undefined || m > after);
  }, [value, after]);
  return (
    <Select id={id} size="md" className="cal-time" value={String(value)} label={label} onChange={(m) => onChange(Number(m))}
      options={choices.map((m) => ({ id: String(m), label: `${m === 1440 ? clockLabel(0, locale()) : clockLabel(m, locale())}${after !== undefined ? ` (${duration(m - after)})` : ""}` }))} />
  );
}

/** A clock string ("09:30") through the same select. */
export function ClockSelect({ value, onChange, label }: { value: string; onChange: (text: string) => void; label: string }) {
  return <TimeSelect value={parseClock(value) ?? 540} onChange={(m) => onChange(clock(m))} label={label} />;
}

/** The calendars an event can go in, as choices with their colours: few enough to show at once, and
 *  a colour beside a name is quicker to recognise than a name in a closed list. */
export function CalendarPicker({ calendars, value, onChange }: { calendars: CalendarRow[]; value: string; onChange: (id: string) => void }) {
  const writable = calendars.filter((c) => c.writable || c.id === value);
  return (
    <div className="cal-pick" role="radiogroup" aria-label={t("cal.field.calendar")}>
      {writable.map((c) => (
        <button key={c.id} type="button" role="radio" aria-checked={c.id === value} className={`cal-pick-item ${c.id === value ? "on" : ""}`} style={{ "--c": c.color } as CSSProperties} onClick={() => onChange(c.id)}>
          <span className="cal-dot" aria-hidden="true" />
          <span>{calendarName(c)}</span>
        </button>
      ))}
    </div>
  );
}

/** A colour from the palette; with `none`, a first choice that means "the calendar's own". */
export function ColorPicker({ value, onChange, none, label }: { value: string | null; onChange: (hex: string | null) => void; none?: string; label: string }) {
  return (
    <div className="cal-swatches" role="radiogroup" aria-label={label}>
      {none !== undefined && (
        <button type="button" role="radio" aria-checked={value === null} className={`cal-swatch default ${value === null ? "on" : ""}`} style={{ "--c": none } as CSSProperties} onClick={() => onChange(null)} aria-label={t("cal.color.default")} title={t("cal.color.default")}>
          {value === null && <Icon name="check" size={12} />}
        </button>
      )}
      {PALETTE.map((p) => (
        <button key={p.id} type="button" role="radio" aria-checked={value?.toLowerCase() === p.hex} className={`cal-swatch ${value?.toLowerCase() === p.hex ? "on" : ""}`} style={{ "--c": p.hex } as CSSProperties} onClick={() => onChange(p.hex)} aria-label={t(`cal.color.${p.id}`)} title={t(`cal.color.${p.id}`)}>
          {value?.toLowerCase() === p.hex && <Icon name="check" size={12} />}
        </button>
      ))}
    </div>
  );
}

/** A time zone, searched by any part of its name ("york", "Moscow", "+03"). Several hundred zones do
 *  not belong in a closed list, and nobody remembers whether theirs is under Europe or Asia. */
export function ZonePicker({ value, onChange }: { value: string; onChange: (zone: string) => void }) {
  const [anchor, setAnchor] = useState<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const zones = useMemo(() => allZones(), []);
  const now = Date.now();
  const found = useMemo(() => {
    const words = query.trim().toLowerCase().replace(/\s+/g, "_");
    const list = words ? zones.filter((z) => z.toLowerCase().includes(words) || offsetLabel(z, now).replace("−", "-").includes(words.replace("−", "-"))) : zones;
    return list.slice(0, 60);
  }, [query, zones, now]);
  const close = () => { setOpen(false); setQuery(""); };
  return (
    <div className="cal-zone">
      <button ref={setAnchor} type="button" className="btn cal-zone-btn" onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-haspopup="dialog" aria-label={`${t("cal.field.zone")}: ${value}`}>
        <Icon name="globe" size={16} />
        <span>{value.replace(/_/g, " ")}</span>
        <small>{offsetLabel(value, now)}</small>
      </button>
      {open && (
        <Popover anchor={anchor} onClose={close} label={t("cal.field.zone")} width={340} side="bottom" role="dialog" className="cal-zone-pop">
          <Combobox inline autoFocus items={found} query={query} onQuery={setQuery} label={t("cal.zone.search")} placeholder={t("cal.zone.search")} toLabel={(z) => z}
            empty={t("cal.zone.none")} itemClassName="cal-zone-item"
            render={(z) => <><span className="cal-zone-name">{z.replace(/_/g, " ")}</span><small>{offsetLabel(z, now)}</small></>}
            onChoose={(z) => { onChange(z); close(); }} />
        </Popover>
      )}
    </div>
  );
}

/** Reminders: each one a chip that can be taken away, and a list to add another. */
export function Reminders({ value, onChange }: { value: number[]; onChange: (next: number[]) => void }) {
  const left = REMINDER_PRESETS.filter((m) => !value.includes(m));
  return (
    <div className="cal-reminders">
      {[...value].sort((a, b) => a - b).map((m) => (
        <span key={m} className="cal-reminder">
          <Icon name="bell" size={13} />
          {reminderLabel(m)}
          <button type="button" className="cal-reminder-x" onClick={() => onChange(value.filter((v) => v !== m))} aria-label={t("cal.reminder.remove", { what: reminderLabel(m) })} title={t("common.delete")}>
            <Icon name="close" size={12} />
          </button>
        </span>
      ))}
      {left.length > 0 && (
        <Menu label={t("cal.reminder.add")} trigger={<button type="button" className="btn small cal-reminder-add"><Icon name="plus" size={14} />{t("cal.reminder.add")}</button>}
          items={left.map((m) => ({ label: reminderLabel(m), onSelect: () => onChange([...value, m]) }))} />
      )}
    </div>
  );
}
