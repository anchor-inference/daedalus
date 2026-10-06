// The small month in the sidebar: a date picker that shows which days the main view covers and which
// days have something on them, and pages through months without moving the main view.

import { useEffect, useState } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { addMonths, dayLabel, monthStart, monthWeeks, weekdayNames, type Day } from "./dates";

export function MiniMonth({ anchor, today, weekStart, rangeStart, rangeEnd, busy, onPick }: { anchor: Day; today: Day; weekStart: number; rangeStart: Day; rangeEnd: Day; busy: Set<Day>; onPick: (day: Day) => void }) {
  const [shown, setShown] = useState(monthStart(anchor));
  // The main view moving to another month brings the picker along; paging the picker does not move the view.
  useEffect(() => setShown(monthStart(anchor)), [anchor.slice(0, 7)]); // eslint-disable-line react-hooks/exhaustive-deps
  const weeks = monthWeeks(shown, weekStart);
  const names = weekdayNames(locale(), weekStart, "narrow");
  return (
    <div className="cal-mini" role="group" aria-label={t("cal.picker")}>
      <div className="cal-mini-head">
        <span className="cal-mini-title">{dayLabel(shown, locale(), { month: "long", year: "numeric" })}</span>
        <button type="button" className="iconbtn small" onClick={() => setShown(addMonths(shown, -1))} aria-label={t("cal.month.previous")} title={t("cal.month.previous")}><Icon name="back" size={14} /></button>
        <button type="button" className="iconbtn small" onClick={() => setShown(addMonths(shown, 1))} aria-label={t("cal.month.next")} title={t("cal.month.next")}><Icon name="forward" size={14} /></button>
      </div>
      <div className="cal-mini-grid">
        {names.map((name, i) => <span key={i} className="cal-mini-wd" aria-hidden="true">{name}</span>)}
        {weeks.flat().map((day) => (
          <button key={day} type="button"
            className={`cal-mini-day ${day.slice(0, 7) !== shown.slice(0, 7) ? "outside" : ""} ${day === today ? "today" : ""} ${day >= rangeStart && day < rangeEnd ? "inrange" : ""} ${day === anchor ? "anchor" : ""} ${busy.has(day) ? "busy" : ""}`}
            onClick={() => onPick(day)} aria-label={dayLabel(day, locale(), { weekday: "long", day: "numeric", month: "long", year: "numeric" })} aria-current={day === today ? "date" : undefined}>
            {Number(day.slice(8, 10))}
          </button>
        ))}
      </div>
    </div>
  );
}
