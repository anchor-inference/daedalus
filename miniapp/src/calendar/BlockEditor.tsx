// The editor's half of structured entries: the blocks as forms, and a type's fields as a form drawn
// from its schema. No raw JSON: a checklist is a list of lines with boxes, a table a grid of cells, a
// pasted list becomes items. Blocks move by dragging their handle with a mouse, and by the arrows
// beside it with a finger or the keyboard. A block of a kind this app does not know is shown as it is
// and saved back untouched.

import { useState, type ClipboardEvent, type KeyboardEvent } from "react";
import { t } from "../i18n";
import { Icon } from "../icons";
import { OverflowMenu } from "../ui/dialogs";
import { blockTitle } from "./BlockView";
import {
  ADDABLE_KINDS, emptyBlock, fieldLabel, isKnown, linesToItems, newId, typedFields,
  type ActionBlock, type Block, type ChecklistBlock, type Column, type ColumnType, type FieldsBlock, type LinkBlock, type MarkdownBlock, type Row, type StepsBlock, type TableBlock, type TypeDef,
} from "./blocks";

type Change = (next: Block[]) => void;

export function BlocksEditor({ blocks, onChange }: { blocks: Block[]; onChange: Change }) {
  const [dragging, setDragging] = useState<string | null>(null);
  const replace = (id: string, next: Block) => onChange(blocks.map((b) => (b.id === id ? next : b)));
  const move = (from: number, to: number) => {
    if (to < 0 || to >= blocks.length || from === to) return;
    const next = [...blocks];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    onChange(next);
  };
  return (
    <div className="cal-bedit">
      {blocks.map((block, index) => (
        <div key={block.id} className={`cal-bedit-block ${dragging === block.id ? "dragging" : ""}`}
          onDragOver={(e) => { if (dragging) e.preventDefault(); }}
          onDrop={(e) => { e.preventDefault(); if (dragging) move(blocks.findIndex((b) => b.id === dragging), index); setDragging(null); }}>
          <div className="cal-bedit-head">
            <span className="cal-bedit-grip" draggable onDragStart={(e) => { setDragging(block.id); e.dataTransfer.effectAllowed = "move"; }} onDragEnd={() => setDragging(null)} title={t("cal.block.drag")} aria-hidden="true">
              <Icon name="vdots" size={16} />
            </span>
            <span className="cal-bedit-kind">{isKnown(block) ? t(`cal.block.kind.${block.kind}`) : `${t("cal.block.unsupported")} · ${block.kind}`}</span>
            {isKnown(block) && (
              <input className="field cal-bedit-title" value={block.title ?? ""} placeholder={t("cal.block.title")} aria-label={t("cal.block.title.named", { kind: blockTitle(block) })} maxLength={200}
                onChange={(e) => replace(block.id, { ...block, title: e.target.value || undefined } as Block)} />
            )}
            <span className="cal-spacer" />
            <button type="button" className="iconbtn small" disabled={index === 0} onClick={() => move(index, index - 1)} aria-label={t("cal.block.up")} title={t("cal.block.up")}><Icon name="up" size={14} /></button>
            <button type="button" className="iconbtn small" disabled={index === blocks.length - 1} onClick={() => move(index, index + 1)} aria-label={t("cal.block.down")} title={t("cal.block.down")}><Icon name="down" size={14} /></button>
            <button type="button" className="iconbtn small" onClick={() => onChange(blocks.filter((b) => b.id !== block.id))} aria-label={t("cal.block.remove")} title={t("cal.block.remove")}><Icon name="trash" size={14} /></button>
          </div>
          <BlockForm block={block} onChange={(next) => replace(block.id, next)} />
        </div>
      ))}
      <OverflowMenu label={t("cal.block.add")} className="cal-bedit-add"
        trigger={<span className="btn small ghost"><Icon name="plus" size={14} /> {t("cal.block.add")}</span>}
        items={ADDABLE_KINDS.map((kind) => ({ label: t(`cal.block.kind.${kind}`), onSelect: () => onChange([...blocks, emptyBlock(kind)]) }))} />
    </div>
  );
}

