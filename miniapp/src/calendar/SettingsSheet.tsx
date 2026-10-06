// The planner's own settings: the shape of the week and the working day, what a new event starts
// with, and the time zone everything is drawn in. Saved as a whole, the way the host stores them.

import { useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { Sheet } from "../ui/dialogs";
import { Segmented, Switch } from "../ui/components";
import { errorText } from "../ui";
import { prime } from "../store";
import { parseClock } from "./dates";
import { SETTINGS } from "./data";
import { ClockSelect, Reminders, ZonePicker } from "./fields";
import type { CalendarSettings } from "./types";
import { duration } from "./words";

const DURATIONS = [15, 30, 45, 60, 90, 120];

export function SettingsSheet({ settings, zone, onClose, toast }: { settings: CalendarSettings; zone: string; onClose: () => void; toast: (text: string) => void }) {
  const [s, setS] = useState<CalendarSettings>({ ...settings, timezone: settings.timezone || zone });
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<CalendarSettings>) => setS((cur) => ({ ...cur, ...patch }));
  const invalid = (parseClock(s.work_end) ?? 0) <= (parseClock(s.work_start) ?? 0);

  async function save() {
    setBusy(true);
    try {
      const saved = await api.put<CalendarSettings>(SETTINGS, s);
      prime(SETTINGS, saved ?? s);
      toast(t("cal.settings.saved"));
      onClose();
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  return (
    <Sheet title={t("cal.settings")} onClose={onClose} size="narrow" className="cal-settings">
      <div className="cal-settings-body">
        <Row label={t("cal.settings.week.start")}>
          <Segmented value={String(s.week_start) as "0" | "1"} onChange={(v) => set({ week_start: Number(v) as 0 | 1 })} label={t("cal.settings.week.start")} options={[{ id: "1", label: t("cal.weekday.mo") }, { id: "0", label: t("cal.weekday.su") }]} />
        </Row>
        <Row label={t("cal.settings.weekends")}>
          <Switch checked={s.show_weekends} onChange={(on) => set({ show_weekends: on })} label={t("cal.settings.weekends")} />
        </Row>
        <Row label={t("cal.settings.work")} hint={invalid ? t("cal.invalid.range") : t("cal.settings.work.hint")} bad={invalid}>
          <span className="cal-settings-pair">
            <ClockSelect value={s.work_start} onChange={(v) => set({ work_start: v })} label={t("cal.settings.work.start")} />
            <span aria-hidden="true">–</span>
            <ClockSelect value={s.work_end} onChange={(v) => set({ work_end: v })} label={t("cal.settings.work.end")} />
          </span>
        </Row>
        <Row label={t("cal.settings.view")}>
          <select className="field" value={s.default_view} onChange={(e) => set({ default_view: e.target.value as CalendarSettings["default_view"] })} aria-label={t("cal.settings.view")}>
            {(["day", "week", "month", "agenda"] as const).map((v) => <option key={v} value={v}>{t(`cal.view.${v}`)}</option>)}
          </select>
        </Row>
        <Row label={t("cal.settings.duration")}>
          <select className="field" value={s.default_duration} onChange={(e) => set({ default_duration: Number(e.target.value) })} aria-label={t("cal.settings.duration")}>
            {DURATIONS.map((m) => <option key={m} value={m}>{duration(m)}</option>)}
          </select>
        </Row>
        <Row label={t("cal.settings.reminders")} wide>
          <Reminders value={s.default_reminders} onChange={(default_reminders) => set({ default_reminders })} />
        </Row>
        <Row label={t("cal.settings.zone")} hint={t("cal.settings.zone.hint")} wide>
          <ZonePicker value={s.timezone} onChange={(timezone) => set({ timezone })} />
        </Row>
        <div className="sheet-foot cal-foot">
          <span className="cal-spacer" />
          <button type="button" className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button type="button" className="btn primary" disabled={busy || invalid} onClick={() => void save()}>{t("common.save")}</button>
        </div>
      </div>
    </Sheet>
  );
}

function Row({ label, hint, children, wide, bad }: { label: string; hint?: string; children: React.ReactNode; wide?: boolean; bad?: boolean }) {
  return (
    <div className={`cal-settings-row ${wide ? "wide" : ""}`}>
      <div className="cal-settings-label"><span>{label}</span>{hint && <small className={bad ? "bad" : ""}>{hint}</small>}</div>
      <div className="cal-settings-control">{children}</div>
    </div>
  );
}
