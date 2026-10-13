// The schedule as a list: day by day, each item a compact row — its time, its type's icon, the type's
// chip ("Завтрак · 518 ккал") or its title, and how far its list has got — and under each day the
// totals its types keep ("Σ 2 028 ккал ✓"). The agenda view is this over a month ahead, and the
// phone's first view; the phone's month view shows the one day picked in the grid with it.
//
// On a touch screen a row slides: to the right to mark it done, to the left to skip it, as far as
// its type allows; the row says what letting go will do before it is let go.

import { useRef, useState, type CSSProperties } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { aggregateParts, swipeActions, type AggregateItem, type EventStatus, type TypeDef } from "./blocks";
import { dayLabel, diffDays, type Day } from "./dates";
import { byStart, onDay } from "./items";
import { ProgressPill, TypeIcon, statusLabel } from "./structured";
import type { CalendarRow, Item, Task } from "./types";
import { calendarName, itemTime, relativeDay } from "./words";

type Common = {
  items: Item[];
  zone: string;
  today: Day;
  now: number;
  calendars: CalendarRow[];
  types: TypeDef[];
  aggregates: Map<Day, AggregateItem[]>;
  dense: boolean;
  onOpen: (item: Item) => void;
  onToggleTask: (task: Task) => void;
  onStatus: (item: Item, status: EventStatus) => void;
};