function BlockForm({ block, onChange }: { block: Block; onChange: (next: Block) => void }) {
  switch (block.kind) {
    case "markdown": {
      const b = block as MarkdownBlock;
      return <textarea className="field" rows={4} value={b.text ?? ""} placeholder={t("cal.block.markdown.hint")} aria-label={t("cal.block.kind.markdown")} onChange={(e) => onChange({ ...b, text: e.target.value })} />;
    }
    case "checklist": return <ItemsForm block={block as ChecklistBlock} steps={false} onChange={onChange} />;
    case "steps": return <ItemsForm block={block as StepsBlock} steps onChange={onChange} />;
    case "table": return <TableForm block={block as TableBlock} onChange={onChange} />;
    case "fields": return <FieldsForm block={block as FieldsBlock} onChange={onChange} />;
    case "link":
    case "attachment": {
      const b = block as LinkBlock;
      return (
        <div className="cal-bedit-pair">
          <input className="field" type="url" value={b.url ?? ""} placeholder="https://" aria-label={t("cal.block.url")} onChange={(e) => onChange({ ...b, url: e.target.value })} />
          <input className="field" value={b.label ?? ""} placeholder={t("cal.block.label")} aria-label={t("cal.block.label")} onChange={(e) => onChange({ ...b, label: e.target.value })} />
        </div>
      );
    }
    case "action": {
      const b = block as ActionBlock;
      return (
        <div className="cal-bedit-stack">
          <div className="cal-bedit-pair">
            <input className="field" value={b.label ?? ""} placeholder={t("cal.block.label")} aria-label={t("cal.block.label")} onChange={(e) => onChange({ ...b, label: e.target.value })} />
            <select className="field" value={b.action} aria-label={t("cal.block.action.kind")} onChange={(e) => onChange({ ...b, action: e.target.value as "url" | "agent" })}>
              <option value="url">{t("cal.block.action.url")}</option>
              <option value="agent">{t("cal.block.action.agent")}</option>
            </select>
          </div>
          {b.action === "url"
            ? <input className="field" type="url" value={b.url ?? ""} placeholder="https://" aria-label={t("cal.block.url")} onChange={(e) => onChange({ ...b, url: e.target.value })} />
            : (
              <>
                <input className="field" value={b.session_id ?? ""} placeholder={t("cal.block.action.session")} aria-label={t("cal.block.action.session")} onChange={(e) => onChange({ ...b, session_id: e.target.value })} />
                <textarea className="field" rows={2} value={b.prompt ?? ""} placeholder={t("cal.block.action.prompt")} aria-label={t("cal.block.action.prompt")} onChange={(e) => onChange({ ...b, prompt: e.target.value })} />
              </>
            )}
        </div>
      );
    }
    case "ref": {
      const b = block as Block & { ref_id?: string };
      return <input className="field" value={b.ref_id ?? ""} placeholder={t("cal.block.ref.id")} aria-label={t("cal.block.ref.id")} onChange={(e) => onChange({ ...b, ref_id: e.target.value } as Block)} />;
    }
    default:
      return (
        <div className="cal-bedit-unknown">
          <p className="cal-hint">{t("cal.block.unsupported.kept")}</p>
          <pre className="cal-unknown">{JSON.stringify(block, null, 2)}</pre>
        </div>
      );
  }
}

/** A checklist's items or a recipe's steps: a line each, and a last line to add to, which takes a
 *  pasted list as one item a line. */
