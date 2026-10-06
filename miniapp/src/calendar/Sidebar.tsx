// The planner's side column on a desktop, and its drawer on a phone: the month to pick a date in, the
// calendars with their colours and whether each is shown, the connected ones with how their sync is
// going, and the tasks.

import { useState, type CSSProperties, type ReactNode } from "react";
import { api } from "../api";
import { relTime } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { OverflowMenu, Sheet } from "../ui/dialogs";
import { confirmAsync, errorText } from "../ui";
import { invalidate, prime } from "../store";
import { CALENDARS, PALETTE, refreshPlanner } from "./data";
import { ColorPicker } from "./fields";
import type { CalendarRow } from "./types";
import { calendarName } from "./words";

export function CalendarsSection({ calendars, onConnections, toast }: { calendars: CalendarRow[]; onConnections: () => void; toast: (text: string) => void }) {
  const [editing, setEditing] = useState<CalendarRow | "new" | null>(null);
  const mine = calendars.filter((c) => c.kind === "local").sort((a, b) => a.position - b.position);
  const connected = calendars.filter((c) => c.kind !== "local").sort((a, b) => a.position - b.position);

  async function toggle(calendar: CalendarRow) {
    // Shown or hidden at once; the host is told after, and the events read again when it answers.
    prime(CALENDARS, calendars.map((c) => (c.id === calendar.id ? { ...c, visible: !c.visible } : c)));
    try { await api.patch(`${CALENDARS}/${encodeURIComponent(calendar.id)}`, { visible: !calendar.visible }); }
    catch (exc) { toast(errorText(exc)); }
    invalidate(CALENDARS);
    refreshPlanner();
  }

  async function remove(calendar: CalendarRow) {
    if (!(await confirmAsync(t("cal.calendar.delete.title", { name: calendarName(calendar) }), { body: t("cal.calendar.delete.body"), action: t("common.delete") }))) return;
    try { await api.delete(`${CALENDARS}/${encodeURIComponent(calendar.id)}`); toast(t("cal.calendar.deleted")); }
    catch (exc) { toast(errorText(exc)); }
    invalidate(CALENDARS);
    refreshPlanner();
  }

  const row = (calendar: CalendarRow, extra?: ReactNode) => (
    <li key={calendar.id} className={`cal-cal ${calendar.visible ? "" : "hidden"}`} style={{ "--c": calendar.color } as CSSProperties}>
      <label className="cal-cal-toggle">
        <input type="checkbox" checked={calendar.visible} onChange={() => void toggle(calendar)} aria-label={t("cal.calendar.show", { name: calendarName(calendar) })} />
        <span className="cal-cal-box" aria-hidden="true">{calendar.visible && <Icon name="check" size={11} />}</span>
        <span className="cal-cal-name">{calendarName(calendar)}</span>
      </label>
      {extra}
      <OverflowMenu small label={t("cal.calendar.menu", { name: calendarName(calendar) })} items={[
        { label: t("cal.calendar.edit"), icon: "pen", onSelect: () => setEditing(calendar) },
        ...(calendar.kind === "local" && mine.length > 1 ? [{ label: t("common.delete"), icon: "trash" as const, danger: true, onSelect: () => void remove(calendar) }] : []),
        ...(calendar.kind !== "local" ? [{ label: t("cal.connections.manage"), icon: "plug" as const, onSelect: onConnections }] : []),
      ]} />
    </li>
  );

  return (
    <>
      <section className="cal-side-section" aria-label={t("cal.calendars.mine")}>
        <div className="cal-side-head">
          <span>{t("cal.calendars.mine")}</span>
          <button type="button" className="iconbtn small" onClick={() => setEditing("new")} aria-label={t("cal.calendar.add")} title={t("cal.calendar.add")}><Icon name="plus" size={14} /></button>
        </div>
        <ul className="cal-cals">{mine.map((c) => row(c))}</ul>
      </section>
      <section className="cal-side-section" aria-label={t("cal.calendars.connected")}>
        <div className="cal-side-head">
          <span>{t("cal.calendars.connected")}</span>
          <button type="button" className="iconbtn small" onClick={onConnections} aria-label={t("cal.connections")} title={t("cal.connections")}><Icon name="plug" size={14} /></button>
        </div>
        {connected.length ? (
          <ul className="cal-cals">{connected.map((c) => row(c, <SyncMark calendar={c} />))}</ul>
        ) : (
          <button type="button" className="cal-side-empty" onClick={onConnections}><Icon name="plus" size={13} /> {t("cal.connect.cta")}</button>
        )}
      </section>
      {editing && <CalendarSheet calendar={editing === "new" ? null : editing} onClose={() => setEditing(null)} toast={toast} />}
    </>
  );
}

function SyncMark({ calendar }: { calendar: CalendarRow }) {
  const sync = calendar.sync;
  if (!sync) return null;
  const label = sync.status === "ok" ? t("cal.sync.ok", { when: relTime(sync.last_sync_at) }) : sync.status === "error" ? `${t("cal.sync.error")}: ${sync.error}` : t(`cal.sync.${sync.status}`);
  return (
    <span className={`cal-sync ${sync.status}`} title={label} aria-label={label} role="img">
      <Icon name={sync.status === "error" ? "alert" : sync.status === "syncing" ? "reload" : sync.status === "never" ? "clock" : "check"} size={12} />
    </span>
  );
}

function CalendarSheet({ calendar, onClose, toast }: { calendar: CalendarRow | null; onClose: () => void; toast: (text: string) => void }) {
  const [name, setName] = useState(calendar ? calendarName(calendar) : "");
  const [color, setColor] = useState(calendar?.color ?? PALETTE[0].hex);
  const [busy, setBusy] = useState(false);
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      if (calendar) await api.patch(`${CALENDARS}/${encodeURIComponent(calendar.id)}`, { name: name.trim(), color });
      else await api.post(CALENDARS, { name: name.trim(), color });
      invalidate(CALENDARS);
      refreshPlanner();
      toast(t(calendar ? "cal.calendar.saved" : "cal.calendar.created"));
      onClose();
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }
  return (
    <Sheet title={t(calendar ? "cal.calendar.edit" : "cal.calendar.add")} onClose={onClose} size="narrow">
      <form className="cal-form" onSubmit={save}>
        <label className="field" htmlFor="cal-cal-name">{t("cal.calendar.name")}</label>
        <input id="cal-cal-name" className="field" autoFocus required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
        <span className="cal-label">{t("cal.field.color")}</span>
        <ColorPicker value={color} onChange={(hex) => hex && setColor(hex)} label={t("cal.field.color")} />
        <div className="sheet-foot cal-foot">
          <span className="cal-spacer" />
          <button type="button" className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button type="submit" className="btn primary" disabled={busy || !name.trim()}>{t("common.save")}</button>
        </div>
      </form>
    </Sheet>
  );
}
