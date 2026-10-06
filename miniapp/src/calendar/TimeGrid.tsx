// The time grid of the day, three-day and week views: a column a day, an hour a row, the all-day row
// above, and the current time as a line across today. With a mouse, a drag on empty time selects a
// slot to create in, a drag on an event moves it and a drag on its lower edge resizes it; a task
// dragged from the list is dropped onto a time to block it out. A finger taps to create and taps an
// event to open it, because a finger dragging over the grid is scrolling it.

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { clockLabel, dayLabel, snap, type Day } from "./dates";
import { byStart, segment } from "./items";
import { clip, layoutDay, packLanes } from "./layout";
import type { Item, Task } from "./types";
import { itemTime } from "./words";

export type Slot = { day: Day; start: number; end: number };

type Props = {
  days: Day[];
  items: Item[];
  zone: string;
  today: Day;
  now: number;
  nowMinutes: number;
  workStart: number;
  workEnd: number;
  hourHeight: number;
  phone: boolean;
  /** Changes when the view should scroll to now (Today, a new view). */
  scrollToken: number;
  dragTask: Task | null;
  defaultDuration: number;
  selection: Slot | null;
  onSelect: (slot: Slot, rect: DOMRect | null) => void;
  onAllDay: (day: Day) => void;
  onOpen: (item: Item) => void;
  onMove: (item: Item, startDay: Day, startMinutes: number, endDay: Day, endMinutes: number) => void;
  onToggleTask: (task: Task) => void;
  onDropTask: (task: Task, day: Day, minutes: number | null) => void;
  onPickDay: (day: Day) => void;
};

type Drag =
  | { kind: "create"; day: number; from: number; to: number; x: number; y: number; moved: boolean }
  | { kind: "move" | "resize"; item: Item; dayShift: number; startShift: number; endShift: number; x: number; y: number; moved: boolean; originDay: number; originMinutes: number };

const STEP = 15;

