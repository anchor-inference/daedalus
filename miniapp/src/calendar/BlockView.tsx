// An event's blocks as the view sheet draws them: read, and ticked, without an editor. Each block is a
// section that folds under its head; a checklist's items and a table's checkbox cells are real
// checkboxes a thumb can hit; a table is cards on a narrow sheet and a table with its first column
// held on a wide one, its totals in a bar that stays at the bottom while the table is in view.

import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { locale, t } from "../i18n";
import { Icon } from "../icons";
import { renderMarkdown } from "../md";
import { RawHtml } from "../rawhtml";
import { useQuery } from "../store";
import { toast } from "../ui/dialogs";
import {
  blockProgress, cellText, checkboxColumn, columnTotal, isKnown, tableCards, typedFields,
  type ActionBlock, type Block, type BlockOp, type ChecklistBlock, type Column, type FieldsBlock, type LinkBlock, type MarkdownBlock, type RefBlock, type Row, type StepsBlock, type TableBlock, type TypeDef,
} from "./blocks";
import { dayLabel } from "./dates";
import { eventKey } from "./data";
import { ProgressPill, TypeIcon } from "./structured";
import type { Occurrence } from "./types";

export type BlockContext = {
  type?: TypeDef;
  fields?: Record<string, unknown>;
  readOnly: boolean;
  /** Cards rather than a table: the sheet is narrower than a table of this many columns needs. */
  narrow: boolean;
  onOps: (ops: BlockOp[], label: string) => void;
  onAction: (block: ActionBlock) => void;
  onOpenRef: (eventId: string) => void;
  types: TypeDef[];
};

export function blockTitle(block: Block): string {
  if (block.title) return block.title;
  return isKnown(block) ? t(`cal.block.kind.${block.kind}`) : t("cal.block.unsupported");
}

/** One block with its head: the title, its progress, and the fold. A markdown block or a link with no
 *  title of its own has no head to fold by, and is drawn as it is. */
export function BlockSection({ block, folded, onFold, ctx }: { block: Block; folded: boolean; onFold: (folded: boolean) => void; ctx: BlockContext }) {
  const progress = blockProgress(block);
  const headless = !block.title && (block.kind === "markdown" || block.kind === "link" || block.kind === "attachment" || block.kind === "action" || (block.kind === "fields" && (block as FieldsBlock).source === "fields"));
  const known = isKnown(block);
  if (headless && known) return <section className={`cal-blk kind-${block.kind} headless`} data-block={block.id}><BlockBody block={block} ctx={ctx} /></section>;
  const shut = folded;
  return (
    <section className={`cal-blk kind-${known ? block.kind : "unknown"} ${shut ? "folded" : ""}`} data-block={block.id} aria-label={blockTitle(block)}>
      <button type="button" className="cal-blk-head" aria-expanded={!shut} onClick={() => onFold(!shut)}>
        <Icon name="chevron" size={14} />
        <span className="cal-blk-title">{blockTitle(block)}</span>
        {!known && <span className="cal-blk-kind">{block.kind}</span>}
        {progress && progress.total > 0 && (block.kind !== "checklist" || (block as ChecklistBlock).show_progress !== false) && <ProgressPill progress={progress} />}
      </button>
      {!shut && <BlockBody block={block} ctx={ctx} />}
    </section>
  );
}

function BlockBody({ block, ctx }: { block: Block; ctx: BlockContext }) {
  switch (block.kind) {
    case "markdown": return <MarkdownView text={(block as MarkdownBlock).text ?? ""} />;
    case "checklist": return <ChecklistView block={block as ChecklistBlock} ctx={ctx} />;
    case "table": return <TableView block={block as TableBlock} ctx={ctx} />;
    case "fields": return <FieldsView block={block as FieldsBlock} ctx={ctx} />;
    case "steps": return <StepsView block={block as StepsBlock} ctx={ctx} />;
    case "link":
    case "attachment": return <LinkView block={block as LinkBlock} />;
    case "action": return <ActionView block={block as ActionBlock} ctx={ctx} />;
    case "ref": return <RefView block={block as RefBlock} ctx={ctx} />;
    default: return <UnknownView block={block} />;
  }
}

/** Markdown from an agent or the reader, drawn through the chat's renderer, which escapes every tag
 *  before it adds its own: no HTML and no script from an event's text ever reaches the page. */
export function MarkdownView({ text, className = "" }: { text: string; className?: string }) {
  const html = useMemo(() => renderMarkdown(text), [text]);
  if (!text.trim()) return null;
  return <RawHtml className={`summary-body cal-md ${className}`} html={html} />;
}

