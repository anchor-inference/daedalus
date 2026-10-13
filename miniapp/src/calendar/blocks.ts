// Structured calendar entries: the blocks an event's body is made of, the types an event can have,
// the groups that hold a plan together, and the pure logic the screens share: progress, a table's
// totals, a type's chip, the copy as text, the local application of a patch before the host answers.
//
// Nothing here knows about meals or shopping. A block is a checklist, a table, a set of fields or a
// list of steps, and what it holds is the agent's business; the screen draws the shapes.

import { locale, num, t } from "../i18n";

export type EventStatus = "planned" | "done" | "skipped" | "cancelled";
export const STATUSES: EventStatus[] = ["planned", "done", "skipped", "cancelled"];

export type ChecklistItem = { id: string; text: string; checked: boolean; note?: string; meta?: unknown };
export type ColumnType = "text" | "number" | "money" | "checkbox" | "link" | "date";
export type Column = { key: string; label: string; type: ColumnType; unit?: string; align?: "left" | "right" | "center"; aggregate?: "sum" | "avg" | "count" };
export type Row = { id: string; cells: Record<string, unknown> };
export type StepItem = { id: string; text: string; duration_min?: number | null; done: boolean };
export type FieldItem = { label: string; value: unknown; unit?: string };

type Base = { id: string; title?: string; collapsed?: boolean };
export type MarkdownBlock = Base & { kind: "markdown"; text: string };
export type ChecklistBlock = Base & { kind: "checklist"; items: ChecklistItem[]; show_progress?: boolean };
export type TableBlock = Base & { kind: "table"; columns: Column[]; rows: Row[] };
/** Either its own items, or `source: "fields"`: the event's typed fields, labelled by its type. */
export type FieldsBlock = Base & { kind: "fields"; items?: FieldItem[]; source?: "fields" };
export type StepsBlock = Base & { kind: "steps"; items: StepItem[] };
export type LinkBlock = Base & { kind: "link" | "attachment"; url?: string; file_id?: string; label?: string };
export type ActionBlock = Base & { kind: "action"; label: string; action: "url" | "agent"; url?: string; session_id?: string; prompt?: string };
export type RefBlock = Base & { kind: "ref"; ref_id: string };
export type KnownBlock = MarkdownBlock | ChecklistBlock | TableBlock | FieldsBlock | StepsBlock | LinkBlock | ActionBlock | RefBlock;
/** A block of a kind this app does not know: drawn as its JSON and written back exactly as read. */
export type OtherBlock = Base & { kind: string; [key: string]: unknown };
export type Block = KnownBlock | OtherBlock;

export const KNOWN_KINDS = ["markdown", "checklist", "table", "fields", "steps", "link", "attachment", "action", "ref"] as const;
/** The kinds the editor offers to add; a link and an attachment are one thing to a person. */
export const ADDABLE_KINDS = ["checklist", "table", "markdown", "fields", "steps", "link", "action"] as const;

export function isKnown(block: Block): block is KnownBlock {
  return (KNOWN_KINDS as readonly string[]).includes(block.kind);
}

export type FieldSchema = { type: "string" | "number" | "integer" | "boolean" | "enum" | "date" | "money"; unit?: string; values?: string[]; label?: string };
export type TypeAggregate = { field: string; op: "sum" | "avg" | "count"; per: "day" | "week"; label?: string; target?: [number, number] };
export type TypeDef = {
  id: string;
  name: string;
  icon: string;
  color: string;
  version?: number;
  owner?: string;
  fields_schema?: Record<string, FieldSchema>;
  default_blocks?: Partial<Block>[];
  display?: { chip?: string; subtitle?: string; badge?: string };
  aggregates?: TypeAggregate[];
  status_flow?: EventStatus[];
  quick_actions?: string[];
};

export type Group = {
  id: string;
  name: string;
  icon?: string;
  color?: string;
  description?: string;
  blocks?: Block[];
  created_by?: string;
  archived?: boolean;
  hidden?: boolean;
  event_count?: number;
  progress?: Progress | null;
  range?: { from: string; to: string } | null;
};

