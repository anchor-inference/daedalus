// The schedule as a list: day by day, each item with its time, colour, title and place. The agenda view
// is this over a month ahead; the phone's month view shows the one day picked in the grid with it.

import type { CSSProperties } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { dayLabel, diffDays, type Day } from "./dates";
import { byStart, onDay } from "./items";
import type { CalendarRow, Item, Task } from "./types";
import { itemTime, relativeDay } from "./words";

type Common = {
  items: Item[];
  zone: string;
  today: Day;
  now: number;
  calendars: CalendarRow[];
  onOpen: (item: Item) => void;
  onToggleTask: (task: Task) => void;
};

export function AgendaView({ days, onCreate, ...common }: Common & { days: Day[]; onCreate: () => void }) {
  const filled = days.map((day) => ({ day, items: common.items.filter((item) => onDay(item, day)).sort(byStart) })).filter((d) => d.items.length);
  if (!filled.length) {
    return (
      <div className="cal-agenda">
        <div className="empty cal-empty">
          <Icon name="calendar" size={28} />
          <b>{t("cal.agenda.empty.title")}</b>
          <div>{t("cal.agenda.empty.body")}</div>
          <button type="button" className="btn primary" onClick={onCreate}><Icon name="plus" size={16} /> {t("cal.new")}</button>
        </div>
      </div>
    );
  }
  return (
    <div className="cal-agenda" role="list" aria-label={t("cal.view.agenda")}>
      {filled.map(({ day, items }) => (
        <section key={day} className={`cal-agenda-day ${day === common.today ? "today" : ""}`} role="listitem" aria-label={relativeDay(day, common.today)}>
          <header className="cal-agenda-date">
            <span className="cal-agenda-num">{Number(day.slice(8, 10))}</span>
            <span className="cal-agenda-wd">
              <b>{Math.abs(diffDays(common.today, day)) <= 1 ? relativeDay(day, common.today) : dayLabel(day, locale(), { weekday: "long" })}</b>
              <small>{dayLabel(day, locale(), { month: "long", year: day.slice(0, 4) === common.today.slice(0, 4) ? undefined : "numeric" })}</small>
            </span>
          </header>
          <DayRows {...common} day={day} items={items} />
        </section>
      ))}
    </div>
  );
}

/** One day's items as rows. */
export function DayList({ day, ...common }: Common & { day: Day }) {
  const items = common.items.filter((item) => onDay(item, day)).sort(byStart);
  if (!items.length) return <div className="cal-day-empty">{t("cal.day.empty")}</div>;
  return <DayRows {...common} day={day} items={items} />;
}

function DayRows({ day, items, zone, now, calendars, onOpen, onToggleTask }: Common & { day: Day }) {
  return (
    <ul className="cal-rows">
      {items.map((item) => {
        const calendar = item.event ? calendars.find((c) => c.id === item.event!.calendar_id) : null;
        const time = itemTime(item, zone, day);
        const task = item.task;
        const meta = [item.location, calendar?.name].filter(Boolean).join(" · ");
        return (
          <li key={item.key} className={`cal-row ${item.kind} ${item.done ? "done" : ""} ${!item.allDay && item.end < now ? "past" : ""}`} style={{ "--c": item.color } as CSSProperties}>
            <span className="cal-row-time">{time.includes(" – ") ? <>{time.split(" – ")[0]}<small>{time.split(" – ")[1]}</small></> : time}</span>
            {task ? (
              <button type="button" className={`cal-check ${item.done ? "on" : ""}`} role="checkbox" aria-checked={!!item.done} aria-label={t("cal.task.complete.named", { title: item.title })} onClick={() => onToggleTask(task)}>
                {item.done && <Icon name="check" size={12} />}
              </button>
            ) : <span className="cal-row-bar" aria-hidden="true" />}
            <button type="button" className="cal-row-open" onClick={() => onOpen(item)}>
              <span className="cal-row-title"><span className="cal-row-name">{item.title || t("cal.untitled")}</span>{item.event?.recurring && <Icon name="loop" size={12} />}</span>
              {meta && <span className="cal-row-meta">{meta}</span>}
            </button>
          </li>
        );
      })}
    </ul>
  );
}
