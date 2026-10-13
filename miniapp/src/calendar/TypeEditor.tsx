// The kinds of event: the agent makes most of them on the fly (a meal, an order, a workout), and the
// reader can make and change them here — a name, an icon, a colour, the fields an event of the kind
// carries, the blocks a new one starts with, the chip the calendar shows for it and the totals the
// agenda keeps under each day.

import { useState, type CSSProperties } from "react";
import { t } from "../i18n";
import { Icon, type IconName } from "../icons";
import { invalidate } from "../store";
import { confirmAsync, errorText } from "../ui";
import { Sheet } from "../ui/dialogs";
import { ADDABLE_KINDS, type FieldSchema, type TypeAggregate, type TypeDef } from "./blocks";
import { TYPES, deleteType, refreshPlanner, saveType } from "./data";
import { ColorPicker } from "./fields";
import { TypeIcon } from "./structured";

const ICONS: IconName[] = ["utensils", "cart", "check", "list", "clock", "loop", "dumbbell", "plane", "home", "heart", "book", "calendar", "bell", "flag", "pin", "user", "chart", "journal"];
const FIELD_TYPES: FieldSchema["type"][] = ["number", "integer", "money", "string", "enum", "boolean", "date"];

type FieldRow = { key: string; schema: FieldSchema };