export type Progress = { done: number; total: number };
export type BlockSummary = { id: string; kind: string; title?: string; done?: number | null; total?: number | null };
export type AggregateItem = { type: string; field: string; op: string; label?: string; value: number; unit?: string; target?: [number, number]; ok?: boolean };
export type AggregateDay = { date: string; items: AggregateItem[] };
export type HistoryEntry = { at: string; actor: string; action: string; block_id?: string; item_id?: string; summary: string };
export type EventLink = { kind: "event" | "task" | "url" | "file"; ref: string; label?: string };

/** The structured part of an event, as the host sends it beside the old fields. */
export type Structured = {
  type?: string | null;
  fields?: Record<string, unknown>;
  blocks?: Block[];
  status?: EventStatus;
  tags?: string[];
  group_id?: string | null;
  links?: EventLink[];
  block_summary?: BlockSummary[];
  progress?: Progress | null;
  chip?: string | null;
};

// ── ids ───────────────────────────────────────────────────────────────────────────────────────

let seq = 0;
/** A fresh id for a block, an item or a row made here. Unique within the session and unlikely to
 *  meet one the host or an agent made, which is all a block id has to be. */
export function newId(prefix: string): string {
  seq += 1;
  return `${prefix}_${Date.now().toString(36)}${seq.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
}

// ── progress ─────────────────────────────────────────────────────────────────────────────────

export function checkboxColumn(block: TableBlock): Column | undefined {
  return block.columns.find((c) => c.type === "checkbox");
}

/** What a block has done of its total: ticked items, finished steps, or rows ticked in the table's
 *  first checkbox column. Null for a block that has no such count. */
export function blockProgress(block: Block): Progress | null {
  if (block.kind === "checklist") {
    const items = (block as ChecklistBlock).items ?? [];
    return { done: items.filter((i) => i.checked).length, total: items.length };
  }
  if (block.kind === "steps") {
    const items = (block as StepsBlock).items ?? [];
    return { done: items.filter((i) => i.done).length, total: items.length };
  }
  if (block.kind === "table") {
    const table = block as TableBlock;
    const column = checkboxColumn(table);
    if (!column) return null;
    const rows = table.rows ?? [];
    return { done: rows.filter((r) => r.cells?.[column.key] === true).length, total: rows.length };
  }
  return null;
}

/** An event's progress as its chip shows it: the block the type's badge names (`progress:table`), or
 *  the first checklist, or the first table with a checkbox column. */
export function eventProgress(blocks: Block[] | undefined, badge?: string): Progress | null {
  if (!blocks?.length) return null;
  const wanted = badge?.startsWith("progress:") ? badge.slice(9) : "";
  const order = wanted ? [wanted] : ["checklist", "table"];
  for (const kind of order) {
    for (const block of blocks) {
      if (block.kind !== kind) continue;
      const progress = blockProgress(block);
      if (progress && progress.total) return progress;
    }
  }
  return null;
}

// ── numbers, money, totals ───────────────────────────────────────────────────────────────────

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value.replace(",", ".").replace(/\s/g, "")))) return Number(value.replace(",", ".").replace(/\s/g, ""));
  return null;
}

/** A column's unit: its own, or a label that is only a symbol ("₸", "$", "%"), which is how an agent
 *  most often writes a money column's head. */
export function columnUnit(column: Column): string {
  if (column.unit) return column.unit;
  if (column.type === "money" && column.label && !/\p{L}{2}/u.test(column.label) && column.label.length <= 3) return column.label;
  return "";
}

/** A number as the reader's language writes it, with at most two decimals and a unit after it. */
export function formatNumber(value: number, unit = ""): string {
  const text = value.toLocaleString(locale(), { maximumFractionDigits: 2 });
  return unit ? `${text} ${unit}` : text;
}

/** One cell as text: a number in the reader's digits, a money amount with its unit, a date in words. */
export function cellText(column: Column, value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  if (column.type === "checkbox") return value === true ? "✓" : "";
  if (column.type === "number" || column.type === "money") {
    const n = asNumber(value);
    if (n === null) return String(value);
    return formatNumber(n, column.type === "money" ? columnUnit(column) : column.unit ?? "");
  }
  if (column.type === "date" && typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value)) {
    const date = new Date(`${value.slice(0, 10)}T12:00:00Z`);
    return date.toLocaleDateString(locale(), { day: "numeric", month: "short", timeZone: "UTC" });
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** A table column's footer: the sum or the average of its numbers, or for a checkbox column how many
 *  of the rows are ticked. Null for a column with no aggregate. */
export function columnTotal(column: Column, rows: Row[]): { text: string; value: number; of?: number } | null {
  if (!column.aggregate) return null;
  if (column.type === "checkbox" || column.aggregate === "count") {
    if (column.type === "checkbox") {
      const done = rows.filter((r) => r.cells?.[column.key] === true).length;
      return { text: `${num(done)}/${num(rows.length)}`, value: done, of: rows.length };
    }
    const filled = rows.filter((r) => r.cells?.[column.key] !== undefined && r.cells?.[column.key] !== null && r.cells?.[column.key] !== "").length;
    return { text: num(filled), value: filled };
  }
  const numbers = rows.map((r) => asNumber(r.cells?.[column.key])).filter((n): n is number => n !== null);
  const sum = numbers.reduce((a, b) => a + b, 0);
  const value = column.aggregate === "avg" ? (numbers.length ? sum / numbers.length : 0) : sum;
  const unit = column.type === "money" ? columnUnit(column) : column.unit ?? "";
  return { text: formatNumber(Math.round(value * 100) / 100, unit), value };
}

/** A day's aggregate as the agenda prints it under the day: "Σ 2 028 ккал", and whether it met the
 *  type's target (a tick), fell short or ran over (an arrow), or has no target. */
export function aggregateParts(item: AggregateItem): { symbol: string; value: string; state: "ok" | "low" | "high" | "none" } {
  const symbol = item.op === "avg" ? "⌀" : item.op === "count" ? "#" : "Σ";
  const value = formatNumber(Math.round(item.value), item.unit ?? "");
  let state: "ok" | "low" | "high" | "none" = "none";
  if (item.target && item.target.length === 2) state = item.value < item.target[0] ? "low" : item.value > item.target[1] ? "high" : "ok";
  else if (item.ok === true) state = "ok";
  return { symbol, value, state };
}

export function aggregateText(item: AggregateItem): string {
  const parts = aggregateParts(item);
  const mark = parts.state === "ok" ? " ✓" : parts.state === "low" ? " ↓" : parts.state === "high" ? " ↑" : "";
  return `${parts.symbol} ${parts.value}${mark}`;
}

// ── the type's chip ──────────────────────────────────────────────────────────────────────────

/** The type's `display.chip` filled from the event: `{meal} · {kcal} ккал` → "Завтрак · 518 ккал".
 *  A hole with nothing to fill it is dropped with the separator before it, so a meal with no calories
 *  yet reads "Завтрак", not "Завтрак ·  ккал". */
export function renderChip(template: string | undefined, title: string, fields: Record<string, unknown> | undefined): string | null {
  if (!template) return null;
  const values: Record<string, unknown> = { ...(fields ?? {}), title };
  const parts = template.split(/\s+·\s+/).map((part) => {
    let missing = false;
    const filled = part.replace(/\{(\w+)\}/g, (_, name: string) => {
      const value = values[name];
      if (value === undefined || value === null || value === "") { missing = true; return ""; }
      return typeof value === "number" ? value.toLocaleString(locale()) : String(value);
    });
    return missing ? "" : filled.trim();
  }).filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

// ── tables on a narrow screen ────────────────────────────────────────────────────────────────

export type TableCard = { id: string; title: string; check: Column | null; checked: boolean; lines: { key: string; label: string; text: string; align?: string }[] };

/** A table as cards for a phone: the first text column is the card's title, a checkbox column its
 *  checkbox, every other filled cell a "label: value" line. Nothing is cut off, which a table scrolled
 *  sideways on a 390 px screen could not promise. */
export function tableCards(block: TableBlock): TableCard[] {
  const check = checkboxColumn(block) ?? null;
  const head = block.columns.find((c) => c.type === "text" || c.type === "link") ?? block.columns.find((c) => c !== check);
  return (block.rows ?? []).map((row) => ({
    id: row.id,
    title: head ? cellText(head, row.cells?.[head.key]) : "",
    check,
    checked: check ? row.cells?.[check.key] === true : false,
    lines: block.columns.filter((c) => c !== head && c !== check).map((c) => ({ key: c.key, label: c.label, text: cellText(c, row.cells?.[c.key]), align: c.align })).filter((line) => line.text),
  }));
}

// ── pasted text into items ───────────────────────────────────────────────────────────────────

/** Lines of pasted text as checklist items: one per non-empty line, with a bullet, a number, a
 *  Markdown task box or a ballot glyph taken off and read as the item's state. */
export function linesToItems(text: string): ChecklistItem[] {
  return text.replace(/\r\n?/g, "\n").split("\n").map((line) => line.trim()).filter(Boolean).map((line) => {
    let body = line.replace(/^(?:[-*+•–]|\d+[.)])\s+/, "");
    let checked = false;
    const box = /^\[( |x|X)\]\s*/.exec(body);
    if (box) { checked = box[1] !== " "; body = body.slice(box[0].length); }
    const glyph = /^([☐☑✓✔])\s*/.exec(body);
    if (glyph) { checked = glyph[1] !== "☐"; body = body.slice(glyph[0].length); }
    return { id: newId("it"), text: body.trim(), checked };
  }).filter((item) => item.text);
}

// ── a new event's blocks from its type ───────────────────────────────────────────────────────

/** The blocks a new event of a type starts with: the type's template, each with its own id and the
 *  empty body its kind needs. */
export function blocksFromType(type: TypeDef | undefined): Block[] {
  return (type?.default_blocks ?? []).map((tpl) => emptyBlock((tpl.kind ?? "markdown") as string, tpl));
}

export function emptyBlock(kind: string, over: Partial<Block> = {}): Block {
  const base = { ...over, id: newId("blk"), kind } as Record<string, unknown>;
  const body: Record<string, unknown> =
    kind === "checklist" ? { items: [], show_progress: true }
    : kind === "steps" ? { items: [] }
    : kind === "markdown" ? { text: "" }
    : kind === "fields" ? ((over as FieldsBlock).source ? {} : { items: [] })
    : kind === "table" ? { columns: [{ key: "name", label: t("cal.block.table.col.name"), type: "text" }, { key: "done", label: "✓", type: "checkbox", aggregate: "count" }], rows: [] }
    : kind === "link" ? { url: "", label: "" }
    : kind === "action" ? { label: "", action: "url", url: "" }
    : {};
  return { ...body, ...base } as unknown as Block;
}

// ── applying a patch here, before the host answers ───────────────────────────────────────────

export type BlockOp =
  | { op: "set_checked"; block_id: string; item_id?: string; row_id?: string; column?: string; checked: boolean }
  | { op: "add_item"; block_id: string; item: Record<string, unknown>; index?: number }
  | { op: "update_item"; block_id: string; item_id: string; patch: Record<string, unknown> }
  | { op: "remove_item"; block_id: string; item_id: string }
  | { op: "add_row"; block_id: string; row: Row; index?: number }
  | { op: "update_cell"; block_id: string; row_id: string; column: string; value: unknown }
  | { op: "remove_row"; block_id: string; row_id: string }
  | { op: "reorder"; block_id: string; ids: string[] }
  | { op: "add_block"; block: Block; index?: number }
  | { op: "update_block"; block_id: string; patch: Record<string, unknown> }
  | { op: "remove_block"; block_id: string }
  | { op: "reorder_blocks"; ids: string[] };

function insert<T>(list: T[], value: T, index?: number): T[] {
  const next = [...list];
  next.splice(index === undefined || index < 0 || index > next.length ? next.length : index, 0, value);
  return next;
}

function reordered<T extends { id: string }>(list: T[], ids: string[]): T[] {
  const byId = new Map(list.map((x) => [x.id, x]));
  const first = ids.map((id) => byId.get(id)).filter((x): x is T => !!x);
  return [...first, ...list.filter((x) => !ids.includes(x.id))];
}

/** One op applied to a list of blocks, the way the host applies it; an op that names something not
 *  there changes nothing, as the host's answer will then say. Blocks of an unknown kind pass through
 *  untouched. */
export function applyOp(blocks: Block[], op: BlockOp): Block[] {
  if (op.op === "add_block") return insert(blocks, op.block, op.index);
  if (op.op === "remove_block") return blocks.filter((b) => b.id !== op.block_id);
  if (op.op === "reorder_blocks") return reordered(blocks, op.ids);
  return blocks.map((block) => {
    if (block.id !== op.block_id) return block;
    const b = block as Record<string, unknown> & Block;
    switch (op.op) {
      case "update_block":
        return { ...b, ...op.patch, id: b.id, kind: b.kind } as Block;
      case "set_checked": {
        if (op.row_id && op.column) {
          const rows = (b.rows as Row[] | undefined) ?? [];
          return { ...b, rows: rows.map((r) => (r.id === op.row_id ? { ...r, cells: { ...r.cells, [op.column!]: op.checked } } : r)) } as Block;
        }
        const field = b.kind === "steps" ? "done" : "checked";
        const items = (b.items as { id: string }[] | undefined) ?? [];
        return { ...b, items: items.map((i) => (i.id === op.item_id ? { ...i, [field]: op.checked } : i)) } as Block;
      }
      case "add_item":
        return { ...b, items: insert((b.items as unknown[]) ?? [], op.item, op.index) } as Block;
      case "update_item":
        return { ...b, items: ((b.items as { id: string }[]) ?? []).map((i) => (i.id === op.item_id ? { ...i, ...op.patch, id: i.id } : i)) } as Block;
      case "remove_item":
        return { ...b, items: ((b.items as { id: string }[]) ?? []).filter((i) => i.id !== op.item_id) } as Block;
      case "add_row":
        return { ...b, rows: insert((b.rows as Row[]) ?? [], op.row, op.index) } as Block;
      case "update_cell":
        return { ...b, rows: ((b.rows as Row[]) ?? []).map((r) => (r.id === op.row_id ? { ...r, cells: { ...r.cells, [op.column]: op.value } } : r)) } as Block;
      case "remove_row":
        return { ...b, rows: ((b.rows as Row[]) ?? []).filter((r) => r.id !== op.row_id) } as Block;
      case "reorder":
        if (Array.isArray(b.rows)) return { ...b, rows: reordered(b.rows as Row[], op.ids) } as Block;
        return { ...b, items: reordered((b.items as { id: string }[]) ?? [], op.ids) } as Block;
      default:
        return block;
    }
  });
}

export function applyOps(blocks: Block[], ops: BlockOp[]): Block[] {
  return ops.reduce(applyOp, blocks);
}

/** The op that takes a tick back: the same address with the old state. */
export function inverseCheck(op: Extract<BlockOp, { op: "set_checked" }>): Extract<BlockOp, { op: "set_checked" }> {
  return { ...op, checked: !op.checked };
}

/** The operations that turn one list of blocks into another, for the editor's save: each block that
 *  changed is replaced whole, blocks gone are removed, new ones added, and the order set last. Whole
 *  blocks rather than item ops, because an editor that rewrote a table row by row would send hundreds
 *  of ops for one pasted list; a block's own id still addresses it, so a tick on another block that
 *  lands meanwhile is not lost. */
export function diffBlocks(before: Block[], after: Block[]): BlockOp[] {
  const ops: BlockOp[] = [];
  const old = new Map(before.map((b) => [b.id, b]));
  const now = new Set(after.map((b) => b.id));
  for (const b of before) if (!now.has(b.id)) ops.push({ op: "remove_block", block_id: b.id });
  after.forEach((b, index) => {
    const was = old.get(b.id);
    if (!was) ops.push({ op: "add_block", block: b, index });
    else if (JSON.stringify(was) !== JSON.stringify(b)) {
      const { id: _id, kind: _kind, ...patch } = b as Record<string, unknown>;
      ops.push({ op: "update_block", block_id: b.id, patch });
    }
  });
  const kept = before.filter((b) => now.has(b.id)).map((b) => b.id);
  const order = after.map((b) => b.id);
  const keptInNewOrder = order.filter((id) => kept.includes(id));
  if (keptInNewOrder.join("\n") !== kept.join("\n") || ops.some((o) => o.op === "add_block")) ops.push({ op: "reorder_blocks", ids: order });
  return ops;
}

// ── copy as text ─────────────────────────────────────────────────────────────────────────────

export type CopyEvent = { title: string; when: string; description?: string; fields?: Record<string, unknown>; blocks?: Block[]; status?: EventStatus; tags?: string[] };

/** The whole event as text to send someone: the title and time, the type's fields, every block in
 *  order (a checklist as ☐/☑ lines, a table as one line a row with its totals), and the description.
 *  As Markdown, a heading, task boxes and a real table; as plain text, the same without the marks. */
export function eventAsText(event: CopyEvent, type: TypeDef | undefined, markdown: boolean): string {
  const out: string[] = [];
  out.push(markdown ? `# ${event.title}` : event.title);
  out.push(event.when);
  if (event.tags?.length) out.push(event.tags.map((tag) => `#${tag}`).join(" "));
  const fieldLines = typedFields(type, event.fields).map((f) => `${f.label}: ${f.text}`);
  if (fieldLines.length) out.push("", ...fieldLines.map((line) => (markdown ? `- ${line}` : line)));
  for (const block of event.blocks ?? []) {
    const lines = blockAsText(block, type, event.fields, markdown);
    if (!lines.length) continue;
    out.push("");
    if (block.title) out.push(markdown ? `## ${block.title}` : block.title);
    out.push(...lines);
  }
  if (event.description?.trim()) out.push("", event.description.trim());
  return out.join("\n");
}