export function TimeGrid(props: Props) {
  const { days, items, zone, today, hourHeight: H, phone } = props;
  const scroller = useRef<HTMLDivElement>(null);
  const columns = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<Drag | null>(null);
  const dragRef = useRef<Drag | null>(null);
  dragRef.current = drag;
  const suppressClick = useRef(false);
  const [dropAt, setDropAt] = useState<{ day: number; minutes: number } | null>(null);
  const [allDayOpen, setAllDayOpen] = useState(false);

  const timed = useMemo(() => items.filter((i) => !i.allDay), [items]);
  const allDay = useMemo(() => items.filter((i) => i.allDay && i.lastDay >= days[0] && i.firstDay <= days[days.length - 1]).sort(byStart), [items, days]);

  // All-day bars: one per item, across the visible days it covers, on the lowest free lane.
  const bars = useMemo(() => {
    const spans = allDay.map((item) => {
      const first = days.findIndex((d) => d >= item.firstDay);
      let last = -1;
      for (let i = days.length - 1; i >= 0; i--) if (days[i] <= item.lastDay) { last = i; break; }
      return { item, span: first < 0 || last < first ? null : clip(first, last + 1, days.length) };
    }).filter((s): s is { item: Item; span: NonNullable<ReturnType<typeof clip>> } => !!s.span);
    const lanes = packLanes(spans.map((s) => ({ id: s.item.key, first: s.span.first, last: s.span.last })));
    return spans.map((s) => ({ ...s, lane: lanes.get(s.item.key) ?? 0, before: s.item.firstDay < days[0], after: s.item.lastDay > days[days.length - 1] }));
  }, [allDay, days]);
  const laneCount = bars.reduce((n, b) => Math.max(n, b.lane + 1), 0);
  const cap = phone ? 2 : 3;
  const collapsed = !allDayOpen && laneCount > cap;
  const shownLanes = collapsed ? cap - 1 : laneCount;

  // Each day's timed pieces, laid out side by side where they overlap.
  const columnsData = useMemo(() => days.map((day) => {
    const pieces = timed.map((item) => ({ item, piece: segment(item, day, zone) })).filter((p): p is { item: Item; piece: NonNullable<ReturnType<typeof segment>> } => !!p.piece);
    const placed = layoutDay(pieces.map((p) => ({ id: p.item.key, start: p.piece.start, end: p.piece.end })));
    return { day, pieces, placed };
  }), [days, timed, zone]);

  // Scroll to now on today's view, otherwise to the start of the working day, when the view opens and
  // whenever Today is pressed. The working day is put an hour below the top so its first event is
  // not flush against the header.
  useLayoutEffect(() => {
    const box = scroller.current;
    if (!box) return;
    const minutes = days.includes(today) ? Math.max(0, props.nowMinutes - 90) : Math.max(0, props.workStart - 60);
    box.scrollTop = (minutes / 60) * H;
    // The days in view change the target too, but only a new view or Today scrolls; moving a week
    // keeps the hours the reader was looking at, as dedicated calendars do.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.scrollToken, H]);

  /** The day column and minute under a pointer, or null outside the columns. */
  function locate(x: number, y: number): { day: number; minutes: number } | null {
    const box = columns.current?.getBoundingClientRect();
    if (!box) return null;
    const day = Math.floor(((x - box.left) / box.width) * days.length);
    if (day < 0 || day >= days.length) return null;
    const minutes = Math.min(1440, Math.max(0, ((y - box.top) / H) * 60));
    return { day, minutes };
  }

  function onBodyPointerDown(e: React.PointerEvent<HTMLDivElement>) {
    if (e.button !== 0 || (e.target as HTMLElement).closest(".cal-block")) return;
    const at = locate(e.clientX, e.clientY);
    if (!at) return;
    const from = Math.floor(at.minutes / STEP) * STEP;
    if (e.pointerType === "touch") {
      // A finger only taps: remember where, and let a scroll cancel it.
      setDrag({ kind: "create", day: at.day, from, to: from, x: e.clientX, y: e.clientY, moved: false });
      return;
    }
    e.preventDefault();
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    setDrag({ kind: "create", day: at.day, from, to: from + STEP, x: e.clientX, y: e.clientY, moved: false });
  }

  function onBlockPointerDown(e: React.PointerEvent<HTMLElement>, item: Item, resize: boolean) {
    if (e.button !== 0 || e.pointerType === "touch" || !movable(item)) return;
    if ((e.target as HTMLElement).closest(".cal-check")) return;
    const at = locate(e.clientX, e.clientY);
    if (!at) return;
    e.stopPropagation();
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    setDrag({ kind: resize ? "resize" : "move", item, dayShift: 0, startShift: 0, endShift: 0, x: e.clientX, y: e.clientY, moved: false, originDay: at.day, originMinutes: at.minutes });
  }

  function onPointerMove(e: React.PointerEvent) {
    const current = dragRef.current;
    if (!current) return;
    const moved = current.moved || Math.abs(e.clientX - current.x) > 4 || Math.abs(e.clientY - current.y) > 4;
    if (current.kind === "create") {
      if (e.pointerType === "touch") {
        if (moved) setDrag(null);
        return;
      }
      const at = locate(e.clientX, e.clientY);
      if (!at) return;
      const to = Math.max(current.from + STEP, Math.ceil(at.minutes / STEP) * STEP);
      setDrag({ ...current, to: Math.min(1440, to), moved });
      return;
    }
    const at = locate(e.clientX, e.clientY);
    if (!at) return;
    const delta = snap(at.minutes - current.originMinutes, STEP);
    if (current.kind === "move") setDrag({ ...current, dayShift: at.day - current.originDay, startShift: delta, endShift: delta, moved });
    else setDrag({ ...current, endShift: delta, moved });
  }

  function onPointerUp(e: React.PointerEvent) {
    const current = dragRef.current;
    setDrag(null);
    if (!current) return;
    if (current.kind === "create") {
      const day = days[current.day];
      if (!current.moved) {
        // A click or a tap: a slot of the default length at the half hour clicked.
        const start = Math.floor(current.from / 30) * 30;
        props.onSelect({ day, start, end: Math.min(1440, start + props.defaultDuration) }, slotRect(current.day, start, start + props.defaultDuration));
      } else {
        props.onSelect({ day, start: current.from, end: current.to }, slotRect(current.day, current.from, current.to));
      }
      return;
    }
    if (!current.moved) return;
    suppressClick.current = true;
    window.setTimeout(() => { suppressClick.current = false; }, 0);
    const result = shifted(current);
    if (result) props.onMove(current.item, result.startDay, result.start, result.endDay, result.end);
    void e;
  }

  /** Where a moved or resized item lands, on the visible days. */
  function shifted(current: Extract<Drag, { kind: "move" | "resize" }>): { startDay: Day; start: number; endDay: Day; end: number } | null {
    const item = current.item;
    const first = columnsData.find((c) => c.placed.has(item.key));
    if (!first) return null;
    const piece = first.pieces.find((p) => p.item.key === item.key)!.piece;
    const dayIndex = days.indexOf(first.day);
    const length = Math.round((item.end - item.start) / 60000);
    if (current.kind === "move") {
      const target = Math.min(days.length - 1, Math.max(0, dayIndex + current.dayShift));
      const start = Math.min(1440 - STEP, Math.max(0, piece.start + current.startShift));
      if (current.startShift === 0 && current.dayShift === 0) return null;
      return { startDay: days[target], start, endDay: days[target], end: start + length };
    }
    const end = Math.max(piece.start + STEP, Math.min(1440, piece.end + current.endShift));
    if (end === piece.end) return null;
    return { startDay: first.day, start: piece.start, endDay: first.day, end };
  }

  function slotRect(dayIndex: number, start: number, end: number): DOMRect | null {
    const box = columns.current?.getBoundingClientRect();
    if (!box) return null;
    const width = box.width / days.length;
    return new DOMRect(box.left + dayIndex * width, box.top + (start / 60) * H, width, ((end - start) / 60) * H);
  }

  function onDragOver(e: React.DragEvent) {
    if (!props.dragTask) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    const at = locate(e.clientX, e.clientY);
    if (at) setDropAt({ day: at.day, minutes: Math.min(1440 - STEP, Math.floor(at.minutes / STEP) * STEP) });
  }

  function onDrop(e: React.DragEvent) {
    const task = props.dragTask;
    setDropAt(null);
    if (!task) return;
    e.preventDefault();
    const at = locate(e.clientX, e.clientY);
    if (at) props.onDropTask(task, days[at.day], Math.min(1440 - STEP, Math.floor(at.minutes / STEP) * STEP));
  }

  const style = { "--days": days.length, "--hour": `${H}px`, "--work-start": `${(props.workStart / 1440) * 100}%`, "--work-end": `${(props.workEnd / 1440) * 100}%` } as CSSProperties;
  const nowTop = (props.nowMinutes / 60) * H;
  const dropLength = props.dragTask?.duration || 30;

  return (
    <div className={`cal-tg ${phone ? "phone" : ""} ${drag && drag.kind !== "create" && drag.moved ? "dragging" : ""}`} style={style}>
      <div ref={scroller} className="cal-tg-scroll">
        <div className="cal-tg-sticky">
          {/* A phone's single day has the week strip above it saying which day this is. */}
          <div className={`cal-tg-heads ${phone && days.length === 1 ? "hidden" : ""}`}>
            <div className="cal-tg-gutter cal-tg-zone" aria-hidden="true" />
            {days.map((day) => {
              const isToday = day === today;
              return (
                <button key={day} type="button" className={`cal-tg-head ${isToday ? "today" : ""} ${day < today ? "past" : ""}`} onClick={() => props.onPickDay(day)} aria-label={dayLabel(day, locale(), { weekday: "long", day: "numeric", month: "long" })} aria-current={isToday ? "date" : undefined}>
                  <span className="cal-tg-wd">{dayLabel(day, locale(), { weekday: "short" })}</span>
                  <span className="cal-tg-num">{Number(day.slice(8, 10))}</span>
                </button>
              );
            })}
          </div>
          <div className="cal-tg-allday" style={{ "--lanes": Math.max(1, shownLanes + (collapsed ? 1 : 0)) } as CSSProperties}
            onDragOver={(e) => { if (props.dragTask) { e.preventDefault(); } }}
            onDrop={(e) => {
              const task = props.dragTask;
              if (!task) return;
              e.preventDefault();
              const box = (e.currentTarget.querySelector(".cal-tg-allday-cells") as HTMLElement | null)?.getBoundingClientRect();
              if (!box) return;
              const index = Math.floor(((e.clientX - box.left) / box.width) * days.length);
              if (index >= 0 && index < days.length) props.onDropTask(task, days[index], null);
            }}>
            <div className="cal-tg-gutter cal-tg-allday-label">{laneCount > cap ? (
              <button type="button" className="cal-allday-toggle" onClick={() => setAllDayOpen((o) => !o)} aria-expanded={!collapsed} aria-label={collapsed ? t("cal.allday.more") : t("cal.allday.less")} title={collapsed ? t("cal.allday.more") : t("cal.allday.less")}>
                <Icon name="chevron" size={14} />
              </button>
            ) : <span>{t("cal.allday.short")}</span>}</div>
            <div className="cal-tg-allday-cells">
              {days.map((day, index) => (
                <button key={day} type="button" className="cal-tg-allday-cell" style={{ gridColumn: index + 1 }} tabIndex={-1} aria-label={t("cal.new.allday.on", { day: dayLabel(day, locale(), { day: "numeric", month: "long" }) })} onClick={() => props.onAllDay(day)} />
              ))}
              {bars.filter((b) => b.lane < shownLanes).map((bar) => (
                <ItemChip key={bar.item.key} item={bar.item} zone={zone} className={`cal-bar ${bar.before ? "cut-start" : ""} ${bar.after ? "cut-end" : ""}`}
                  style={{ gridColumn: `${bar.span.first + 1} / ${bar.span.last + 2}`, gridRow: bar.lane + 1 }}
                  onOpen={props.onOpen} onToggleTask={props.onToggleTask} />
              ))}
              {collapsed && days.map((day, index) => {
                const hidden = bars.filter((b) => b.lane >= shownLanes && index >= b.span.first && index <= b.span.last).length;
                return hidden ? <button key={`more-${day}`} type="button" className="cal-more" style={{ gridColumn: index + 1, gridRow: shownLanes + 1 }} onClick={() => setAllDayOpen(true)}>{t("cal.more", { n: hidden })}</button> : null;
              })}
            </div>
          </div>
        </div>
        <div className="cal-tg-body" style={{ height: 24 * H }}>
          <div className="cal-tg-gutter cal-tg-hours" aria-hidden="true">
            {Array.from({ length: 23 }, (_, i) => <span key={i} style={{ top: (i + 1) * H }}>{clockLabel((i + 1) * 60, locale())}</span>)}
          </div>
          <div ref={columns} className="cal-tg-cols" onPointerDown={onBodyPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={() => setDrag(null)} onDragOver={onDragOver} onDragLeave={() => setDropAt(null)} onDrop={onDrop}>
            {columnsData.map(({ day, pieces, placed }, index) => (
              <div key={day} className={`cal-tg-col ${day === today ? "today" : ""} ${isWeekendDay(day) ? "weekend" : ""}`} data-day={day} role="group" aria-label={dayLabel(day, locale(), { weekday: "long", day: "numeric", month: "long" })}>
                {pieces.map(({ item, piece }) => {
                  const at = placed.get(item.key)!;
                  let top = piece.start;
                  let bottom = piece.end;
                  let hidden = false;
                  if (drag && drag.kind !== "create" && drag.moved && drag.item.key === item.key) {
                    if (drag.kind === "move") hidden = true;
                    else bottom = Math.max(top + STEP, Math.min(1440, bottom + drag.endShift));
                  }
                  const height = Math.max(((bottom - top) / 60) * H, 18);
                  const blockStyle = { top: (top / 60) * H, height, left: `calc(${at.left * 100}% + 1px)`, width: `calc(${at.width * 100}% - ${at.left + at.width >= 0.999 ? 6 : 2}px)`, zIndex: 1 + at.column, "--c": item.color } as CSSProperties;
                  return (
                    <Block key={item.key} item={item} zone={zone} day={day} style={blockStyle} height={height} past={item.end < props.now} ghost={hidden}
                      continuesBefore={piece.continuesBefore} continuesAfter={piece.continuesAfter}
                      onPointerDown={(e, resize) => onBlockPointerDown(e, item, resize)}
                      onOpen={() => { if (!suppressClick.current) props.onOpen(item); }}
                      onToggleTask={props.onToggleTask} resizable={!phone && movable(item)} />
                  );
                })}
                {drag && drag.kind === "move" && drag.moved && (() => {
                  const result = shifted(drag);
                  if (!result || result.startDay !== day) return null;
                  const height = Math.max(((result.end - result.start) / 60) * H, 18);
                  return <div className="cal-block preview" style={{ top: (result.start / 60) * H, height, left: 1, right: 6, "--c": drag.item.color } as CSSProperties}><span className="cal-block-title">{drag.item.title}</span><span className="cal-block-time">{clockLabel(result.start, locale())} – {clockLabel(result.end, locale())}</span></div>;
                })()}
                {drag && drag.kind === "create" && drag.day === index && (drag.moved || drag.to > drag.from) && drag.y >= 0 && (
                  <div className="cal-selection" style={{ top: (drag.from / 60) * H, height: ((drag.to - drag.from) / 60) * H }}>
                    <span>{clockLabel(drag.from, locale())} – {clockLabel(drag.to, locale())}</span>
                  </div>
                )}
                {!drag && props.selection && props.selection.day === day && (
                  <div className="cal-selection" style={{ top: (props.selection.start / 60) * H, height: ((props.selection.end - props.selection.start) / 60) * H }}>
                    <span>{clockLabel(props.selection.start, locale())} – {clockLabel(props.selection.end, locale())}</span>
                  </div>
                )}
                {dropAt && dropAt.day === index && (
                  <div className="cal-selection drop" style={{ top: (dropAt.minutes / 60) * H, height: (dropLength / 60) * H }}>
                    <span>{props.dragTask?.title} · {clockLabel(dropAt.minutes, locale())}</span>
                  </div>
                )}
                {day === today && <div className="cal-now" style={{ top: nowTop }} role="presentation"><span className="cal-now-dot" /></div>}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function isWeekendDay(day: Day): boolean {
  const n = new Date(`${day}T00:00:00Z`).getUTCDay();
  return n === 0 || n === 6;
}

/** Whether the reader may drag an item: their own events and time-blocked tasks, never a read-only
 *  subscription's, and never a piece of an event that crosses midnight (it would be moved by half). */
function movable(item: Item): boolean {
  if (item.kind === "task") return !item.done;
  return !!item.event?.writable && item.firstDay === item.lastDay;
}

function Block({ item, zone, day, style, height, past, ghost, continuesBefore, continuesAfter, onPointerDown, onOpen, onToggleTask, resizable }: {
  item: Item; zone: string; day: Day; style: CSSProperties; height: number; past: boolean; ghost: boolean; continuesBefore: boolean; continuesAfter: boolean;
  onPointerDown: (e: React.PointerEvent<HTMLElement>, resize: boolean) => void; onOpen: () => void; onToggleTask: (task: Task) => void; resizable: boolean;
}) {
  const time = itemTime(item, zone, day);
  const short = height < 34;
  const task = item.task;
  const label = `${item.title || t("cal.untitled")}, ${time}${item.location ? `, ${item.location}` : ""}`;
  return (
    <div className={`cal-block ${item.kind} ${short ? "short" : ""} ${past ? "past" : ""} ${ghost ? "ghost" : ""} ${item.done ? "done" : ""} ${item.event?.pending_sync ? "pending" : ""} ${continuesBefore ? "cut-start" : ""} ${continuesAfter ? "cut-end" : ""}`}
      style={style} data-key={item.key} onPointerDown={(e) => onPointerDown(e, false)} onClick={onOpen}>
      {/* The click is also read on the block itself: once a mouse press is captured for a possible
          drag, the browser sends the click to the block rather than to this button. */}
      <button type="button" className="cal-block-open" onClick={(e) => { e.stopPropagation(); onOpen(); }} aria-label={label} title={label} />
      {task && (
        <button type="button" className={`cal-check ${item.done ? "on" : ""}`} role="checkbox" aria-checked={!!item.done} aria-label={t("cal.task.complete.named", { title: item.title })} onClick={(e) => { e.stopPropagation(); onToggleTask(task); }}>
          {item.done && <Icon name="check" size={12} />}
        </button>
      )}
      <span className="cal-block-text">
        {/* As many lines of title as the block has room for under its time, never a half-cut line. */}
        <span className="cal-block-title" style={short ? undefined : { WebkitLineClamp: Math.max(1, Math.floor((height - 8) / 15) - 1) }}>{item.title || t("cal.untitled")}{short && <span className="cal-block-time inline">{time.split(" – ")[0]}</span>}</span>
        {!short && <span className="cal-block-time">{time}</span>}
        {!short && height > 64 && item.location && <span className="cal-block-place">{item.location}</span>}
      </span>
      {item.event?.recurring && height > 40 && <span className="cal-block-icon" aria-hidden="true"><Icon name="loop" size={12} /></span>}
      {item.event?.conflict && <span className="cal-block-icon warn" aria-hidden="true"><Icon name="alert" size={12} /></span>}
      {resizable && <span className="cal-resize" onPointerDown={(e) => { e.stopPropagation(); onPointerDown(e, true); }} aria-hidden="true" />}
    </div>
  );
}

/** One line for an item in the all-day row, a month cell or a list: its colour, a checkbox for a
 *  task, its title. */
export function ItemChip({ item, zone, className = "", style, onOpen, onToggleTask, showTime }: { item: Item; zone: string; className?: string; style?: CSSProperties; onOpen: (item: Item) => void; onToggleTask: (task: Task) => void; showTime?: boolean }) {
  const task = item.task;
  const time = showTime && !item.allDay ? itemTime(item, zone).split(" – ")[0] : "";
  const label = `${item.title || t("cal.untitled")}${item.allDay ? "" : `, ${itemTime(item, zone)}`}`;
  return (
    <div className={`cal-chip ${item.kind} ${item.allDay ? "solid" : "timed"} ${item.done ? "done" : ""} ${className}`} style={{ ...style, "--c": item.color } as CSSProperties}>
      {task && (
        <button type="button" className={`cal-check ${item.done ? "on" : ""}`} role="checkbox" aria-checked={!!item.done} aria-label={t("cal.task.complete.named", { title: item.title })} onClick={(e) => { e.stopPropagation(); onToggleTask(task); }}>
          {item.done && <Icon name="check" size={10} />}
        </button>
      )}
      <button type="button" className="cal-chip-open" onClick={(e) => { e.stopPropagation(); onOpen(item); }} aria-label={label} title={label}>
        {!item.allDay && !task && <span className="cal-chip-dot" aria-hidden="true" />}
        {time && <span className="cal-chip-time">{time}</span>}
        <span className="cal-chip-title">{item.title || t("cal.untitled")}</span>
      </button>
    </div>
  );
}
