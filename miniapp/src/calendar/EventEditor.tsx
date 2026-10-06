// The full event editor: a sheet on a desktop, a sheet from the bottom on a phone. Times are shown on
// the event's own time zone's clock, which is the settings' zone unless the event names another.

import { useState, type CSSProperties } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { Sheet } from "../ui/dialogs";
import { Switch } from "../ui/components";
import { addDays, dayLabel, diffDays, type Day } from "./dates";
import { allDayTimes, type EventFields } from "./data";
import { CalendarPicker, ColorPicker, Reminders, TimeSelect, ZonePicker } from "./fields";
import { BYDAY, buildRule, dayCode, presetOf, presetRule, ruleOrDefault, type ByDay, type Freq, type Preset, type Rule } from "./rrule";
import type { CalendarRow, Occurrence } from "./types";
import { ruleText } from "./words";
import { fromWall, toWall, validZone } from "./zone";

export type EventDraft = {
  occurrence?: Occurrence;
  calendar_id: string;
  title: string;
  description: string;
  location: string;
  allDay: boolean;
  startDay: Day;
  startMin: number;
  /** The last day for an all-day event (inclusive), the end's day otherwise. */
  endDay: Day;
  endMin: number;
  timezone: string;
  recurrence: string;
  reminders: number[];
  color: string | null;
};

export function draftFromOccurrence(o: Occurrence, viewZone: string): EventDraft {
  const zone = o.timezone && validZone(o.timezone) ? o.timezone : viewZone;
  const base = { occurrence: o, calendar_id: o.calendar_id, title: o.title, description: o.description ?? "", location: o.location ?? "", timezone: zone, recurrence: o.recurrence ?? "", reminders: o.reminders ?? [], color: o.color_override ?? null };
  if (o.all_day) {
    const first = o.start_date || o.start_at.slice(0, 10);
    const end = o.end_date || o.end_at.slice(0, 10);
    return { ...base, allDay: true, startDay: first, startMin: 540, endDay: end > first ? addDays(end, -1) : first, endMin: 600 };
  }
  const from = toWall(o.start_at, zone);
  const to = toWall(o.end_at, zone);
  // An end at midnight is the end of the day before, shown as 24:00 there rather than 00:00 the next day.
  const endsAtMidnight = to.minutes === 0 && to.day > from.day;
  return { ...base, allDay: false, startDay: from.day, startMin: from.minutes, endDay: endsAtMidnight ? addDays(to.day, -1) : to.day, endMin: endsAtMidnight ? 1440 : to.minutes };
}

export function fieldsFromDraft(d: EventDraft): EventFields | null {
  const common = { calendar_id: d.calendar_id, title: d.title.trim(), description: d.description, location: d.location.trim(), timezone: d.timezone, recurrence: d.recurrence, reminders: d.reminders, color: d.color };
  if (d.allDay) {
    if (d.endDay < d.startDay) return null;
    return { ...common, all_day: true, ...allDayTimes(d.startDay, d.endDay) };
  }
  const start = fromWall(d.startDay, d.startMin, d.timezone);
  const end = fromWall(d.endDay, d.endMin, d.timezone);
  if (Date.parse(end) <= Date.parse(start)) return null;
  return { ...common, all_day: false, start_at: start, end_at: end, start_date: null, end_date: null };
}

type Props = {
  draft: EventDraft;
  calendars: CalendarRow[];
  phone: boolean;
  busy: boolean;
  onClose: () => void;
  onSave: (draft: EventDraft) => void;
  onDelete: (occurrence: Occurrence) => void;
  onResolve: (occurrence: Occurrence, choice: "local" | "remote") => void;
};