function Check({ on, label, onToggle, disabled, className = "" }: { on: boolean; label: string; onToggle: () => void; disabled: boolean; className?: string }) {
  return (
    <button type="button" role="checkbox" aria-checked={on} aria-label={label} disabled={disabled} className={`cal-box ${on ? "on" : ""} ${className}`} onClick={(e) => { e.stopPropagation(); onToggle(); }}>
      {on && <Icon name="check" size={14} />}
    </button>
  );
}

function ChecklistView({ block, ctx }: { block: ChecklistBlock; ctx: BlockContext }) {
  const items = block.items ?? [];
  if (!items.length) return <p className="cal-blk-empty">{t("cal.block.empty")}</p>;
  return (
    <ul className="cal-checklist">
      {items.map((item) => (
        // The whole row is the checkbox: a thumb aiming at a 22 px square in a list of fifty misses.
        <li key={item.id}>
          <button type="button" role="checkbox" aria-checked={!!item.checked} disabled={ctx.readOnly} className={`cal-ci ${item.checked ? "on" : ""}`}
            onClick={() => ctx.onOps([{ op: "set_checked", block_id: block.id, item_id: item.id, checked: !item.checked }], item.text)}>
            <span className={`cal-box ${item.checked ? "on" : ""}`} aria-hidden="true">{item.checked && <Icon name="check" size={14} />}</span>
            <span className="cal-ci-text">
              <span className="cal-ci-name">{item.text}</span>
              {item.note && <small>{item.note}</small>}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function TableView({ block, ctx }: { block: TableBlock; ctx: BlockContext }) {
  const columns = block.columns ?? [];
  const rows = block.rows ?? [];
  const check = checkboxColumn(block);
  const totals = columns.map((c) => ({ column: c, total: columnTotal(c, rows) })).filter((x) => x.total);
  const toggle = (row: Row, column: Column, title: string) => ctx.onOps([{ op: "set_checked", block_id: block.id, row_id: row.id, column: column.key, checked: row.cells?.[column.key] !== true }], title);
  const head = columns.find((c) => c.type === "text" || c.type === "link") ?? columns.find((c) => c !== check);
  const titleOf = (row: Row) => (head ? cellText(head, row.cells?.[head.key]) : row.id);
  if (!rows.length) return <p className="cal-blk-empty">{t("cal.block.empty")}</p>;
  return (
    <>
      {ctx.narrow ? (
        <ul className="cal-cards" aria-label={blockTitle(block)}>
          {tableCards(block).map((card, index) => {
            const row = rows[index];
            return (
              <li key={card.id} className={`cal-card ${card.checked ? "on" : ""}`}>
                {card.check ? (
                  <button type="button" role="checkbox" aria-checked={card.checked} disabled={ctx.readOnly} className="cal-card-hit" onClick={() => toggle(row, card.check!, card.title)}>
                    <span className={`cal-box ${card.checked ? "on" : ""}`} aria-hidden="true">{card.checked && <Icon name="check" size={14} />}</span>
                    <CardText title={card.title} lines={card.lines} />
                  </button>
                ) : (
                  <div className="cal-card-hit static"><CardText title={card.title} lines={card.lines} /></div>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <div className="cal-table-wrap">
          <table className="cal-table">
            {block.title && <caption className="cal-sr">{block.title}</caption>}
            <thead>
              <tr>{columns.map((c) => <th key={c.key} scope="col" className={`t-${c.type} a-${c.align ?? (c.type === "money" || c.type === "number" ? "right" : c.type === "checkbox" ? "center" : "left")}`}>{c.label}</th>)}</tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className={check && row.cells?.[check.key] === true ? "on" : ""}>
                  {columns.map((c, i) => {
                    const align = c.align ?? (c.type === "money" || c.type === "number" ? "right" : c.type === "checkbox" ? "center" : "left");
                    const value = row.cells?.[c.key];
                    const content = c.type === "checkbox"
                      ? <Check on={value === true} label={`${titleOf(row)}: ${c.label}`} disabled={ctx.readOnly} onToggle={() => toggle(row, c, titleOf(row))} />
                      : c.type === "link" && typeof value === "string" && /^https?:\/\//.test(value) ? <a href={value} target="_blank" rel="noreferrer">{value.replace(/^https?:\/\//, "")}</a>
                      : cellText(c, value);
                    return i === 0 ? <th key={c.key} scope="row" className={`t-${c.type} a-${align}`}>{content}</th> : <td key={c.key} className={`t-${c.type} a-${align}`}>{content}</td>;
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {totals.length > 0 && (
        <div className="cal-totals" role="status" aria-label={t("cal.block.total")}>
          <span className="cal-totals-label">{t("cal.block.total")}</span>
          {totals.map(({ column, total }) => (
            <span key={column.key} className={`cal-total t-${column.type}`}>
              {column.type === "checkbox" ? <><Icon name="check" size={12} />{total!.text}</> : <><small>{column.label}</small> {total!.text}</>}
            </span>
          ))}
        </div>
      )}
    </>
  );
}

function CardText({ title, lines }: { title: string; lines: { key: string; label: string; text: string }[] }) {
  return (
    <span className="cal-card-text">
      <span className="cal-card-title">{title || "—"}</span>
      {lines.length > 0 && (
        <span className="cal-card-lines">
          {lines.map((line) => <span key={line.key}><em>{line.label}</em> {line.text}</span>)}
        </span>
      )}
    </span>
  );
}

function FieldsView({ block, ctx }: { block: FieldsBlock; ctx: BlockContext }) {
  const items = block.source === "fields"
    ? typedFields(ctx.type, ctx.fields).map((f) => ({ key: f.key, label: f.label, text: f.text, stale: f.stale }))
    : (block.items ?? []).map((f, i) => ({ key: String(i), label: f.label, text: typeof f.value === "number" ? `${f.value.toLocaleString(locale(), { maximumFractionDigits: 2 })}${f.unit ? ` ${f.unit}` : ""}` : `${f.value ?? ""}${f.unit ? ` ${f.unit}` : ""}`, stale: false }));
  if (!items.length) return null;
  return (
    <dl className="cal-fields">
      {items.map((f) => (
        <div key={f.key} className={`cal-field ${f.stale ? "stale" : ""}`} title={f.stale ? t("cal.field.stale") : undefined}>
          <dt>{f.label}{f.stale && <span className="cal-stale">{t("cal.field.stale.short")}</span>}</dt>
          <dd>{f.text}</dd>
        </div>
      ))}
    </dl>
  );
}

// ── steps and their timers ───────────────────────────────────────────────────────────────────

const TIMERS = "daedalus.calendar.timers";
type Timer = { endsAt: number; label: string };
const timers = new Map<string, Timer>();
const timerListeners = new Set<() => void>();
const alarms = new Map<string, number>();

function saveTimers() {
  try { localStorage.setItem(TIMERS, JSON.stringify(Object.fromEntries(timers))); } catch { /* the timer lasts the visit */ }
  for (const fire of timerListeners) fire();
}

/** The timer rings on its own, wherever the reader is in the app: a notification when the browser
 *  allows one, a toast and a buzz always. Armed again after a reload from what was stored. */
function arm(id: string, timer: Timer) {
  window.clearTimeout(alarms.get(id));
  alarms.set(id, window.setTimeout(() => {
    timers.delete(id);
    alarms.delete(id);
    saveTimers();
    const text = t("cal.timer.done", { step: timer.label });
    toast(text, { ms: 10000 });
    try { navigator.vibrate?.([200, 100, 200]); } catch { /* no vibration here */ }
    try { if ("Notification" in window && Notification.permission === "granted") new Notification(t("cal.timer.title"), { body: timer.label }); } catch { /* a page without notifications */ }
  }, Math.max(0, timer.endsAt - Date.now())));
}

if (typeof window !== "undefined") {
  try {
    const stored = JSON.parse(localStorage.getItem(TIMERS) || "{}") as Record<string, Timer>;
    for (const [id, timer] of Object.entries(stored)) if (timer && timer.endsAt > Date.now() - 60000) { timers.set(id, timer); arm(id, timer); }
  } catch { /* nothing stored */ }
}

function startTimer(id: string, minutes: number, label: string) {
  const timer = { endsAt: Date.now() + minutes * 60000, label };
  timers.set(id, timer);
  arm(id, timer);
  saveTimers();
  try { if ("Notification" in window && Notification.permission === "default") void Notification.requestPermission(); } catch { /* asked another time */ }
}

function stopTimer(id: string) {
  window.clearTimeout(alarms.get(id));
  alarms.delete(id);
  timers.delete(id);
  saveTimers();
}

function useTimer(id: string): number | null {
  const [, tick] = useState(0);
  useEffect(() => {
    const fire = () => tick((n) => n + 1);
    timerListeners.add(fire);
    return () => { timerListeners.delete(fire); };
  }, []);
  const timer = timers.get(id);
  useEffect(() => {
    if (!timer) return;
    const handle = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(handle);
  }, [timer]);
  return timer ? Math.max(0, timer.endsAt - Date.now()) : null;
}

function clockText(ms: number): string {
  const total = Math.ceil(ms / 1000);
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, "0")}`;
}

function StepsView({ block, ctx }: { block: StepsBlock; ctx: BlockContext }) {
  const items = block.items ?? [];
  if (!items.length) return <p className="cal-blk-empty">{t("cal.block.empty")}</p>;
  return (
    <ol className="cal-steps-view">
      {items.map((step, n) => (
        <li key={step.id} className={`cal-step ${step.done ? "done" : ""}`}>
          <button type="button" role="checkbox" aria-checked={!!step.done} disabled={ctx.readOnly} className="cal-step-n" aria-label={t("cal.step.named", { n: n + 1, text: step.text })}
            onClick={() => ctx.onOps([{ op: "set_checked", block_id: block.id, item_id: step.id, checked: !step.done }], t("cal.step.short", { n: n + 1 }))}>
            {step.done ? <Icon name="check" size={16} /> : n + 1}
          </button>
          <div className="cal-step-body">
            <span className="cal-step-text">{step.text}</span>
            {!!step.duration_min && <StepTimer id={`${block.id}:${step.id}`} minutes={step.duration_min} label={step.text} />}
          </div>
        </li>
      ))}
    </ol>
  );
}

function StepTimer({ id, minutes, label }: { id: string; minutes: number; label: string }) {
  const left = useTimer(id);
  if (left === null) {
    return (
      <button type="button" className="cal-timer" onClick={() => startTimer(id, minutes, label)} aria-label={t("cal.timer.start", { n: minutes })}>
        <Icon name="clock" size={14} /> {t("cal.timer.minutes", { n: minutes })}
      </button>
    );
  }
  return (
    <button type="button" className="cal-timer running" onClick={() => stopTimer(id)} aria-label={t("cal.timer.stop")} aria-live="off">
      <Icon name="stop" size={12} /> <span className="cal-timer-clock">{clockText(left)}</span>
    </button>
  );
}

// ── links, actions, references ───────────────────────────────────────────────────────────────

function hostOf(url: string): string {
  try { return new URL(url).host; } catch { return url; }
}

function LinkView({ block }: { block: LinkBlock }) {
  const url = block.url && /^https?:\/\//.test(block.url) ? block.url : "";
  const label = block.label || block.title || (url ? hostOf(url) : block.file_id ?? "");
  const inner: ReactNode = (
    <>
      <Icon name={block.kind === "attachment" ? "attach" : "link"} size={16} />
      <span className="cal-link-text"><b>{label}</b>{url && <small>{hostOf(url)}</small>}</span>
      {url && <Icon name="external" size={14} />}
    </>
  );
  return url ? <a className="cal-link" href={url} target="_blank" rel="noreferrer">{inner}</a> : <div className="cal-link">{inner}</div>;
}

function ActionView({ block, ctx }: { block: ActionBlock; ctx: BlockContext }) {
  const agent = block.action === "agent";
  return (
    <button type="button" className={`btn cal-action ${agent ? "agent" : ""}`} disabled={agent && ctx.readOnly} onClick={() => ctx.onAction(block)}>
      <Icon name={agent ? "bots" : "external"} size={16} />
      <span>{block.label || block.title || t("cal.block.kind.action")}</span>
    </button>
  );
}

function RefView({ block, ctx }: { block: RefBlock; ctx: BlockContext }) {
  const found = useQuery<Occurrence>(block.ref_id ? eventKey(block.ref_id) : null, { staleMs: 30000 });
  const event = found.data;
  const type = event?.type ? ctx.types.find((x) => x.id === event.type) : undefined;
  if (!event) return <div className="cal-ref missing">{found.error ? t("cal.ref.missing") : t("common.loading")}</div>;
  const day = event.all_day ? (event.start_date || event.start_at.slice(0, 10)) : event.start_at.slice(0, 10);
  return (
    <button type="button" className="cal-ref" style={{ "--c": type?.color ?? event.color } as CSSProperties} onClick={() => ctx.onOpenRef(event.event_id ?? event.id)}>
      <TypeIcon icon={type?.icon} color={type?.color ?? event.color} />
      <span className="cal-ref-text"><b>{event.title || t("cal.untitled")}</b><small>{dayLabel(day, locale(), { weekday: "short", day: "numeric", month: "short" })}</small></span>
      <Icon name="forward" size={14} />
    </button>
  );
}

/** A block this app cannot draw: its kind and its JSON, folded, so nothing an agent wrote is hidden
 *  from the reader and nothing is dropped when the event is saved. */
function UnknownView({ block }: { block: Block }) {
  return <pre className="cal-unknown">{JSON.stringify(block, null, 2)}</pre>;
}
