// The month: whole weeks, an event that spans days drawn as one bar across them, timed events as a
// line with their start, and "N more" where a day holds more than its cell can show. On a phone the
// cells are too small for words, so a cell shows the day and a coloured mark per item, and the day
// picked in it is listed in full under the grid.

import { useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { locale, t } from "../i18n";
import { dayLabel, weekdayNames, type Day } from "./dates";
import { byStart, onDay } from "./items";
import { clip, packLanes } from "./layout";
import { ItemChip } from "./TimeGrid";
import type { Item, Task } from "./types";

type Props = {
  weeks: Day[][];
  month: string;
  items: Item[];
  zone: string;
  today: Day;
  weekStart: number;
  compact: boolean;
  selected: Day;
  dragTask: Task | null;
  onPickDay: (day: Day) => void;
  onMore: (day: Day) => void;
  onCreate: (day: Day) => void;
  onOpen: (item: Item) => void;
  onToggleTask: (task: Task) => void;
  onDropTask: (task: Task, day: Day) => void;
};

const LINE = 22;
const HEAD = 30;

export function MonthView(props: Props) {
  const { weeks, items, today, compact } = props;
  const grid = useRef<HTMLDivElement>(null);
  const [rowHeight, setRowHeight] = useState(110);
  const names = weekdayNames(locale(), props.weekStart, compact ? "narrow" : "short");

  // How many lines a cell has room for follows the window: measured, not guessed.
  useLayoutEffect(() => {
    const box = grid.current;
    if (!box || compact) return;
    const measure = () => {
      const row = box.querySelector(".cal-month-week") as HTMLElement | null;
      if (row) setRowHeight(row.getBoundingClientRect().height);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [compact, weeks.length]);
  const capacity = Math.max(1, Math.floor((rowHeight - HEAD - 4) / LINE));

  const sorted = useMemo(() => [...items].sort(byStart), [items]);

  return (
    <div ref={grid} className={`cal-month ${compact ? "compact" : ""}`} role="grid" aria-label={props.month} style={{ "--weeks": weeks.length } as CSSProperties}>
      <div className="cal-month-names" role="row">
        {names.map((name, i) => <div key={i} className="cal-month-name" role="columnheader">{name}</div>)}
      </div>
      {weeks.map((week) => compact ? (
        <div key={week[0]} className="cal-month-week" role="row">
          {week.map((day) => {
            const mine = sorted.filter((item) => onDay(item, day));
            const colours = mine.slice(0, 4);
            return (
              <button key={day} type="button" role="gridcell" className={`cal-month-cell ${day.slice(0, 7) !== props.month ? "outside" : ""} ${day === today ? "today" : ""} ${day === props.selected ? "selected" : ""}`}
                aria-selected={day === props.selected} aria-label={`${dayLabel(day, locale(), { weekday: "long", day: "numeric", month: "long" })}${mine.length ? `, ${t("cal.items.count", { n: mine.length })}` : ""}`}
                onClick={() => props.onPickDay(day)}>
                <span className="cal-month-num">{Number(day.slice(8, 10))}</span>
                <span className="cal-month-marks" aria-hidden="true">
                  {colours.map((item) => <span key={item.key} className={`cal-mark ${item.kind} ${item.allDay ? "bar" : ""}`} style={{ "--c": item.color } as CSSProperties} />)}
                </span>
              </button>
            );
          })}
        </div>
      ) : (
        <WeekRow key={week[0]} {...props} week={week} items={sorted} capacity={capacity} />
      ))}
    </div>
  );
}

function WeekRow({ week, items, capacity, ...props }: Props & { week: Day[]; capacity: number }) {
  const placed = useMemo(() => {
    const mine = items.filter((item) => item.lastDay >= week[0] && item.firstDay <= week[6]);
    // Bars first, longest first, then the single-day lines by start: a long trip keeps one straight
    // lane, and a day's timed events read top to bottom in the order they happen.
    const ordered = [...mine].sort((a, b) => {
      const spanA = a.allDay ? 0 : 1;
      const spanB = b.allDay ? 0 : 1;
      return spanA - spanB || byStart(a, b);
    });
    const spans = ordered.map((item) => {
      const first = week.findIndex((d) => d >= item.firstDay);
      let last = -1;
      for (let i = 6; i >= 0; i--) if (week[i] <= item.lastDay) { last = i; break; }
      return { item, span: clip(first, last + 1, 7)! };
    }).filter((s) => s.span);
    const lanes = packLanes(spans.map((s) => ({ id: s.item.key, first: s.span.first, last: s.span.last })), true);
    return spans.map((s) => ({ ...s, lane: lanes.get(s.item.key) ?? 0 }));
  }, [items, week]);

  // A day whose items need more lanes than fit gives its last line to "N more".
  const overflowing = week.map((_, index) => placed.some((p) => p.lane >= capacity && index >= p.span.first && index <= p.span.last));
  const visible = placed.filter((p) => p.lane < capacity - 1 || (p.lane === capacity - 1 && !overflowing.slice(p.span.first, p.span.last + 1).some(Boolean)));

  return (
    <div className="cal-month-week" role="row" style={{ "--lines": capacity } as CSSProperties}>
      {week.map((day, index) => (
        <div key={day} role="gridcell" className={`cal-month-cell ${day.slice(0, 7) !== props.month ? "outside" : ""} ${day === props.today ? "today" : ""}`} style={{ gridColumn: index + 1 }}
          onDragOver={(e) => { if (props.dragTask) e.preventDefault(); }}
          onDrop={(e) => { if (props.dragTask) { e.preventDefault(); props.onDropTask(props.dragTask, day); } }}>
          <button type="button" className="cal-month-new" tabIndex={-1} aria-label={t("cal.new.on", { day: dayLabel(day, locale(), { day: "numeric", month: "long" }) })} onClick={() => props.onCreate(day)} />
          <button type="button" className="cal-month-num" onClick={() => props.onPickDay(day)} aria-label={dayLabel(day, locale(), { weekday: "long", day: "numeric", month: "long" })} aria-current={day === props.today ? "date" : undefined}>
            {day.endsWith("-01") ? dayLabel(day, locale(), { day: "numeric", month: "short" }) : Number(day.slice(8, 10))}
          </button>
        </div>
      ))}
      <div className="cal-month-lanes">
        {visible.map(({ item, span, lane }) => (
          <ItemChip key={item.key} item={item} zone={props.zone} showTime className={`${item.allDay ? "cal-bar" : ""} ${item.firstDay < week[span.first] ? "cut-start" : ""} ${item.lastDay > week[span.last] ? "cut-end" : ""}`}
            style={{ gridColumn: `${span.first + 1} / ${span.last + 2}`, gridRow: lane + 1 }} onOpen={props.onOpen} onToggleTask={props.onToggleTask} />
        ))}
        {week.map((day, index) => {
          if (!overflowing[index]) return null;
          const hidden = placed.filter((p) => index >= p.span.first && index <= p.span.last).length - visible.filter((p) => index >= p.span.first && index <= p.span.last).length;
          return <button key={`more-${day}`} type="button" className="cal-more" style={{ gridColumn: index + 1, gridRow: capacity }} onClick={() => props.onMore(day)}>{t("cal.more", { n: hidden })}</button>;
        })}
      </div>
    </div>
  );
}
