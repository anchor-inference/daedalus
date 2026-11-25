// The planner's own settings: the shape of the week and the working day, what a new event starts
// with, and the time zone everything is drawn in. Saved as a whole, the way the host stores them.

import { useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { Button, Segmented, Select, Sheet, Switch } from "../ui/primitives";
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
      onClose();
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  const views = ["day", "week", "month", "agenda"] as const;
  return (
    <Sheet title={t("cal.settings")} onClose={onClose} size="md" className="cal-settings"
      footer={<>
        <span className="cal-spacer" />
        <Button onClick={onClose}>{t("common.cancel")}</Button>
        <Button variant="primary" disabled={busy || invalid} onClick={() => void save()}>{t("common.save")}</Button>
      </>}>
      <div className="cal-set">
        <div className="cal-set-label">{t("cal.settings.section.week")}</div>
        <div className="cal-set-card">
          <Row label={t("cal.settings.week.start")}>
            <Segmented value={String(s.week_start) as "0" | "1"} onChange={(v) => set({ week_start: Number(v) as 0 | 1 })} label={t("cal.settings.week.start")} options={[{ id: "1", label: t("cal.weekday.mo") }, { id: "0", label: t("cal.weekday.su") }]} />
          </Row>
          <Row label={t("cal.settings.weekends")}>
            <Switch checked={s.show_weekends} onChange={(on) => set({ show_weekends: on })} label={t("cal.settings.weekends")} />
          </Row>
          <Row label={t("cal.settings.view")}>
            <Select value={s.default_view} label={t("cal.settings.view")} onChange={(v) => set({ default_view: v })} options={views.map((v) => ({ id: v, label: t(`cal.view.${v}`) }))} />
          </Row>
        </div>
        <div className="cal-set-label">{t("cal.settings.section.day")}</div>
        <div className="cal-set-card">
          <Row label={t("cal.settings.work")} hint={invalid ? t("cal.invalid.range") : t("cal.settings.work.hint")} bad={invalid}>
            <span className="cal-settings-pair">
              <ClockSelect value={s.work_start} onChange={(v) => set({ work_start: v })} label={t("cal.settings.work.start")} />
              <span aria-hidden="true">–</span>
              <ClockSelect value={s.work_end} onChange={(v) => set({ work_end: v })} label={t("cal.settings.work.end")} />
            </span>
          </Row>
          <Row label={t("cal.settings.zone")} hint={t("cal.settings.zone.hint")}>
            <ZonePicker value={s.timezone} onChange={(timezone) => set({ timezone })} />
          </Row>
        </div>
        <div className="cal-set-label">{t("cal.settings.section.new")}</div>
        <div className="cal-set-card">
          <Row label={t("cal.settings.duration")}>
            <Select value={String(s.default_duration)} label={t("cal.settings.duration")} onChange={(m) => set({ default_duration: Number(m) })} options={DURATIONS.map((m) => ({ id: String(m), label: duration(m) }))} />
          </Row>
          <Row label={t("cal.settings.reminders")} wide>
            <Reminders value={s.default_reminders} onChange={(default_reminders) => set({ default_reminders })} />
          </Row>
        </div>
      </div>
    </Sheet>
  );
}

/** A card's row: what it sets and, under it, the one line that says what that does; the control at the end. */
function Row({ label, hint, children, wide, bad }: { label: string; hint?: string; children: React.ReactNode; wide?: boolean; bad?: boolean }) {
  return (
    <div className={`cal-set-row ${wide ? "wide" : ""}`}>
      <div className="cal-set-title"><span>{label}</span>{hint && <small className={bad ? "bad" : ""}>{hint}</small>}</div>
      <div className="cal-set-control">{children}</div>
    </div>
  );
}