function slug(text: string): string {
  const base = text.toLowerCase().normalize("NFKD").replace(/[^\p{L}\p{N}]+/gu, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  return /^[a-z0-9-]+$/.test(base) && base ? base : `type-${Date.now().toString(36)}`;
}

export function TypesSheet({ types, onClose, toast }: { types: TypeDef[]; onClose: () => void; toast: (text: string) => void }) {
  const [editing, setEditing] = useState<TypeDef | "new" | null>(null);
  if (editing) return <TypeForm type={editing === "new" ? null : editing} taken={types.map((x) => x.id)} onClose={() => setEditing(null)} toast={toast} />;
  return (
    <Sheet title={t("cal.types")} onClose={onClose} size="narrow" className="cal-types">
      <p className="cal-hint">{t("cal.types.hint")}</p>
      <ul className="cal-type-list">
        {types.map((type) => (
          <li key={type.id}>
            <button type="button" className="cal-type-row" onClick={() => setEditing(type)}>
              <TypeIcon icon={type.icon} color={type.color} size={16} />
              <span className="cal-type-name"><b>{type.name}</b><small>{[type.owner?.startsWith("agent") ? t("cal.type.by.agent") : t("cal.type.by.you"), Object.keys(type.fields_schema ?? {}).length ? t("cal.type.fields.n", { n: Object.keys(type.fields_schema ?? {}).length }) : ""].filter(Boolean).join(" · ")}</small></span>
              <Icon name="forward" size={14} />
            </button>
          </li>
        ))}
      </ul>
      <div className="sheet-foot cal-foot">
        <span className="cal-spacer" />
        <button type="button" className="btn primary" onClick={() => setEditing("new")}><Icon name="plus" size={14} /> {t("cal.type.new")}</button>
      </div>
    </Sheet>
  );
}

function TypeForm({ type, taken, onClose, toast }: { type: TypeDef | null; taken: string[]; onClose: () => void; toast: (text: string) => void }) {
  const [name, setName] = useState(type?.name ?? "");
  const [icon, setIcon] = useState(type?.icon ?? "list");
  const [color, setColor] = useState(type?.color ?? "#4f8ff7");
  const [fields, setFields] = useState<FieldRow[]>(Object.entries(type?.fields_schema ?? {}).map(([key, schema]) => ({ key, schema })));
  const [blocks, setBlocks] = useState<{ kind: string; title?: string; source?: string }[]>((type?.default_blocks ?? []).map((b) => ({ kind: b.kind ?? "markdown", title: b.title, source: (b as { source?: string }).source })));
  const [chip, setChip] = useState(type?.display?.chip ?? "");
  const [aggregates, setAggregates] = useState<TypeAggregate[]>(type?.aggregates ?? []);
  const [quick, setQuick] = useState<string[]>(type?.quick_actions ?? ["mark_done", "skip"]);
  const [busy, setBusy] = useState(false);
  const numeric = fields.filter((f) => f.schema.type === "number" || f.schema.type === "integer" || f.schema.type === "money");

  async function save(e: React.FormEvent) {
    e.preventDefault();
    const id = type?.id ?? (() => { let base = slug(name); while (taken.includes(base)) base = `${base}-2`; return base; })();
    const keys = fields.map((f) => f.key.trim()).filter(Boolean);
    if (new Set(keys).size !== keys.length) { toast(t("cal.type.fields.duplicate")); return; }
    const next: TypeDef = {
      ...(type ?? {}),
      id, name: name.trim(), icon, color,
      fields_schema: Object.fromEntries(fields.filter((f) => f.key.trim()).map((f) => [f.key.trim(), f.schema])),
      default_blocks: blocks.map((b) => ({ kind: b.kind, ...(b.title ? { title: b.title } : {}), ...(b.source ? { source: "fields" as const } : {}) })),
      display: { ...(type?.display ?? {}), chip: chip.trim() || undefined },
      aggregates: aggregates.filter((a) => a.field),
      quick_actions: quick,
      owner: type?.owner ?? "user",
    };
    setBusy(true);
    try {
      await saveType(next);
      invalidate(TYPES);
      refreshPlanner();
      toast(t("cal.type.saved"));
      onClose();
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  async function remove() {
    if (!type) return;
    if (!(await confirmAsync(t("cal.type.delete.title", { name: type.name }), { body: t("cal.type.delete.body"), action: t("common.delete"), danger: true }))) return;
    try {
      await deleteType(type.id);
      invalidate(TYPES);
      refreshPlanner();
      toast(t("cal.type.deleted"));
      onClose();
    } catch (exc) { toast(errorText(exc)); }
  }

  const setField = (i: number, patch: Partial<FieldRow> | Partial<FieldSchema>, schema = false) => setFields(fields.map((f, j) => (j === i ? (schema ? { ...f, schema: { ...f.schema, ...(patch as Partial<FieldSchema>) } } : { ...f, ...(patch as Partial<FieldRow>) }) : f)));

  return (
    <Sheet title={type ? t("cal.type.edit") : t("cal.type.new")} onClose={onClose} className="cal-editor cal-type-form">
      <form className="cal-form" onSubmit={save}>
        <div className="cal-type-top" style={{ "--c": color } as CSSProperties}>
          <TypeIcon icon={icon} color={color} size={20} className="cal-type-preview" />
          <input className="field cal-title-input" required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} placeholder={t("cal.type.name")} aria-label={t("cal.type.name")} autoFocus={!type} />
        </div>
        <div className="cal-section-label">{t("cal.type.icon")}</div>
        <div className="cal-icon-grid" role="radiogroup" aria-label={t("cal.type.icon")}>
          {ICONS.map((name) => (
            <button key={name} type="button" role="radio" aria-checked={icon === name} className={`cal-icon-pick ${icon === name ? "on" : ""}`} style={{ "--c": color } as CSSProperties} onClick={() => setIcon(name)} aria-label={name}>
              <Icon name={name} size={16} />
            </button>
          ))}
          <input className="field cal-emoji" value={ICONS.includes(icon as IconName) ? "" : icon} maxLength={4} placeholder="🙂" aria-label={t("cal.type.emoji")} onChange={(e) => e.target.value && setIcon(e.target.value)} />
        </div>
        <div className="cal-section-label">{t("cal.field.color")}</div>
        <ColorPicker value={color} onChange={(hex) => hex && setColor(hex)} label={t("cal.field.color")} />

        <div className="cal-section-label">{t("cal.type.fields")}</div>
        <div className="cal-bedit-items">
          {fields.map((f, i) => (
            <div key={i} className="cal-type-field">
              <input className="field" value={f.key} placeholder={t("cal.type.field.key")} aria-label={t("cal.type.field.key.n", { n: i + 1 })} onChange={(e) => setField(i, { key: e.target.value.replace(/[^\w-]/g, "_") })} />
              <input className="field" value={f.schema.label ?? ""} placeholder={t("cal.type.field.label")} aria-label={t("cal.type.field.label.n", { n: i + 1 })} onChange={(e) => setField(i, { label: e.target.value || undefined }, true)} />
              <select className="field" value={f.schema.type} aria-label={t("cal.type.field.type.n", { n: i + 1 })} onChange={(e) => setField(i, { type: e.target.value as FieldSchema["type"] }, true)}>
                {FIELD_TYPES.map((x) => <option key={x} value={x}>{t(`cal.type.ftype.${x}`)}</option>)}
              </select>
              {f.schema.type === "enum" ? (
                <input className="field" value={(f.schema.values ?? []).join(", ")} placeholder={t("cal.type.field.values")} aria-label={t("cal.type.field.values.n", { n: i + 1 })} onChange={(e) => setField(i, { values: e.target.value.split(",").map((v) => v.trim()).filter(Boolean) }, true)} />
              ) : (
                <input className="field" value={f.schema.unit ?? ""} placeholder={t("cal.block.unit")} aria-label={t("cal.type.field.unit.n", { n: i + 1 })} onChange={(e) => setField(i, { unit: e.target.value || undefined }, true)} />
              )}
              <button type="button" className="iconbtn small" onClick={() => setFields(fields.filter((_, j) => j !== i))} aria-label={t("cal.type.field.remove", { n: i + 1 })} title={t("common.delete")}><Icon name="close" size={14} /></button>
            </div>
          ))}
          <button type="button" className="btn small ghost" onClick={() => setFields([...fields, { key: "", schema: { type: "number" } }])}><Icon name="plus" size={14} /> {t("cal.type.field.add")}</button>
        </div>

        <div className="cal-section-label">{t("cal.type.blocks")}</div>
        <div className="cal-bedit-items">
          {blocks.map((b, i) => (
            <div key={i} className="cal-type-block">
              <select className="field" value={b.source ? "fields:source" : b.kind} aria-label={t("cal.type.block.kind.n", { n: i + 1 })} onChange={(e) => setBlocks(blocks.map((x, j) => (j === i ? (e.target.value === "fields:source" ? { kind: "fields", source: "fields", title: x.title } : { kind: e.target.value, title: x.title }) : x)))}>
                <option value="fields:source">{t("cal.type.block.fields")}</option>
                {ADDABLE_KINDS.map((k) => <option key={k} value={k}>{t(`cal.block.kind.${k}`)}</option>)}
              </select>
              <input className="field" value={b.title ?? ""} placeholder={t("cal.block.title")} aria-label={t("cal.type.block.title.n", { n: i + 1 })} onChange={(e) => setBlocks(blocks.map((x, j) => (j === i ? { ...x, title: e.target.value || undefined } : x)))} />
              <button type="button" className="iconbtn small" disabled={i === 0} onClick={() => { const next = [...blocks]; [next[i - 1], next[i]] = [next[i], next[i - 1]]; setBlocks(next); }} aria-label={t("cal.block.up")}><Icon name="up" size={14} /></button>
              <button type="button" className="iconbtn small" onClick={() => setBlocks(blocks.filter((_, j) => j !== i))} aria-label={t("cal.type.block.remove", { n: i + 1 })}><Icon name="close" size={14} /></button>
            </div>
          ))}
          <button type="button" className="btn small ghost" onClick={() => setBlocks([...blocks, { kind: "checklist" }])}><Icon name="plus" size={14} /> {t("cal.block.add")}</button>
        </div>

        <label className="cal-section-label" htmlFor="cal-type-chip">{t("cal.type.chip")}</label>
        <input id="cal-type-chip" className="field" value={chip} maxLength={120} placeholder={fields.length ? `{${fields[0].key || "field"}} · {title}` : "{title}"} onChange={(e) => setChip(e.target.value)} />
        <p className="cal-hint">{t("cal.type.chip.hint")}</p>

        {numeric.length > 0 && (
          <>
            <div className="cal-section-label">{t("cal.type.totals")}</div>
            <div className="cal-bedit-items">
              {aggregates.map((a, i) => (
                <div key={i} className="cal-type-agg">
                  <select className="field" value={a.op} aria-label={t("cal.type.total.op.n", { n: i + 1 })} onChange={(e) => setAggregates(aggregates.map((x, j) => (j === i ? { ...x, op: e.target.value as TypeAggregate["op"] } : x)))}>
                    <option value="sum">{t("cal.block.total.sum")}</option>
                    <option value="avg">{t("cal.block.total.avg")}</option>
                    <option value="count">{t("cal.type.total.count")}</option>
                  </select>
                  <select className="field" value={a.field} aria-label={t("cal.type.total.field.n", { n: i + 1 })} onChange={(e) => setAggregates(aggregates.map((x, j) => (j === i ? { ...x, field: e.target.value } : x)))}>
                    {numeric.map((f) => <option key={f.key} value={f.key}>{f.schema.label || f.key}</option>)}
                  </select>
                  <input className="field" type="number" value={a.target?.[0] ?? ""} placeholder={t("cal.type.total.min")} aria-label={t("cal.type.total.min")} onChange={(e) => setAggregates(aggregates.map((x, j) => (j === i ? { ...x, target: e.target.value === "" ? undefined : [Number(e.target.value), x.target?.[1] ?? Number(e.target.value)] } : x)))} />
                  <input className="field" type="number" value={a.target?.[1] ?? ""} placeholder={t("cal.type.total.max")} aria-label={t("cal.type.total.max")} onChange={(e) => setAggregates(aggregates.map((x, j) => (j === i ? { ...x, target: e.target.value === "" ? undefined : [x.target?.[0] ?? 0, Number(e.target.value)] } : x)))} />
                  <button type="button" className="iconbtn small" onClick={() => setAggregates(aggregates.filter((_, j) => j !== i))} aria-label={t("cal.type.total.remove", { n: i + 1 })}><Icon name="close" size={14} /></button>
                </div>
              ))}
              <button type="button" className="btn small ghost" onClick={() => setAggregates([...aggregates, { field: numeric[0].key, op: "sum", per: "day", label: numeric[0].schema.label }])}><Icon name="plus" size={14} /> {t("cal.type.total.add")}</button>
            </div>
          </>
        )}

        <div className="cal-section-label">{t("cal.type.swipes")}</div>
        <div className="cal-type-quick">
          {(["mark_done", "skip"] as const).map((q) => (
            <label key={q} className="cal-switch-row">
              <input type="checkbox" checked={quick.includes(q)} onChange={(e) => setQuick(e.target.checked ? [...quick, q] : quick.filter((x) => x !== q))} />
              <span>{t(`cal.type.quick.${q}`)}</span>
            </label>
          ))}
        </div>

        <div className="sheet-foot cal-foot">
          {type && <button type="button" className="btn danger" onClick={() => void remove()}><Icon name="trash" size={14} /> {t("common.delete")}</button>}
          <span className="cal-spacer" />
          <button type="button" className="btn" onClick={onClose}>{t("common.cancel")}</button>
          <button type="submit" className="btn primary" disabled={busy || !name.trim()}>{t("common.save")}</button>
        </div>
      </form>
    </Sheet>
  );
}