function ItemsForm({ block, steps, onChange }: { block: ChecklistBlock | StepsBlock; steps: boolean; onChange: (next: Block) => void }) {
  const [draft, setDraft] = useState("");
  const items = (block.items ?? []) as (ChecklistBlock["items"][number] & StepsBlock["items"][number])[];
  const set = (next: typeof items) => onChange({ ...block, items: next } as Block);
  const addLines = (text: string) => {
    const parsed = linesToItems(text);
    if (!parsed.length) return;
    set([...items, ...parsed.map((p) => (steps ? { id: p.id, text: p.text, done: p.checked } : p)) as typeof items]);
  };
  const onPaste = (e: ClipboardEvent<HTMLInputElement>) => {
    const text = e.clipboardData.getData("text");
    if (!text.includes("\n")) return;
    e.preventDefault();
    addLines(draft ? `${draft}\n${text}` : text);
    setDraft("");
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (draft.trim()) { addLines(draft); setDraft(""); }
  };
  return (
    <div className="cal-bedit-items">
      {items.map((item, n) => (
        <div key={item.id} className="cal-bedit-item">
          {steps ? <span className="cal-bedit-n">{n + 1}</span> : (
            <input type="checkbox" checked={!!item.checked} aria-label={t("cal.block.item.done", { text: item.text })} onChange={(e) => set(items.map((x) => (x.id === item.id ? { ...x, checked: e.target.checked } : x)))} />
          )}
          <input className="field" value={item.text} aria-label={t("cal.block.item.text", { n: n + 1 })} onChange={(e) => set(items.map((x) => (x.id === item.id ? { ...x, text: e.target.value } : x)))} />
          {steps && (
            <input className="field cal-bedit-min" type="number" min={0} max={1440} value={item.duration_min ?? ""} placeholder={t("cal.unit.min")} aria-label={t("cal.block.step.minutes", { n: n + 1 })}
              onChange={(e) => set(items.map((x) => (x.id === item.id ? { ...x, duration_min: e.target.value ? Math.max(0, Number(e.target.value)) : null } : x)))} />
          )}
          <button type="button" className="iconbtn small" onClick={() => set(items.filter((x) => x.id !== item.id))} aria-label={t("cal.block.item.remove", { text: item.text })} title={t("common.delete")}><Icon name="close" size={14} /></button>
        </div>
      ))}
      <div className="cal-bedit-item add">
        <Icon name="plus" size={14} />
        <input className="field" value={draft} placeholder={steps ? t("cal.block.step.add") : t("cal.block.item.add")} aria-label={steps ? t("cal.block.step.add") : t("cal.block.item.add")} onChange={(e) => setDraft(e.target.value)} onPaste={onPaste} onKeyDown={onKey} />
      </div>
      <p className="cal-hint">{t("cal.block.paste.hint")}</p>
    </div>
  );
}

const COLUMN_TYPES: ColumnType[] = ["text", "number", "money", "checkbox", "link", "date"];