export function blockAsText(block: Block, type: TypeDef | undefined, fields: Record<string, unknown> | undefined, markdown: boolean): string[] {
  switch (block.kind) {
    case "markdown":
      return (block as MarkdownBlock).text ? [(block as MarkdownBlock).text] : [];
    case "checklist":
      return ((block as ChecklistBlock).items ?? []).map((i) => (markdown ? `- [${i.checked ? "x" : " "}] ${i.text}` : `${i.checked ? "☑" : "☐"} ${i.text}`));
    case "steps":
      return ((block as StepsBlock).items ?? []).map((s, n) => `${n + 1}. ${s.done ? (markdown ? "~~" : "✓ ") : ""}${s.text}${s.done && markdown ? "~~" : ""}${s.duration_min ? ` (${s.duration_min} ${t("cal.unit.min")})` : ""}`);
    case "fields": {
      const f = block as FieldsBlock;
      const items = f.source === "fields" ? typedFields(type, fields).map((x) => `${x.label}: ${x.text}`) : (f.items ?? []).map((x) => `${x.label}: ${typeof x.value === "number" ? formatNumber(x.value, x.unit) : `${x.value ?? ""}${x.unit ? ` ${x.unit}` : ""}`}`);
      return items.map((line) => (markdown ? `- ${line}` : line));
    }
    case "table": {
      const table = block as TableBlock;
      const columns = table.columns ?? [];
      const rows = table.rows ?? [];
      const totals = columns.map((c) => columnTotal(c, rows));
      if (markdown) {
        const head = `| ${columns.map((c) => c.label).join(" | ")} |`;
        const rule = `| ${columns.map((c) => (c.align === "right" || c.type === "money" || c.type === "number" ? "---:" : "---")).join(" | ")} |`;
        const body = rows.map((r) => `| ${columns.map((c) => (c.type === "checkbox" ? (r.cells?.[c.key] === true ? "☑" : "☐") : cellText(c, r.cells?.[c.key]).replace(/\|/g, "\\|"))).join(" | ")} |`);
        const foot = totals.some(Boolean) ? [`| ${totals.map((x, i) => (x ? x.text : i === 0 ? t("cal.block.total") : "")).join(" | ")} |`] : [];
        return [head, rule, ...body, ...foot];
      }
      const check = checkboxColumn(table);
      const body = rows.map((r) => {
        const box = check ? `${r.cells?.[check.key] === true ? "☑" : "☐"} ` : "";
        return box + columns.filter((c) => c !== check).map((c) => cellText(c, r.cells?.[c.key])).filter(Boolean).join(" · ");
      });
      const foot = totals.map((x, i) => (x ? `${columns[i].label}: ${x.text}` : "")).filter(Boolean);
      return foot.length ? [...body, `${t("cal.block.total")}: ${foot.join(", ")}`] : body;
    }
    case "link":
    case "attachment": {
      const l = block as LinkBlock;
      if (!l.url) return l.label ? [l.label] : [];
      return [markdown ? `[${l.label || l.url}](${l.url})` : `${l.label ? `${l.label}: ` : ""}${l.url}`];
    }
    case "action": {
      const a = block as ActionBlock;
      return a.action === "url" && a.url ? [markdown ? `[${a.label}](${a.url})` : `${a.label}: ${a.url}`] : [];
    }
    default:
      return [];
  }
}

