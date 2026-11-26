// Making something in the slot just picked: a small card beside the selection on a desktop, a sheet
// from the bottom below 1024 px (the Popover primitive decides). A title and Enter make an event (or a
// time-blocked task); More options carries what was typed into the full editor.

import type { CSSProperties } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { Button, Popover, Segmented, Select, type Anchor } from "../ui/primitives";
import { dayLabel, type Day } from "./dates";
import { TimeSelect } from "./fields";
import type { CalendarRow } from "./types";
import { calendarName } from "./words";

export type QuickDraft = { kind: "event" | "task"; title: string; day: Day; start: number; end: number; allDay: boolean; calendar_id: string };

type Props = {
  draft: QuickDraft;
  calendars: CalendarRow[];
  /** The selected slot, or the button that was pressed. */
  anchor: Anchor | null;
  /** Which side of the slot the card stands on: the one with room. */
  side: "right" | "left" | "bottom";
  busy: boolean;
  onChange: (draft: QuickDraft) => void;
  onSave: (draft: QuickDraft) => void;
  onMore: (draft: QuickDraft) => void;
  onClose: () => void;
};

export const QUICK_WIDTH = 360;

export function QuickCreate(props: Props) {
  const label = t(props.draft.kind === "task" ? "cal.new.task" : "cal.new");
  return (
    <Popover anchor={props.anchor} onClose={props.onClose} label={label} role="dialog" width={QUICK_WIDTH} side={props.side} align={props.side === "bottom" ? "end" : "start"} offset={10} className="cal-quick">
      <QuickForm {...props} />
    </Popover>
  );
}

function QuickForm({ draft, calendars, busy, onChange, onSave, onMore }: Props) {
  const set = (patch: Partial<QuickDraft>) => onChange({ ...draft, ...patch });
  const writable = calendars.filter((c) => c.writable);
  const calendar = calendars.find((c) => c.id === draft.calendar_id);
  return (
    <form className="cal-quick-form" onSubmit={(e) => { e.preventDefault(); if (draft.title.trim()) onSave(draft); }}>
      {/* The title comes first in the card: the popover puts the keyboard on the first field it finds. */}
      <input className="field cal-title-input" autoFocus value={draft.title} maxLength={240} onChange={(e) => set({ title: e.target.value })} placeholder={t(draft.kind === "task" ? "cal.task.placeholder" : "cal.field.title")} aria-label={t("cal.field.title")} />
      <Segmented className="cal-quick-kind" label={t("cal.quick.kind")} value={draft.kind} onChange={(kind) => set({ kind })}
        options={(["event", "task"] as const).map((kind) => ({ id: kind, label: t(`cal.quick.${kind}`) }))} />
      <div className="cal-quick-when">
        <Icon name="clock" size={16} />
        <span className="cal-quick-day">{dayLabel(draft.day, locale(), { weekday: "short", day: "numeric", month: "short" })}</span>
        {draft.allDay && <span className="cal-quick-allday">{t("cal.allday")}</span>}
      </div>
      {!draft.allDay && (
        <div className="cal-quick-times">
          <TimeSelect value={draft.start} onChange={(m) => set({ start: m, end: Math.min(1440, m + (draft.end - draft.start)) })} label={t("cal.field.start.time")} />
          <span className="cal-when-dash" aria-hidden="true">–</span>
          <TimeSelect value={draft.end} after={draft.start} onChange={(m) => set({ end: m })} label={t("cal.field.end.time")} />
        </div>
      )}
      {draft.kind === "event" && writable.length > 1 && (
        <div className="cal-quick-cal" style={{ "--c": calendar?.color ?? "var(--accent)" } as CSSProperties}>
          <span className="cal-dot" aria-hidden="true" />
          <Select size="md" value={draft.calendar_id} label={t("cal.field.calendar")} onChange={(id) => set({ calendar_id: id })}
            options={writable.map((c) => ({ id: c.id, label: calendarName(c) }))} />
        </div>
      )}
      <div className="cal-quick-actions">
        <Button variant="quiet" onClick={() => onMore(draft)}>{t("cal.more.options")}</Button>
        <Button variant="primary" type="submit" disabled={busy || !draft.title.trim()}>{t("common.save")}</Button>
      </div>
    </form>
  );
}