function TableForm({ block, onChange }: { block: TableBlock; onChange: (next: Block) => void }) {
  const columns = block.columns ?? [];
  const rows = block.rows ?? [];
  const setColumns = (next: Column[]) => onChange({ ...block, columns: next });
  const setRows = (next: Row[]) => onChange({ ...block, rows: next });
  const parseCell = (column: Column, text: string): unknown => {
    if (column.type === "number" || column.type === "money") {
      const n = Number(text.replace(",", ".").replace(/\s/g, ""));
      return text.trim() === "" ? null : Number.isFinite(n) ? n : text;
    }
    return text;
  };
  /** A pasted block of lines, one row each, its cells split at tabs (a spreadsheet's copy), at
   *  semicolons or at vertical bars. */
  const onPaste = (e: ClipboardEvent<HTMLElement>) => {
    const text = e.clipboardData.getData("text");
    if (!text.includes("\n") && !text.includes("\t")) return;
    e.preventDefault();
    const added = text.replace(/\r\n?/g, "\n").split("\n").filter((line) => line.trim()).map((line) => {
      const parts = line.split(line.includes("\t") ? "\t" : line.includes("|") ? "|" : ";").map((p) => p.trim()).filter((p, i, all) => !(p === "" && (i === 0 || i === all.length - 1) && line.includes("|")));
      const cells: Record<string, unknown> = {};
      let at = 0;
      for (const column of columns) {
        if (column.type === "checkbox") { cells[column.key] = false; continue; }
        if (at < parts.length) cells[column.key] = parseCell(column, parts[at++]);
      }
      return { id: newId("row"), cells };
    });
    setRows([...rows, ...added]);
  };
  return (
    <div className="cal-bedit-table">
      <div className="cal-bedit-cols">
        {columns.map((c, i) => (
          <div key={c.key} className="cal-bedit-col">
            <input className="field" value={c.label} aria-label={t("cal.block.column.label", { n: i + 1 })} onChange={(e) => setColumns(columns.map((x) => (x.key === c.key ? { ...x, label: e.target.value } : x)))} />
            <select className="field" value={c.type} aria-label={t("cal.block.column.type", { n: i + 1 })} onChange={(e) => setColumns(columns.map((x) => (x.key === c.key ? { ...x, type: e.target.value as ColumnType, aggregate: e.target.value === "checkbox" ? "count" : x.aggregate } : x)))}>
              {COLUMN_TYPES.map((type) => <option key={type} value={type}>{t(`cal.block.column.${type}`)}</option>)}
            </select>
            {(c.type === "number" || c.type === "money") && (
              <select className="field" value={c.aggregate ?? ""} aria-label={t("cal.block.column.total", { n: i + 1 })} onChange={(e) => setColumns(columns.map((x) => (x.key === c.key ? { ...x, aggregate: (e.target.value || undefined) as Column["aggregate"] } : x)))}>
                <option value="">{t("cal.block.total.none")}</option>
                <option value="sum">{t("cal.block.total.sum")}</option>
                <option value="avg">{t("cal.block.total.avg")}</option>
              </select>
            )}
            <button type="button" className="iconbtn small" disabled={columns.length <= 1} onClick={() => setColumns(columns.filter((x) => x.key !== c.key))} aria-label={t("cal.block.column.remove", { name: c.label })} title={t("common.delete")}><Icon name="close" size={14} /></button>
          </div>
        ))}
        <button type="button" className="btn small ghost" onClick={() => setColumns([...columns, { key: newId("col"), label: t("cal.block.column.new"), type: "text" }])}><Icon name="plus" size={14} /> {t("cal.block.column.add")}</button>
      </div>
      <div className="cal-bedit-grid" onPaste={onPaste} style={{ gridTemplateColumns: `${columns.map((c) => (c.type === "checkbox" ? "36px" : "minmax(90px, 1fr)")).join(" ")} 32px` }}>
        {columns.map((c) => <span key={c.key} className="cal-bedit-th">{c.label}</span>)}
        <span />
        {rows.map((row, r) => (
          <div key={row.id} className="cal-bedit-row" role="group" aria-label={t("cal.block.row", { n: r + 1 })}>
            {columns.map((c) => c.type === "checkbox" ? (
              <input key={c.key} type="checkbox" checked={row.cells?.[c.key] === true} aria-label={`${c.label} ${r + 1}`} onChange={(e) => setRows(rows.map((x) => (x.id === row.id ? { ...x, cells: { ...x.cells, [c.key]: e.target.checked } } : x)))} />
            ) : (
              <input key={c.key} className="field" type={c.type === "number" || c.type === "money" ? "text" : c.type === "date" ? "date" : "text"} inputMode={c.type === "number" || c.type === "money" ? "decimal" : undefined}
                value={row.cells?.[c.key] === undefined || row.cells?.[c.key] === null ? "" : String(row.cells[c.key])} aria-label={`${c.label} ${r + 1}`}
                onChange={(e) => setRows(rows.map((x) => (x.id === row.id ? { ...x, cells: { ...x.cells, [c.key]: parseCell(c, e.target.value) } } : x)))} />
            ))}
            <button type="button" className="iconbtn small" onClick={() => setRows(rows.filter((x) => x.id !== row.id))} aria-label={t("cal.block.row.remove", { n: r + 1 })} title={t("common.delete")}><Icon name="close" size={14} /></button>
          </div>
        ))}
      </div>
      <button type="button" className="btn small ghost" onClick={() => setRows([...rows, { id: newId("row"), cells: Object.fromEntries(columns.filter((c) => c.type === "checkbox").map((c) => [c.key, false])) }])}><Icon name="plus" size={14} /> {t("cal.block.row.add")}</button>
      <p className="cal-hint">{t("cal.block.table.paste")}</p>
    </div>
  );
}

