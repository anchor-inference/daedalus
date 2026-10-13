// Groups: the events of one plan held together — a week's menu and its two orders, a trip, a course.
// The side column lists them with a switch each, the way it lists calendars; a group's page shows its
// events day by day with the days' totals and how far the plan has got, and moves, copies, archives,
// hides or deletes the whole plan at once.

import { useState, type CSSProperties } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { invalidate, prime, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { OverflowMenu, Sheet } from "../ui/dialogs";
import { AgendaView } from "./Agenda";
import { groupDays, type AggregateDay, type EventStatus, type Group, type TypeDef } from "./blocks";
import { addDays, dayLabel, type Day } from "./dates";
import { GROUPS, aggregatesKey, deleteGroup, duplicateGroup, eventsKey, patchGroup, refreshPlanner, shiftGroup } from "./data";
import { eventItem } from "./items";
import { MarkdownView } from "./BlockView";
import { ProgressPill, TypeIcon } from "./structured";
import type { CalendarRow, Item, Occurrence, Task } from "./types";

export function GroupsSection({ groups, onOpen, toast }: { groups: Group[]; onOpen: (id: string) => void; toast: (text: string) => void }) {
  const [archived, setArchived] = useState(false);
  const live = groups.filter((g) => !g.archived);
  const old = groups.filter((g) => g.archived);
  if (!groups.length) return null;

  async function toggle(group: Group) {
    prime(GROUPS, { groups: groups.map((g) => (g.id === group.id ? { ...g, hidden: !g.hidden } : g)) });
    try { await patchGroup(group.id, { hidden: !group.hidden }); }
    catch (exc) { toast(errorText(exc)); }
    refreshPlanner();
  }

  const row = (group: Group) => (
    <li key={group.id} className={`cal-cal cal-grp ${group.hidden ? "hidden" : ""}`} style={{ "--c": group.color ?? "var(--accent)" } as CSSProperties}>
      <label className="cal-cal-toggle cal-grp-toggle">
        <input type="checkbox" checked={!group.hidden} onChange={() => void toggle(group)} aria-label={t("cal.group.show", { name: group.name })} />
        <span className="cal-cal-box" aria-hidden="true">{!group.hidden && <Icon name="check" size={11} />}</span>
      </label>
      <button type="button" className="cal-grp-open" onClick={() => onOpen(group.id)} title={t("cal.group.open", { name: group.name })}>
        <TypeIcon icon={group.icon} color={group.color} size={13} />
        <span className="cal-cal-name">{group.name}</span>
        {group.progress && group.progress.total > 0 && <span className="cal-grp-count">{group.progress.done}/{group.progress.total}</span>}
      </button>
    </li>
  );

  return (
    <section className="cal-side-section" aria-label={t("cal.groups")}>
      <div className="cal-side-head"><span>{t("cal.groups")}</span></div>
      <ul className="cal-cals">{live.map(row)}</ul>
      {old.length > 0 && (
        <>
          <button type="button" className="btn small ghost cal-grp-archived" aria-expanded={archived} onClick={() => setArchived(!archived)}>
            <Icon name="archive" size={13} /> {t("cal.groups.archived", { n: old.length })}
          </button>
          {archived && <ul className="cal-cals">{old.map(row)}</ul>}
        </>
      )}
    </section>
  );
}

type PageProps = {
  groupId: string;
  groups: Group[];
  types: TypeDef[];
  calendars: CalendarRow[];
  zone: string;
  today: Day;
  now: number;
  phone: boolean;
  dense: boolean;
  onClose: () => void;
  onOpen: (item: Item) => void;
  onStatus: (item: Item, status: EventStatus) => void;
  toast: (text: string) => void;
};

export function GroupPage({ groupId, groups, types, calendars, zone, today, now, phone, dense, onClose, onOpen, onStatus, toast }: PageProps) {
  const listed = groups.find((g) => g.id === groupId);
  const read = useQuery<Group>(`${GROUPS}/${encodeURIComponent(groupId)}`, { staleMs: 5000 });
  const group = read.data ?? listed;
  const range = groupDays(group?.range, zone);
  const from = range?.from ?? today;
  const to = range ? addDays(range.to, 1) : addDays(today, 7);
  const days: Day[] = [];
  for (let d = from; d < to && days.length < 92; d = addDays(d, 1)) days.push(d);
  const events = useQuery<Occurrence[]>(group ? `${eventsKey(from, to, zone)}&group_id=${encodeURIComponent(groupId)}` : null, { pollMs: 30000, staleMs: 5000 });
  const totals = useQuery<{ days: AggregateDay[] }>(group ? aggregatesKey(from, to, groupId) : null, { staleMs: 5000 });
  const aggregates = new Map((totals.data?.days ?? []).map((d) => [d.date, d.items]));
  const items = (events.data ?? []).map((o) => eventItem(o, zone, o.type ? types.find((x) => x.id === o.type) : undefined));
  const [shiftBy, setShiftBy] = useState(1);
  const [busy, setBusy] = useState(false);

  async function act(run: () => Promise<unknown>, done: string) {
    setBusy(true);
    try {
      await run();
      invalidate(GROUPS);
      refreshPlanner();
      toast(done);
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  async function remove() {
    if (!group) return;
    const withEvents = await confirmAsync(t("cal.group.delete.title", { name: group.name }), { body: t("cal.group.delete.body", { n: group.event_count ?? items.length }), action: t("cal.group.delete.events"), danger: true });
    if (withEvents) { await act(() => deleteGroup(group.id, "delete"), t("cal.group.deleted")); onClose(); }
  }

  async function ungroup() {
    if (!group) return;
    if (!(await confirmAsync(t("cal.group.ungroup.title", { name: group.name }), { body: t("cal.group.ungroup.body"), action: t("cal.group.ungroup") }))) return;
    await act(() => deleteGroup(group.id, "keep"), t("cal.group.ungrouped"));
    onClose();
  }

  if (!group) {
    return <Sheet title={t("cal.group")} onClose={onClose} size={phone ? "full" : "wide"}><div className="cal-blk-empty">{read.error ? t("cal.group.missing") : t("common.loading")}</div></Sheet>;
  }

  const rangeText = range ? `${dayLabel(range.from, locale(), { day: "numeric", month: "short" })} – ${dayLabel(range.to, locale(), { day: "numeric", month: "short" })}` : "";
  const head = (
    <div className="cal-view-head" style={{ "--c": group.color ?? "var(--accent)" } as CSSProperties}>
      <TypeIcon icon={group.icon} color={group.color} size={18} className="cal-view-icon" />
      <div className="cal-view-titles">
        <h3 className="cal-view-title">{group.name}</h3>
        <div className="cal-view-when">
          {rangeText && <span>{rangeText}</span>}
          <span>{t("cal.group.events", { n: group.event_count ?? items.length })}</span>
          <ProgressPill progress={group.progress} />
          {group.hidden && <span className="cal-tag">{t("cal.group.hidden")}</span>}
          {group.archived && <span className="cal-tag">{t("cal.group.archived")}</span>}
        </div>
      </div>
      <OverflowMenu label={t("cal.group.menu")} items={[
        { label: t("cal.group.duplicate.week"), icon: "copy", disabled: busy, onSelect: () => void act(() => duplicateGroup(group.id, 7), t("cal.group.duplicated")) },
        { label: group.hidden ? t("cal.group.unhide") : t("cal.group.hide"), icon: group.hidden ? "eye" : "eyeoff", disabled: busy, onSelect: () => void act(() => patchGroup(group.id, { hidden: !group.hidden }), group.hidden ? t("cal.group.shown") : t("cal.group.hidden.done")) },
        { label: group.archived ? t("cal.group.unarchive") : t("cal.group.archive"), icon: "archive", disabled: busy, onSelect: () => void act(() => patchGroup(group.id, { archived: !group.archived }), group.archived ? t("cal.group.unarchived") : t("cal.group.archived.done")) },
        "-",
        { label: t("cal.group.ungroup"), icon: "unlink", disabled: busy, onSelect: () => void ungroup() },
        { label: t("cal.group.delete"), icon: "trash", danger: true, disabled: busy, onSelect: () => void remove() },
      ]} />
    </div>
  );

  return (
    <Sheet ariaLabel={group.name} onClose={onClose} className={`cal-view cal-group-page ${phone ? "phone" : ""}`} size={phone ? "full" : "wide"} head={head}>
      <div className="cal-view-body">
        {group.description && <MarkdownView text={group.description} className="cal-view-desc" />}
        <div className="cal-bulk" role="group" aria-label={t("cal.group.bulk")}>
          <span className="cal-bulk-label">{t("cal.group.shift")}</span>
          <div className="cal-bulk-shift">
            <button type="button" className="iconbtn small" onClick={() => setShiftBy((n) => Math.max(-60, n - 1 === 0 ? -1 : n - 1))} aria-label={t("cal.group.shift.less")}><Icon name="back" size={14} /></button>
            <output className="cal-bulk-n" aria-live="polite">{shiftBy > 0 ? `+${shiftBy}` : shiftBy}</output>
            <button type="button" className="iconbtn small" onClick={() => setShiftBy((n) => Math.min(60, n + 1 === 0 ? 1 : n + 1))} aria-label={t("cal.group.shift.more")}><Icon name="forward" size={14} /></button>
            <span className="cal-bulk-unit">{t("cal.group.days")}</span>
          </div>
          <button type="button" className="btn small" disabled={busy} onClick={() => void act(() => shiftGroup(group.id, shiftBy), t("cal.group.shifted", { n: shiftBy }))}>{t("cal.group.shift.do")}</button>
          <button type="button" className="btn small" disabled={busy} onClick={() => void act(() => duplicateGroup(group.id, 7), t("cal.group.duplicated"))}><Icon name="copy" size={14} /> {t("cal.group.duplicate.week")}</button>
        </div>
        {events.data ? (
          <AgendaView days={days} items={items} zone={zone} today={today} now={now} calendars={calendars} types={types} aggregates={aggregates} dense={dense} embedded
            onOpen={onOpen} onToggleTask={(_task: Task) => undefined} onStatus={onStatus} onCreate={onClose} />
        ) : <div className="cal-view-loading" role="progressbar" aria-label={t("common.loading")} />}
      </div>
    </Sheet>
  );
}