export function EventEditor({ draft: initial, calendars, busy, onClose, onSave, onDelete, onResolve }: Props) {
  const [d, setD] = useState<EventDraft>(initial);
  const [preset, setPreset] = useState<Preset>(presetOf(initial.recurrence, initial.startDay));
  const [rule, setRule] = useState<Rule>(ruleOrDefault(initial.recurrence, initial.startDay));
  const [more, setMore] = useState(!!(initial.description || initial.location || initial.color));
  const [endDate, setEndDate] = useState(false);
  const calendar = calendars.find((c) => c.id === d.calendar_id);
  const readOnly = !!initial.occurrence && (!initial.occurrence.writable || calendar?.writable === false);
  const conflict = initial.occurrence?.conflict;
  const set = (patch: Partial<EventDraft>) => setD((cur) => ({ ...cur, ...patch }));
  const sameDay = d.startDay === d.endDay;
  const invalid = d.allDay ? d.endDay < d.startDay : d.endDay < d.startDay || (sameDay && d.endMin <= d.startMin);

  /** Moving the start keeps the length, as every calendar does: an hour-long meeting moved to 15:00
   *  ends at 16:00 instead of becoming a negative length the reader has to repair. */
  function moveStart(day: Day, minutes: number) {
    const length = diffDays(d.startDay, d.endDay) * 1440 + d.endMin - d.startMin;
    const endTotal = minutes + Math.max(length, 15);
    set({ startDay: day, startMin: minutes, endDay: addDays(day, Math.floor((endTotal - 1) / 1440)), endMin: endTotal - Math.floor((endTotal - 1) / 1440) * 1440 });
    if (preset !== "none" && preset !== "custom") set({ recurrence: presetRule(preset, day) });
  }

  function pickPreset(next: Preset) {
    setPreset(next);
    if (next === "custom") {
      const base = ruleOrDefault(d.recurrence || presetRule("weekly", d.startDay), d.startDay);
      setRule(base);
      set({ recurrence: buildRule(base) });
    } else set({ recurrence: presetRule(next, d.startDay) });
  }

  function changeRule(patch: Partial<Rule>) {
    const next = { ...rule, ...patch };
    setRule(next);
    set({ recurrence: buildRule(next) });
  }

  const dayName = dayLabel(d.startDay, locale(), { weekday: "long" });
  const dateName = dayLabel(d.startDay, locale(), { day: "numeric", month: "long" });
  const presets: { id: Preset; label: string }[] = [
    { id: "none", label: t("cal.repeat.none") },
    { id: "daily", label: t("cal.repeat.daily") },
    { id: "weekdays", label: t("cal.repeat.weekdays") },
    { id: "weekly", label: t("cal.repeat.weekly", { day: dayName }) },
    { id: "monthly", label: t("cal.repeat.monthly", { n: Number(d.startDay.slice(8, 10)) }) },
    { id: "yearly", label: t("cal.repeat.yearly", { date: dateName }) },
    { id: "custom", label: t("cal.repeat.custom") },
  ];

  return (
    <Sheet title={initial.occurrence ? t("cal.edit") : t("cal.new")} onClose={onClose} className="cal-editor">
      <form className="cal-form" onSubmit={(e) => { e.preventDefault(); if (!invalid && !readOnly) onSave(d); }}>
        {conflict && initial.occurrence && (
          <div className="cal-conflict" role="alert">
            <Icon name="alert" size={16} />
            <div>
              <b>{t("cal.conflict.title")}</b>
              <p>{t("cal.conflict.body")}{conflict.remote_title && conflict.remote_title !== initial.occurrence.title ? ` ${t("cal.conflict.remote", { title: conflict.remote_title })}` : ""}</p>
              <div className="cal-conflict-actions">
                <button type="button" className="btn small" disabled={busy} onClick={() => onResolve(initial.occurrence!, "local")}>{t("cal.conflict.local")}</button>
                <button type="button" className="btn small" disabled={busy} onClick={() => onResolve(initial.occurrence!, "remote")}>{t("cal.conflict.remote.use")}</button>
              </div>
            </div>
          </div>
        )}
        {readOnly && <div className="cal-note"><Icon name="lock" size={14} /> {t("cal.readonly")}</div>}
        <input className="field cal-title-input" autoFocus={!initial.occurrence} required maxLength={240} value={d.title} onChange={(e) => set({ title: e.target.value })} placeholder={t("cal.field.title")} aria-label={t("cal.field.title")} disabled={readOnly} />
        <fieldset className="cal-fieldset" disabled={readOnly}>
          <div className="cal-line">
            <Icon name="clock" size={16} />
            <div className="cal-when">
              {/* One line for the usual case, a day and its two times; the end's own date only for
                  an event that runs into another day, as the calendars people know lay it out. */}
              <div className="cal-when-row">
                <input type="date" className="field cal-date" value={d.startDay} required aria-label={t("cal.field.start.date")} onChange={(e) => e.target.value && moveStart(e.target.value, d.startMin)} />
                {!d.allDay && <TimeSelect value={d.startMin} onChange={(m) => moveStart(d.startDay, m)} label={t("cal.field.start.time")} />}
                {!d.allDay && (
                  <span className="cal-when-end">
                    <span className="cal-when-dash" aria-hidden="true">–</span>
                    <TimeSelect value={d.endMin} after={sameDay ? d.startMin : undefined} onChange={(m) => set({ endMin: m })} label={t("cal.field.end.time")} />
                  </span>
                )}
              </div>
              {(d.allDay || !sameDay || endDate) ? (
                <div className="cal-when-row">
                  <span className="cal-when-label">{t(d.allDay ? "cal.field.last.day" : "cal.field.end.date")}</span>
                  <input type="date" className="field cal-date" value={d.endDay} min={d.startDay} required aria-label={t(d.allDay ? "cal.field.last.day" : "cal.field.end.date")} onChange={(e) => e.target.value && set({ endDay: e.target.value })} />
                </div>
              ) : (
                <button type="button" className="btn small ghost cal-end-day" onClick={() => setEndDate(true)}>{t("cal.field.end.other")}</button>
              )}
              {invalid && <div className="cal-error">{t("cal.invalid.range")}</div>}
              <label className="cal-switch-row">
                <Switch checked={d.allDay} onChange={(on) => set({ allDay: on, endDay: on ? d.endDay : d.startDay, endMin: on ? d.endMin : Math.min(1440, Math.max(d.endMin, d.startMin + 60)) })} label={t("cal.allday")} />
                <span>{t("cal.allday")}</span>
              </label>
              {!d.allDay && <ZonePicker value={d.timezone} onChange={(zone) => set({ timezone: zone })} />}
            </div>
          </div>
          <div className="cal-line">
            <Icon name="loop" size={16} />
            <div className="cal-grow">
              <select className="field" value={preset} onChange={(e) => pickPreset(e.target.value as Preset)} aria-label={t("cal.field.repeat")}>
                {presets.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
              </select>
              {preset === "custom" && (
                <div className="cal-custom">
                  <div className="cal-custom-row">
                    <span>{t("cal.repeat.every")}</span>
                    <input type="number" className="field cal-num" min={1} max={99} value={rule.interval} aria-label={t("cal.repeat.interval")} onChange={(e) => changeRule({ interval: Math.max(1, Math.min(99, Number(e.target.value) || 1)) })} />
                    <select className="field" value={rule.freq} aria-label={t("cal.repeat.unit")} onChange={(e) => changeRule({ freq: e.target.value as Freq, byday: e.target.value === "WEEKLY" ? (rule.byday.length ? rule.byday : [dayCode(d.startDay)]) : [] })}>
                      {(["DAILY", "WEEKLY", "MONTHLY", "YEARLY"] as Freq[]).map((f) => <option key={f} value={f}>{t(`cal.unit.${f.toLowerCase()}`)}</option>)}
                    </select>
                  </div>
                  {rule.freq === "WEEKLY" && (
                    <div className="cal-weekdays" role="group" aria-label={t("cal.repeat.on")}>
                      {[1, 2, 3, 4, 5, 6, 0].map((n) => {
                        const code = BYDAY[n] as ByDay;
                        const on = rule.byday.includes(code);
                        return <button key={code} type="button" aria-pressed={on} className={`cal-weekday ${on ? "on" : ""}`} onClick={() => changeRule({ byday: on ? rule.byday.filter((c) => c !== code) : [...rule.byday, code] })} title={t(`cal.weekday.${code.toLowerCase()}`)}>{t(`cal.weekday.${code.toLowerCase()}`).slice(0, 2)}</button>;
                      })}
                    </div>
                  )}
                  <div className="cal-custom-row">
                    <span>{t("cal.repeat.ends")}</span>
                    <select className="field" value={rule.until ? "until" : rule.count ? "count" : "never"} aria-label={t("cal.repeat.ends")} onChange={(e) => changeRule(e.target.value === "until" ? { until: addDays(d.startDay, 90), count: null } : e.target.value === "count" ? { count: 10, until: null } : { until: null, count: null })}>
                      <option value="never">{t("cal.repeat.never")}</option>
                      <option value="until">{t("cal.repeat.until")}</option>
                      <option value="count">{t("cal.repeat.after")}</option>
                    </select>
                    {rule.until && <input type="date" className="field cal-date" value={rule.until} min={d.startDay} aria-label={t("cal.repeat.until")} onChange={(e) => e.target.value && changeRule({ until: e.target.value })} />}
                    {rule.count && <input type="number" className="field cal-num" min={1} max={999} value={rule.count} aria-label={t("cal.repeat.count")} onChange={(e) => changeRule({ count: Math.max(1, Number(e.target.value) || 1) })} />}
                  </div>
                  <div className="cal-hint">{ruleText(d.recurrence)}</div>
                </div>
              )}
            </div>
          </div>
          <div className="cal-line">
            <span className="cal-dot big" style={{ "--c": d.color ?? calendar?.color ?? "var(--accent)" } as CSSProperties} aria-hidden="true" />
            <div className="cal-grow"><CalendarPicker calendars={calendars} value={d.calendar_id} onChange={(id) => { const next = calendars.find((c) => c.id === id); set({ calendar_id: id, reminders: initial.occurrence || !next ? d.reminders : next.default_reminders.length ? next.default_reminders : d.reminders }); }} /></div>
          </div>
          <div className="cal-line">
            <Icon name="bell" size={16} />
            <div className="cal-grow"><Reminders value={d.reminders} onChange={(reminders) => set({ reminders })} /></div>
          </div>
          {more ? (
            <>
              <div className="cal-line">
                <Icon name="pin" size={16} />
                <input className="field cal-grow" value={d.location} maxLength={500} onChange={(e) => set({ location: e.target.value })} placeholder={t("cal.field.location")} aria-label={t("cal.field.location")} />
              </div>
              <div className="cal-line top">
                <Icon name="journal" size={16} />
                <textarea className="field cal-grow" rows={4} value={d.description} onChange={(e) => set({ description: e.target.value })} placeholder={t("cal.field.description")} aria-label={t("cal.field.description")} />
              </div>
              <div className="cal-line">
                <Icon name="pen" size={16} />
                <div className="cal-grow"><ColorPicker value={d.color} none={calendar?.color ?? "var(--accent)"} onChange={(color) => set({ color })} label={t("cal.field.color")} /></div>
              </div>
            </>
          ) : (
            <button type="button" className="btn ghost cal-more-fields" onClick={() => setMore(true)}><Icon name="plus" size={14} /> {t("cal.more.fields")}</button>
          )}
        </fieldset>
        <div className="sheet-foot cal-foot">
          {initial.occurrence && !readOnly && <button type="button" className="btn danger" disabled={busy} onClick={() => onDelete(initial.occurrence!)}><Icon name="trash" size={14} /> {t("common.delete")}</button>}
          {initial.occurrence?.pending_sync && <span className="cal-pending"><Icon name="reload" size={12} /> {t("cal.pending")}</span>}
          <span className="cal-spacer" />
          <button type="button" className="btn" onClick={onClose}>{t("common.cancel")}</button>
          {!readOnly && <button type="submit" className="btn primary" disabled={busy || invalid || !d.title.trim()}>{t("common.save")}</button>}
        </div>
      </form>
    </Sheet>
  );
}