// ── a type's fields ──────────────────────────────────────────────────────────────────────────

export type TypedField = { key: string; label: string; text: string; schema?: FieldSchema; stale: boolean; value: unknown };

/** The event's field values with their labels and units from the type's schema, in the schema's
 *  order, and then any value the schema no longer names: a type that dropped a field leaves the old
 *  value on old events, marked stale rather than lost. */
export function typedFields(type: TypeDef | undefined, fields: Record<string, unknown> | undefined): TypedField[] {
  const values = fields ?? {};
  const schema = type?.fields_schema ?? {};
  const out: TypedField[] = [];
  for (const [key, s] of Object.entries(schema)) {
    if (!(key in values) || values[key] === null || values[key] === "") continue;
    out.push({ key, label: s.label || fieldLabel(key), text: fieldText(s, values[key]), schema: s, stale: false, value: values[key] });
  }
  for (const [key, value] of Object.entries(values)) {
    if (key in schema || value === null || value === "") continue;
    out.push({ key, label: fieldLabel(key), text: typeof value === "object" ? JSON.stringify(value) : String(value), stale: !!type, value });
  }
  return out;
}

/** A field's key as a label when the schema gives none: `protein_g` → "Protein g". */
export function fieldLabel(key: string): string {
  const words = key.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toLocaleUpperCase(locale()) + words.slice(1);
}

