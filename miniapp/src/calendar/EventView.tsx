// The event as it is read: what tapping an event opens, instead of the editor it used to open, where
// a list could only be read through a form it was easy to change by accident.
//
// The head stays put while the body scrolls: the type's icon, the title, the time, the status and the
// progress. Below it, the event's blocks in their order, each folding under its own head, ticked in
// place; the description as Markdown; and at the end the event's history. A phone gets the whole
// screen and one scroll; nothing inside the sheet scrolls on its own except a wide table sideways.

import { useRef, useState, type CSSProperties } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { prime, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { copyText } from "../ui/components";
import { OverflowMenu, Sheet, toast } from "../ui/dialogs";
import { BlockSection, MarkdownView, type BlockContext } from "./BlockView";
import { eventAsText, eventProgress, inverseCheck, isKnown, statusFlow, type ActionBlock, type BlockOp, type EventStatus, type Group, type HistoryEntry, type TypeDef } from "./blocks";
import { eventHistory, eventKey, refreshPlanner, runAction, setStatus } from "./data";
import { ProgressPill, TypeIcon, statusIcon, statusLabel, ticks, useCollapsed, useQueued, useWidth } from "./structured";
import type { CalendarRow, Occurrence } from "./types";
import { calendarName, itemTime, relativeDay, ruleText } from "./words";
import { eventItem } from "./items";
import { todayIn } from "./zone";

type Props = {
  occurrence: Occurrence;
  calendars: CalendarRow[];
  types: TypeDef[];
  groups: Group[];
  zone: string;
  phone: boolean;
  busy: boolean;
  onClose: () => void;
  onEdit: (event: Occurrence) => void;
  onDelete: (event: Occurrence) => void;
  onResolve: (event: Occurrence, choice: "local" | "remote") => void;
  onOpenGroup: (groupId: string) => void;
  onOpenEvent: (eventId: string) => void;
};

/** Below this width a table is drawn as cards: four columns of a basket do not fit a phone. */
const CARDS_BELOW = 560;

export function EventView({ occurrence, calendars, types, groups, zone, phone, busy, onClose, onEdit, onDelete, onResolve, onOpenGroup, onOpenEvent }: Props) {
  const full = useQuery<Occurrence>(eventKey(occurrence.event_id), { pollMs: 15000, staleMs: 3000 });
  useQueued();
  // The occurrence keeps its own times (one meeting of a weekly series), the read event brings the
  // body; until it arrives, the list's copy is drawn, which has everything but the blocks.
  // The read event is the stored row, not an occurrence: what it does not say (whether it can be
  // written, whether it repeats, its resolved colour) is kept from the list's copy.
  const event: Occurrence = full.data ? { ...occurrence, ...defined(full.data), id: occurrence.id, event_id: occurrence.event_id, start_at: occurrence.start_at, end_at: occurrence.end_at, start_date: occurrence.start_date, end_date: occurrence.end_date, occurrence_start: occurrence.occurrence_start, writable: occurrence.writable, color: occurrence.color } : occurrence;
  const blocks = ticks.overlay(occurrence.event_id, event.blocks ?? []);
  const loadingBody = !full.data && !full.error && (occurrence.block_summary?.length ?? 0) > 0;
  const type = event.type ? types.find((x) => x.id === event.type) : undefined;
  const group = event.group_id ? groups.find((g) => g.id === event.group_id) : undefined;
  const calendar = calendars.find((c) => c.id === event.calendar_id);
  const readOnly = !event.writable || calendar?.writable === false;
  const progress = eventProgress(blocks, type?.display?.badge) ?? (full.data ? null : occurrence.progress ?? null);
  const [isFolded, fold] = useCollapsed();
  const [box, setBox] = useState<HTMLDivElement | null>(null);
  const width = useWidth(box);
  const [history, setHistory] = useState<HistoryEntry[] | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [statusBusy, setStatusBusy] = useState(false);
  const lastQueuedNote = useRef(0);

  const item = eventItem(event, zone);
  const day = item.firstDay;
  const today = todayIn(zone);
  const when = `${relativeDay(day, today)}${item.allDay ? ` · ${t("cal.allday")}` : ` · ${itemTime(item, zone, day)}`}`;
  const accent = type?.color ?? event.color;

  function onOps(ops: BlockOp[], label: string) {
    if (readOnly) return;
    const single = ops.length === 1 && ops[0].op === "set_checked" ? ops[0] : null;
    void ticks.submit(occurrence.event_id, ops).then((how) => {
      if (how === "queued" && Date.now() - lastQueuedNote.current > 30000) {
        lastQueuedNote.current = Date.now();
        toast(t("cal.queued"));
      }
    }, (exc) => toast(errorText(exc)));
    if (single) {
      toast(single.checked ? t("cal.ticked", { what: label }) : t("cal.unticked", { what: label }), {
        undo: () => { void ticks.submit(occurrence.event_id, [inverseCheck(single)]).catch((exc) => toast(errorText(exc))); },
      });
    }
  }

  async function changeStatus(next: EventStatus) {
    const before = event.status ?? "planned";
    if (next === before) return;
    setStatusBusy(true);
    try {
      const saved = await setStatus(occurrence.event_id, next);
      if (saved && typeof saved === "object" && "id" in saved) prime(eventKey(occurrence.event_id), saved);
      refreshPlanner();
      toast(t("cal.status.changed", { status: statusLabel(next) }), {
        undo: () => { void setStatus(occurrence.event_id, before).then((back) => { if (back && typeof back === "object" && "id" in back) prime(eventKey(occurrence.event_id), back); refreshPlanner(); }, (exc) => toast(errorText(exc))); },
      });
    } catch (exc) { toast(errorText(exc)); }
    finally { setStatusBusy(false); }
  }

  async function onAction(block: ActionBlock) {
    if (block.action === "url") {
      if (block.url && /^https?:\/\//.test(block.url)) window.open(block.url, "_blank", "noopener,noreferrer");
      return;
    }
    // Nothing reaches an agent's session without the reader saying so: the prompt is shown first.
    if (!(await confirmAsync(t("cal.action.confirm"), { body: block.prompt ?? "", action: t("cal.action.send") }))) return;
    try {
      await runAction(occurrence.event_id, block.id);
      toast(t("cal.action.sent"));
    } catch (exc) { toast(errorText(exc)); }
  }

  async function copy(markdown: boolean) {
    const text = eventAsText({ title: event.title || t("cal.untitled"), when, description: event.description, fields: event.fields, blocks, status: event.status, tags: event.tags }, type, markdown);
    toast((await copyText(text)) ? t("cal.copied") : t("cal.copy.failed"));
  }

  async function toggleHistory() {
    const open = !historyOpen;
    setHistoryOpen(open);
    if (open) {
      try { setHistory((await eventHistory(occurrence.event_id)).entries ?? []); }
      catch (exc) { setHistory([]); toast(errorText(exc)); }
    }
  }

  const ctx: BlockContext = { type, fields: event.fields, readOnly, narrow: (width || (phone ? 360 : 640)) < CARDS_BELOW, onOps, onAction: (b) => void onAction(b), onOpenRef: onOpenEvent, types };
  const flow = statusFlow(type);
  const status = event.status ?? "planned";
  const description = event.description?.trim() ?? "";
  const meta = [calendar && calendarName(calendar), event.location, event.recurring && event.recurrence ? ruleText(event.recurrence) : ""].filter(Boolean);

  const head = (
    <div className="cal-view-head" style={{ "--c": accent } as CSSProperties}>
      <TypeIcon icon={type?.icon} color={accent} size={18} className="cal-view-icon" />
      <div className="cal-view-titles">
        <h3 className="cal-view-title">{event.title || t("cal.untitled")}</h3>
        <div className="cal-view-when">
          <span>{when}</span>
          {!readOnly ? (
            <OverflowMenu label={t("cal.status.change")} className={`cal-status s-${status}`} small
              trigger={<span className="cal-status-face"><Icon name={statusIcon(status)} size={12} />{statusLabel(status)}<Icon name="chevron" size={12} /></span>}
              items={flow.map((s) => ({ label: statusLabel(s), icon: s === status ? "check" as const : statusIcon(s), disabled: statusBusy, onSelect: () => void changeStatus(s) }))} />
          ) : status !== "planned" && <span className={`cal-status-face s-${status}`}><Icon name={statusIcon(status)} size={12} />{statusLabel(status)}</span>}
          <ProgressPill progress={progress} />
        </div>
      </div>
      {!readOnly && <button type="button" className="iconbtn cal-view-edit" onClick={() => onEdit(event)} aria-label={t("cal.edit")} title={t("cal.edit")} disabled={busy}><Icon name="pen" size={16} /></button>}
      <OverflowMenu label={t("cal.view.menu")} items={[
        { label: t("cal.copy.text"), icon: "copy", onSelect: () => void copy(false) },
        { label: t("cal.copy.markdown"), icon: "copy", onSelect: () => void copy(true) },
        { label: t("cal.history"), icon: "journal", onSelect: () => { if (!historyOpen) void toggleHistory(); window.setTimeout(() => box?.querySelector(".cal-history")?.scrollIntoView({ block: "start", behavior: "smooth" }), 60); } },
        ...(!readOnly ? ["-" as const, { label: t("common.delete"), icon: "trash" as const, danger: true, onSelect: () => onDelete(event) }] : []),
      ]} />
    </div>
  );

  return (
    <Sheet ariaLabel={event.title || t("cal.untitled")} onClose={onClose} className={`cal-view ${phone ? "phone" : ""}`} size={phone ? "full" : undefined} head={head}>
      <div ref={setBox} className="cal-view-body">
        {event.conflict && (
          <div className="cal-conflict" role="alert">
            <Icon name="alert" size={16} />
            <div>
              <b>{t("cal.conflict.title")}</b>
              <p>{t("cal.conflict.body")}</p>
              <div className="cal-conflict-actions">
                <button type="button" className="btn small" disabled={busy} onClick={() => onResolve(event, "local")}>{t("cal.conflict.local")}</button>
                <button type="button" className="btn small" disabled={busy} onClick={() => onResolve(event, "remote")}>{t("cal.conflict.remote.use")}</button>
              </div>
            </div>
          </div>
        )}
        {(group || event.tags?.length || meta.length > 0) && (
          <div className="cal-view-meta">
            {group && (
              <button type="button" className="cal-chipbtn group" style={{ "--c": group.color ?? "var(--accent)" } as CSSProperties} onClick={() => onOpenGroup(group.id)}>
                <TypeIcon icon={group.icon} color={group.color} size={13} />
                <span>{group.name}</span>
                <Icon name="forward" size={12} />
              </button>
            )}
            {event.tags?.map((tag) => <span key={tag} className="cal-tag">#{tag}</span>)}
            {meta.length > 0 && <span className="cal-view-where">{meta.join(" · ")}</span>}
          </div>
        )}
        {readOnly && <div className="cal-note"><Icon name="lock" size={14} /> {t("cal.readonly")}</div>}
        {(event.pending_sync || ticks.pendingFor(occurrence.event_id).length > 0) && (
          <div className="cal-view-pending"><Icon name="reload" size={12} /> {ticks.pendingFor(occurrence.event_id).length ? t("cal.queued.here") : t("cal.pending")}</div>
        )}
        {description && !blocks.length && <MarkdownView text={description} className="cal-view-desc" />}
        {loadingBody && <div className="cal-view-loading" role="progressbar" aria-label={t("common.loading")} />}
        {blocks.map((block) => (
          <BlockSection key={block.id} block={block} ctx={ctx} folded={isFolded(block.id, block.collapsed ?? !isKnown(block))} onFold={(f) => fold(block.id, f)} />
        ))}
        {description && blocks.length > 0 && <MarkdownView text={description} className="cal-view-desc" />}
        {!description && !blocks.length && !loadingBody && <p className="cal-blk-empty">{t("cal.view.empty")}</p>}
        <section className={`cal-history ${historyOpen ? "open" : ""}`}>
          <button type="button" className="cal-blk-head" aria-expanded={historyOpen} onClick={() => void toggleHistory()}>
            <Icon name="chevron" size={14} />
            <span className="cal-blk-title">{t("cal.history")}</span>
          </button>
          {historyOpen && (
            history === null ? <div className="cal-blk-empty">{t("common.loading")}</div>
              : history.length === 0 ? <div className="cal-blk-empty">{t("cal.history.empty")}</div>
              : (
                <ol className="cal-history-list">
                  {history.map((entry, i) => (
                    <li key={`${entry.at}-${i}`}>
                      <span className={`cal-history-who ${entry.actor.startsWith("agent") ? "agent" : ""}`}><Icon name={entry.actor.startsWith("agent") ? "bots" : "user"} size={12} />{entry.actor.startsWith("agent") ? t("cal.history.agent") : t("cal.history.you")}</span>
                      <span className="cal-history-what">{entry.summary}</span>
                      <time dateTime={entry.at}>{historyTime(entry.at)}</time>
                    </li>
                  ))}
                </ol>
              )
          )}
        </section>
      </div>
    </Sheet>
  );
}

function defined<T extends object>(value: T): Partial<T> {
  return Object.fromEntries(Object.entries(value).filter(([, v]) => v !== undefined && v !== null)) as Partial<T>;
}

function historyTime(at: string): string {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return at;
  return date.toLocaleString(locale(), { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}