function FieldsForm({ block, onChange }: { block: FieldsBlock; onChange: (next: Block) => void }) {
  if (block.source === "fields") return <p className="cal-hint">{t("cal.block.fields.source")}</p>;
  const items = block.items ?? [];
  const set = (next: typeof items) => onChange({ ...block, items: next });
  return (
    <div className="cal-bedit-items">
      {items.map((item, i) => (
        <div key={i} className="cal-bedit-item fields">
          <input className="field" value={item.label} placeholder={t("cal.block.label")} aria-label={t("cal.block.field.label", { n: i + 1 })} onChange={(e) => set(items.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)))} />
          <input className="field" value={item.value === undefined || item.value === null ? "" : String(item.value)} placeholder={t("cal.block.value")} aria-label={t("cal.block.field.value", { n: i + 1 })}
            onChange={(e) => set(items.map((x, j) => (j === i ? { ...x, value: e.target.value !== "" && Number.isFinite(Number(e.target.value)) ? Number(e.target.value) : e.target.value } : x)))} />
          <input className="field cal-bedit-min" value={item.unit ?? ""} placeholder={t("cal.block.unit")} aria-label={t("cal.block.field.unit", { n: i + 1 })} onChange={(e) => set(items.map((x, j) => (j === i ? { ...x, unit: e.target.value || undefined } : x)))} />
          <button type="button" className="iconbtn small" onClick={() => set(items.filter((_, j) => j !== i))} aria-label={t("cal.block.field.remove", { n: i + 1 })} title={t("common.delete")}><Icon name="close" size={14} /></button>
        </div>
      ))}
      <button type="button" className="btn small ghost" onClick={() => set([...items, { label: "", value: "" }])}><Icon name="plus" size={14} /> {t("cal.block.field.add")}</button>
    </div>
  );
}

/** A type's fields as a form drawn from its schema: a number with its unit, a choice from a list, a
 *  yes or no, a date. A value the schema no longer names is shown as stale and kept. */
export function TypeFieldsForm({ type, fields, onChange }: { type: TypeDef; fields: Record<string, unknown>; onChange: (next: Record<string, unknown>) => void }) {
  const schema = type.fields_schema ?? {};
  const set = (key: string, value: unknown) => onChange({ ...fields, [key]: value });
  const stale = typedFields(type, fields).filter((f) => f.stale);
  return (
    <div className="cal-tfields">
      {Object.entries(schema).map(([key, s]) => {
        const label = s.label || fieldLabel(key);
        const value = fields[key];
        let control;
        if (s.type === "enum") {
          control = (
            <select className="field" value={value === undefined || value === null ? "" : String(value)} aria-label={label} onChange={(e) => set(key, e.target.value || null)}>
              <option value="">—</option>
              {(s.values ?? []).map((v) => <option key={v} value={v}>{v}</option>)}
            </select>
          );
        } else if (s.type === "boolean") {
          control = <input type="checkbox" checked={value === true} aria-label={label} onChange={(e) => set(key, e.target.checked)} />;
        } else if (s.type === "number" || s.type === "integer" || s.type === "money") {
          control = <input className="field" type="number" step={s.type === "integer" ? 1 : "any"} value={value === undefined || value === null ? "" : String(value)} aria-label={label} onChange={(e) => set(key, e.target.value === "" ? null : Number(e.target.value))} />;
        } else if (s.type === "date") {
          control = <input className="field" type="date" value={typeof value === "string" ? value.slice(0, 10) : ""} aria-label={label} onChange={(e) => set(key, e.target.value || null)} />;
        } else {
          control = <input className="field" value={value === undefined || value === null ? "" : String(value)} aria-label={label} onChange={(e) => set(key, e.target.value)} />;
        }
        return (
          <label key={key} className="cal-tfield">
            <span>{label}</span>
            <span className="cal-tfield-control">{control}{s.unit && <small>{s.unit}</small>}</span>
          </label>
        );
      })}
      {stale.map((f) => (
        <div key={f.key} className="cal-tfield stale" title={t("cal.field.stale")}>
          <span>{f.label} <span className="cal-stale">{t("cal.field.stale.short")}</span></span>
          <span className="cal-tfield-control">{f.text}</span>
        </div>
      ))}
    </div>
  );
}