/** `embedded` draws the list inside another scroll (a group's page) rather than as a view of its own. */
export function AgendaView({ days, onCreate, embedded, ...common }: Common & { days: Day[]; onCreate: () => void; embedded?: boolean }) {
  const filled = days.map((day) => ({ day, items: common.items.filter((item) => onDay(item, day)).sort(byStart) })).filter((d) => d.items.length);
  if (!filled.length) {
    return (
      <div className={`cal-agenda ${embedded ? "embedded" : ""}`}>
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
    <div className={`cal-agenda ${common.dense ? "dense" : ""} ${embedded ? "embedded" : ""}`} role="list" aria-label={t("cal.view.agenda")}>
      {filled.map(({ day, items }) => (
        <section key={day} className={`cal-agenda-day ${day === common.today ? "today" : ""} ${day < common.today ? "past" : ""}`} role="listitem" aria-label={relativeDay(day, common.today)} data-day={day}>
          <header className="cal-agenda-date">
            <span className="cal-agenda-num">{Number(day.slice(8, 10))}</span>
            <span className="cal-agenda-wd">
              <b>{Math.abs(diffDays(common.today, day)) <= 1 ? relativeDay(day, common.today) : dayLabel(day, locale(), { weekday: "long" })}</b>
              <small>{dayLabel(day, locale(), { month: "long", year: day.slice(0, 4) === common.today.slice(0, 4) ? undefined : "numeric" })}</small>
            </span>
          </header>
          <div className="cal-agenda-items">
            <DayRows {...common} day={day} items={items} />
            <DayTotals items={common.aggregates.get(day)} types={common.types} />
          </div>
        </section>
      ))}
    </div>
  );
}

/** One day's items as rows, with the day's totals under them. */
export function DayList({ day, ...common }: Common & { day: Day }) {
  const items = common.items.filter((item) => onDay(item, day)).sort(byStart);
  if (!items.length) return <div className="cal-day-empty">{t("cal.day.empty")}</div>;
  return (
    <>
      <DayRows {...common} day={day} items={items} />
      <DayTotals items={common.aggregates.get(day)} types={common.types} />
    </>
  );
}

/** A day's totals: each type's sum (or average) with its unit, a tick when it met the type's target
 *  and an arrow when it fell short or ran over. */
export function DayTotals({ items, types }: { items: AggregateItem[] | undefined; types: TypeDef[] }) {
  if (!items?.length) return null;
  return (
    <ul className="cal-totals-day" aria-label={t("cal.day.totals")}>
      {items.map((item) => {
        const type = types.find((x) => x.id === item.type);
        const parts = aggregateParts(item);
        const range = item.target ? t("cal.target", { min: item.target[0].toLocaleString(locale()), max: item.target[1].toLocaleString(locale()) }) : "";
        const label = [item.label, range].filter(Boolean).join(" · ");
        return (
          <li key={`${item.type}:${item.field}:${item.op}`} className={`cal-agg ${parts.state}`} title={label} style={{ "--c": type?.color ?? "var(--fg-3)" } as CSSProperties}>
            <TypeIcon icon={type?.icon} color={type?.color} size={12} />
            <span className="cal-agg-value">{parts.symbol} {parts.value}</span>
            {parts.state === "ok" && <Icon name="check" size={12} />}
            {parts.state === "low" && <span className="cal-agg-mark" aria-label={t("cal.target.low")}>↓</span>}
            {parts.state === "high" && <span className="cal-agg-mark" aria-label={t("cal.target.high")}>↑</span>}
            {item.label && <span className="cal-agg-label">{item.label}</span>}
          </li>
        );
      })}
    </ul>
  );
}

function DayRows({ day, items, ...common }: Common & { day: Day }) {
  return (
    <ul className="cal-rows">
      {items.map((item) => <Row key={item.key} item={item} day={day} {...common} />)}
    </ul>
  );
}

const SWIPE_AT = 72;

function Row({ item, day, zone, now, calendars, types, dense, onOpen, onToggleTask, onStatus }: Omit<Common, "items" | "aggregates"> & { item: Item; day: Day }) {
  const event = item.event;
  const type = event?.type ? types.find((x) => x.id === event.type) : undefined;
  const calendar = event ? calendars.find((c) => c.id === event.calendar_id) : null;
  const time = itemTime(item, zone, day);
  const task = item.task;
  const status = event?.status ?? "planned";
  const chip = item.chip;
  const main = chip || item.title || t("cal.untitled");
  const second = chip ? item.title : "";
  const meta = [second, item.location, !type && calendar && calendarName(calendar)].filter(Boolean).join(" · ");
  const actions = event && event.writable ? swipeActions(type) : { right: null, left: null };
  const [dx, setDxState] = useState(0);
  // The distance is read back from a ref when the finger lifts: two touch events can arrive before
  // React draws again, and the drawn state would still say the row had not moved.
  const moved = useRef(0);
  const setDx = (value: number) => { moved.current = value; setDxState(value); };
  const start = useRef<{ x: number; y: number; engaged: boolean } | null>(null);

  // A sideways slide of the row, told apart from a scroll by its first few pixels; once it is a
  // slide it is the row's, and the view does not also turn to the next day under it.
  const onTouchStart = (e: React.TouchEvent) => {
    if (!actions.right && !actions.left) return;
    const p = e.touches[0];
    start.current = { x: p.clientX, y: p.clientY, engaged: false };
  };
  const onTouchMove = (e: React.TouchEvent) => {
    const s = start.current;
    if (!s) return;
    const p = e.touches[0];
    const x = p.clientX - s.x;
    const y = p.clientY - s.y;
    if (!s.engaged) {
      if (Math.abs(y) > 10 && Math.abs(y) > Math.abs(x)) { start.current = null; return; }
      if (Math.abs(x) < 12) return;
      s.engaged = true;
    }
    e.stopPropagation();
    const allowed = (x > 0 && actions.right) || (x < 0 && actions.left) ? x : x / 4;
    setDx(Math.max(-140, Math.min(140, allowed)));
  };
  const onTouchEnd = (e: React.TouchEvent) => {
    const s = start.current;
    start.current = null;
    if (!s?.engaged) return;
    e.stopPropagation();
    const distance = moved.current;
    const done = distance >= SWIPE_AT ? actions.right : distance <= -SWIPE_AT ? actions.left : null;
    setDx(0);
    if (done) onStatus(item, status === done ? "planned" : done);
  };
  const reveal = dx > 0 ? actions.right : dx < 0 ? actions.left : null;
  const armed = Math.abs(dx) >= SWIPE_AT;

  return (
    <li className={`cal-row-wrap ${dx ? "sliding" : ""}`}>
      {reveal && (
        <span className={`cal-row-under ${dx > 0 ? "right" : "left"} ${armed ? "armed" : ""}`} aria-hidden="true">
          <Icon name={reveal === "done" ? "check" : "forward"} size={16} />
          {status === reveal ? t("cal.status.back") : statusLabel(reveal)}
        </span>
      )}
      <div className={`cal-row ${item.kind} ${item.done || status === "done" ? "done" : ""} ${status === "skipped" || status === "cancelled" ? "skipped" : ""} ${!item.allDay && item.end < now ? "past" : ""} ${dense ? "dense" : ""}`}
        style={{ "--c": item.typeColor ?? item.color, transform: dx ? `translateX(${dx}px)` : undefined } as CSSProperties}
        onTouchStart={onTouchStart} onTouchMove={onTouchMove} onTouchEnd={onTouchEnd} onTouchCancel={() => { start.current = null; setDx(0); }}>
        <span className="cal-row-time">{time.includes(" – ") ? <>{time.split(" – ")[0]}<small>{time.split(" – ")[1]}</small></> : time}</span>
        {task ? (
          <button type="button" className={`cal-check ${item.done ? "on" : ""}`} role="checkbox" aria-checked={!!item.done} aria-label={t("cal.task.complete.named", { title: item.title })} onClick={() => onToggleTask(task)}>
            {item.done && <Icon name="check" size={12} />}
          </button>
        ) : type ? <TypeIcon icon={type.icon} color={type.color} size={15} className="cal-row-icon" /> : <span className="cal-row-bar" aria-hidden="true" />}
        <button type="button" className="cal-row-open" onClick={() => onOpen(item)} aria-label={[main, second, time, status !== "planned" ? statusLabel(status) : ""].filter(Boolean).join(", ")}>
          <span className="cal-row-title"><span className="cal-row-name">{main}</span>{event?.recurring && <Icon name="loop" size={12} />}</span>
          {meta && !dense && <span className="cal-row-meta">{meta}</span>}
        </button>
        <span className="cal-row-end">
          {status === "done" && !task && <span className="cal-row-status done" title={statusLabel(status)}><Icon name="check" size={13} /></span>}
          {status === "skipped" && <span className="cal-row-status skipped" title={statusLabel(status)}>{statusLabel(status)}</span>}
          <ProgressPill progress={event?.progress} />
        </span>
      </div>
    </li>
  );
}