export function fieldText(schema: FieldSchema | undefined, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (schema?.type === "boolean") return value ? t("cal.yes") : t("cal.no");
  if (schema && (schema.type === "number" || schema.type === "integer" || schema.type === "money")) {
    const n = asNumber(value);
    if (n !== null) return formatNumber(n, schema.unit ?? "");
  }
  if (schema?.type === "date" && typeof value === "string") return cellText({ key: "", label: "", type: "date" }, value);
  return `${String(value)}${schema?.unit && typeof value !== "number" ? ` ${schema.unit}` : ""}`;
}

/** The statuses an event of a type moves through, in the type's order, or every status. */
export function statusFlow(type: TypeDef | undefined): EventStatus[] {
  const flow = type?.status_flow?.filter((s): s is EventStatus => (STATUSES as string[]).includes(s));
  return flow?.length ? (flow.includes("planned") ? flow : ["planned", ...flow]) : STATUSES;
}

/** What a swipe does on a row: right for done and left for skip, as the type's quick actions allow
 *  (every event allows both when its type says nothing). */
export function swipeActions(type: TypeDef | undefined): { right: EventStatus | null; left: EventStatus | null } {
  const quick = type?.quick_actions;
  if (!quick) return { right: "done", left: "skipped" };
  return { right: quick.includes("mark_done") ? "done" : null, left: quick.includes("skip") ? "skipped" : null };
}

/** Whether a colour is light enough that text on it should be dark: the type's colour fills a chip
 *  in both themes, and a yellow chip with white text could not be read. */
export function inkFor(hex: string): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return "#fff";
  const n = parseInt(m[1], 16);
  const [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((c) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return luminance > 0.4 ? "#141416" : "#ffffff";
}
