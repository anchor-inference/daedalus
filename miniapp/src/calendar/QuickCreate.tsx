// Making something in the slot just picked: a small card beside the selection on a desktop, a sheet
// from the bottom on a phone. A title and Enter make an event (or a time-blocked task); More options
// carries what was typed into the full editor.

import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { Sheet, useLayer } from "../ui/dialogs";
import { dayLabel, type Day } from "./dates";
import { TimeSelect } from "./fields";
import type { CalendarRow } from "./types";
import { calendarName } from "./words";

export type QuickDraft = { kind: "event" | "task"; title: string; day: Day; start: number; end: number; allDay: boolean; calendar_id: string };

type Props = {
  draft: QuickDraft;
  calendars: CalendarRow[];
  anchor: DOMRect | null;
  phone: boolean;
  busy: boolean;
  onChange: (draft: QuickDraft) => void;
  onSave: (draft: QuickDraft) => void;
  onMore: (draft: QuickDraft) => void;
  onClose: () => void;
};

export function QuickCreate(props: Props) {
  if (props.phone || !props.anchor) {
    return (
      <Sheet title={t(props.draft.kind === "task" ? "cal.new.task" : "cal.new")} onClose={props.onClose} size="narrow" className="cal-quick-sheet">
        <QuickForm {...props} />
      </Sheet>
    );
  }
  return <QuickPopover {...props} />;
}

function QuickPopover(props: Props) {
  const box = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  useLayer(props.onClose);
  // Beside the selection, on whichever side has room, and never past the window's edges.
  useLayoutEffect(() => {
    const r = props.anchor!;
    const width = box.current?.offsetWidth ?? 340;
    const height = box.current?.offsetHeight ?? 260;
    const right = r.right + 10;
    const left = right + width < window.innerWidth - 8 ? right : Math.max(8, r.left - width - 10);
    const top = Math.min(Math.max(8, r.top - 20), window.innerHeight - height - 8);
    setPos({ left, top });
  }, [props.anchor]);
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (box.current?.contains(e.target as Node)) return;
      // A press on the grid starts a new selection; this card gives way to it.
      props.onClose();
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [props.onClose]); // eslint-disable-line react-hooks/exhaustive-deps
  return createPortal(
    <div ref={box} className="cal-quick" role="dialog" aria-modal="false" aria-label={t(props.draft.kind === "task" ? "cal.new.task" : "cal.new")} style={{ left: pos?.left ?? 0, top: pos?.top ?? 0, visibility: pos ? "visible" : "hidden" } as CSSProperties}>
      <div className="cal-quick-head">
        <span>{t(props.draft.kind === "task" ? "cal.new.task" : "cal.new")}</span>
        <button type="button" className="iconbtn small" onClick={props.onClose} aria-label={t("common.close")} title={t("common.close")}><Icon name="close" size={14} /></button>
      </div>
      <QuickForm {...props} />
    </div>,
    document.body,
  );
}

function QuickForm({ draft, calendars, busy, onChange, onSave, onMore }: Props) {
  const set = (patch: Partial<QuickDraft>) => onChange({ ...draft, ...patch });
  const writable = calendars.filter((c) => c.writable);
  const calendar = calendars.find((c) => c.id === draft.calendar_id);
  return (
    <form className="cal-quick-form" onSubmit={(e) => { e.preventDefault(); if (draft.title.trim()) onSave(draft); }}>
      <div className="segmented inline cal-quick-kind" role="radiogroup" aria-label={t("cal.quick.kind")}>
        {(["event", "task"] as const).map((kind) => (
          <button key={kind} type="button" role="radio" aria-checked={draft.kind === kind} className={draft.kind === kind ? "on" : ""} onClick={() => set({ kind })}>{t(`cal.quick.${kind}`)}</button>
        ))}
      </div>
      <input className="field cal-title-input" autoFocus value={draft.title} maxLength={240} onChange={(e) => set({ title: e.target.value })} placeholder={t(draft.kind === "task" ? "cal.task.placeholder" : "cal.field.title")} aria-label={t("cal.field.title")} />
      <div className="cal-quick-when">
        <Icon name="clock" size={14} />
        <span className="cal-quick-day">{dayLabel(draft.day, locale(), { weekday: "short", day: "numeric", month: "short" })}</span>
        {draft.allDay ? <span className="cal-quick-allday">{t("cal.allday")}</span> : (
          <>
            <TimeSelect value={draft.start} onChange={(m) => set({ start: m, end: Math.min(1440, m + (draft.end - draft.start)) })} label={t("cal.field.start.time")} />
            <span className="cal-when-end">
              <span className="cal-when-dash" aria-hidden="true">–</span>
              <TimeSelect value={draft.end} after={draft.start} onChange={(m) => set({ end: m })} label={t("cal.field.end.time")} />
            </span>
          </>
        )}
      </div>
      {draft.kind === "event" && writable.length > 1 && (
        <label className="cal-quick-cal" style={{ "--c": calendar?.color ?? "var(--accent)" } as CSSProperties}>
          <span className="cal-dot" aria-hidden="true" />
          <select className="field" value={draft.calendar_id} onChange={(e) => set({ calendar_id: e.target.value })} aria-label={t("cal.field.calendar")}>
            {writable.map((c) => <option key={c.id} value={c.id}>{calendarName(c)}</option>)}
          </select>
        </label>
      )}
      <div className="cal-quick-actions">
        <button type="button" className="btn ghost" onClick={() => onMore(draft)}>{t("cal.more.options")}</button>
        <button type="submit" className="btn primary" disabled={busy || !draft.title.trim()}>{t("common.save")}</button>
      </div>
    </form>
  );
}
