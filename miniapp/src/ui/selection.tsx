// Choosing from a list in place: a search field, rows that say what each item is, groups that can be
// taken whole, and the chosen set drawn as its own list underneath.
//
// It replaced a compact button that joined the chosen names with commas and hid the list in a popover.
// Past three models the button read "Grok 4.7, opencode/deepseek-v4.1-flash, codex/g…", the list had
// no search and said nothing about a model beyond its name, and taking ten of forty meant ten clicks
// in a menu that closed whenever the pointer strayed. Here the state is never behind a click: what is
// chosen is listed with a way to remove each, and what can be chosen is one search away.
//
// `SelectionList` takes several; `SelectionSingle` takes one, shown as its full row with the list a
// "Change" away. Both take plain items, so a model, a search backend or a language look alike to them;
// `modelitems.ts` turns the configured models into items with their provider, window and price.

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from "react";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { useLayer } from "./dialogs";
import "./selection.css";

export type SelectionItem = {
  id: string;
  label: string;
  /** The second line: what the item is beyond its name, such as a model's provider and id. */
  detail?: string;
  /** Short facts drawn as quiet tags: a context window, a price, "Vision". */
  facts?: string[];
  /** The id of the group the item sits under; items without one sit together at the top. */
  group?: string;
  /** A leading glyph, such as the provider's mark. */
  mark?: ReactNode;
  /** More words the search matches beyond the label, detail and facts. */
  keywords?: string;
  /** Not choosable now; a chosen one can still be removed. `hint` says why. */
  disabled?: boolean;
  hint?: string;
};

export type SelectionGroup = { id: string; label: string; mark?: ReactNode };

/** A setting that can follow a default: whether it does now, what the default gives, and the way back. */
export type InheritState = { active: boolean; label: string; detail?: string; onUse: () => void };

/** The items whose words contain every word of the query, in their own order. */
export function matchItems(items: SelectionItem[], query: string): SelectionItem[] {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return items;
  return items.filter((item) => {
    const text = [item.id, item.label, item.detail, item.keywords, ...(item.facts ?? [])].filter(Boolean).join(" ").toLocaleLowerCase();
    return words.every((word) => text.includes(word));
  });
}

/** `value` with every id in `ids` added (in the order given, after the ones already chosen) or taken out. */
export function setMany(value: string[], ids: string[], on: boolean): string[] {
  if (!on) return value.filter((id) => !ids.includes(id));
  return [...value, ...ids.filter((id) => !value.includes(id))];
}

/** The ids between two positions of the visible list, both ends included, in list order. */
export function rangeIds(visible: SelectionItem[], from: number, to: number): string[] {
  const [a, b] = from <= to ? [from, to] : [to, from];
  return visible.slice(Math.max(0, a), b + 1).map((item) => item.id);
}

/** The items laid out under their groups, groups in the order given and then in the order first met. */
function grouped(items: SelectionItem[], groups: SelectionGroup[] | undefined): { group: SelectionGroup | null; items: SelectionItem[] }[] {
  const order = new Map<string, SelectionGroup>();
  for (const g of groups ?? []) order.set(g.id, g);
  const buckets = new Map<string, SelectionItem[]>();
  for (const item of items) {
    const key = item.group ?? "";
    if (key && !order.has(key)) order.set(key, { id: key, label: key });
    buckets.set(key, [...(buckets.get(key) ?? []), item]);
  }
  const out: { group: SelectionGroup | null; items: SelectionItem[] }[] = [];
  if (buckets.has("")) out.push({ group: null, items: buckets.get("")! });
  for (const g of order.values()) if (buckets.has(g.id)) out.push({ group: g, items: buckets.get(g.id)! });
  return out;
}

function Facts({ facts }: { facts?: string[] }) {
  if (!facts?.length) return null;
  return <span className="sel-facts">{facts.map((fact) => <span key={fact} className="sel-fact">{fact}</span>)}</span>;
}

function RowText({ item, missing }: { item: SelectionItem; missing?: boolean }) {
  return (
    <span className="sel-text">
      <span className="sel-name">{item.label}</span>
      {(item.detail || item.hint || missing) && (
        <span className="sel-detail">
          {missing ? t("sel.missing") : [item.detail, item.disabled ? item.hint : ""].filter(Boolean).join(" — ")}
        </span>
      )}
    </span>
  );
}

/** Whether a setting follows its default, what the default gives, and the button back to it. */
export function InheritNote({ inherit, disabled }: { inherit: InheritState; disabled?: boolean }) {
  return (
    <div className={`sel-inherit ${inherit.active ? "on" : ""}`}>
      <Icon name={inherit.active ? "check" : "undo"} size={14} />
      <span className="sel-inherit-text">
        <b>{inherit.active ? t("sel.inherit.on") : t("sel.inherit.off")}</b>
        <span className="sub">{inherit.label}{inherit.detail ? ` — ${inherit.detail}` : ""}</span>
      </span>
      {!inherit.active && <button type="button" className="btn small" disabled={disabled} onClick={inherit.onUse}>{t("sel.inherit.use")}</button>}
    </div>
  );
}

function Search({ value, onChange, placeholder, label, onDown, controls }: { value: string; onChange: (v: string) => void; placeholder: string; label: string; onDown: () => void; controls: string }) {
  return (
    <div className="sel-search">
      <Icon name="search" size={14} />
      <input
        type="search"
        className="sel-search-field"
        value={value}
        placeholder={placeholder}
        aria-label={label}
        aria-controls={controls}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") { e.preventDefault(); onDown(); }
          // An Escape a layer above has already answered (an open single list) is not answered twice.
          if (e.key === "Escape" && !e.defaultPrevented && value) { e.preventDefault(); e.stopPropagation(); onChange(""); }
        }}
      />
    </div>
  );
}

/**
 * Whether the list is being focused by a press on it rather than by the keyboard.
 *
 * The list takes the keyboard itself (it holds `aria-activedescendant`), and on a focus from the
 * keyboard it marks a row so the arrows have somewhere to start: the current one, or the first. A
 * press on a row focuses the list too, and that answer then ran between the press and the release:
 * the marked row was scrolled into view, the list moved under the pointer, the release landed on
 * another row, and the browser sent the click to the list rather than to either row. With the search
 * field holding the keyboard, the first click on a model was lost and only a second one chose. A
 * press now leaves the mark where the click will put it. The flag is cleared by the click, not by the
 * release: a finger's focus arrives after it lifts.
 */
function usePress() {
  const pressing = useRef(false);
  const clear = () => { pressing.current = false; };
  return {
    pressing,
    handlers: { onPointerDown: () => { pressing.current = true; }, onPointerCancel: clear, onClickCapture: clear, onKeyDownCapture: clear },
  };
}

/**
 * Several of a list, chosen in place. `value` is the chosen ids in their order; every change hands
 * back the whole new list. `ordered` numbers the chosen list and lets it be reordered, for a choice
 * whose order means something (fallbacks tried in turn). An id in `value` that no item names any
 * more stays in the chosen list, marked missing, so it can be seen and removed rather than lingering
 * unseen in the configuration.
 */
export function SelectionList({ label, items, groups, value, onChange, ordered, chosenLabel, emptyChosen, placeholder, disabled, inherit, id }: {
  label: string;
  items: SelectionItem[];
  groups?: SelectionGroup[];
  value: string[];
  onChange: (next: string[]) => void;
  ordered?: boolean;
  chosenLabel?: string;
  emptyChosen?: string;
  placeholder?: string;
  disabled?: boolean;
  inherit?: InheritState;
  id?: string;
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  const anchor = useRef<number | null>(null);
  const list = useRef<HTMLDivElement>(null);
  const press = usePress();
  const base = useId();
  const listId = `${id ?? base}-list`;
  const visible = useMemo(() => matchItems(items, query), [items, query]);
  const sections = useMemo(() => grouped(visible, groups), [visible, groups]);
  // The flat order the keyboard and a shift-click walk, which is the drawn order, group by group.
  const flat = useMemo(() => sections.flatMap((s) => s.items), [sections]);
  const chosen = new Set(value);
  const takeable = (ids: string[]) => ids.filter((x) => !flat.find((item) => item.id === x)?.disabled || chosen.has(x));

  useEffect(() => { setActive((a) => Math.min(a, flat.length - 1)); }, [flat.length]);
  useEffect(() => {
    if (active < 0) return;
    // Optional call: a document without layout (the unit tests' jsdom) has no scrollIntoView.
    list.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView?.({ block: "nearest" });
  }, [active]);

  function change(next: string[]) { if (!disabled) onChange(next); }
  function toggle(index: number, shift: boolean) {
    const item = flat[index];
    if (!item) return;
    if (shift && anchor.current !== null && flat[anchor.current]) {
      // A range takes the state of the row it started from, the way a file manager extends a selection.
      const on = chosen.has(flat[anchor.current].id);
      change(setMany(value, takeable(rangeIds(flat, anchor.current, index)), on));
    } else {
      if (item.disabled && !chosen.has(item.id)) return;
      change(setMany(value, [item.id], !chosen.has(item.id)));
      anchor.current = index;
    }
    setActive(index);
  }
  function onRowClick(e: MouseEvent, index: number) {
    e.preventDefault();
    toggle(index, e.shiftKey);
  }
  function onListKey(e: KeyboardEvent<HTMLDivElement>) {
    const last = flat.length - 1;
    const move = (to: number) => {
      e.preventDefault();
      const next = Math.max(0, Math.min(last, to));
      if (e.shiftKey && active >= 0) {
        if (anchor.current === null) anchor.current = active;
        change(setMany(value, takeable(rangeIds(flat, anchor.current, next)), true));
      }
      setActive(next);
    };
    if (e.key === "ArrowDown") move(active + 1);
    else if (e.key === "ArrowUp") {
      if (active <= 0 && !e.shiftKey) { e.preventDefault(); setActive(-1); list.current?.parentElement?.querySelector<HTMLInputElement>(".sel-search-field")?.focus(); return; }
      move(active - 1);
    } else if (e.key === "Home") move(0);
    else if (e.key === "End") move(last);
    else if (e.key === " " || e.key === "Enter") { e.preventDefault(); if (active >= 0) toggle(active, e.shiftKey); }
    else if ((e.key === "a" || e.key === "A") && (e.ctrlKey || e.metaKey)) { e.preventDefault(); change(setMany(value, takeable(flat.map((i) => i.id)), true)); }
  }

  const shownIds = flat.map((item) => item.id);
  const shownChosen = shownIds.filter((x) => chosen.has(x)).length;
  const searching = query.trim().length > 0;
  const missing = value.filter((x) => !items.some((item) => item.id === x));
  const chosenItems = value.map((x) => items.find((item) => item.id === x) ?? { id: x, label: x });
  let index = -1;
  return (
    <div id={id} className={`sel ${disabled ? "disabled" : ""}`} role="group" aria-label={label} data-value={value.join(",")}>
      {inherit && <InheritNote inherit={inherit} disabled={disabled} />}
      <Search value={query} onChange={(v) => { setQuery(v); setActive(-1); }} placeholder={placeholder ?? t("sel.search")} label={t("sel.search.in", { what: label })} controls={listId}
        onDown={() => { list.current?.focus(); setActive(0); }} />
      <div className="sel-bar">
        <span className="sel-count" aria-live="polite">
          {searching ? t("sel.count.found", { n: flat.length, of: items.length }) : t("sel.count.chosen", { n: value.length - missing.length, of: items.length })}
        </span>
        <button type="button" className="linkbtn accent" disabled={disabled || shownChosen === takeable(shownIds).length} onClick={() => change(setMany(value, takeable(shownIds), true))}>
          {searching ? plural("sel.select.found", flat.length) : t("sel.select.all")}
        </button>
        <button type="button" className="linkbtn accent" disabled={disabled || shownChosen === 0} onClick={() => change(setMany(value, shownIds, false))}>
          {searching ? t("sel.clear.found") : t("sel.clear.all")}
        </button>
      </div>
      <div
        ref={list}
        id={listId}
        className="sel-list"
        role="listbox"
        aria-multiselectable="true"
        aria-label={label}
        aria-activedescendant={active >= 0 ? `${base}-o${active}` : undefined}
        tabIndex={0}
        onKeyDown={onListKey}
        {...press.handlers}
        onFocus={() => { if (!press.pressing.current && active < 0 && flat.length) setActive(0); }}
      >
        {flat.length === 0 && <div className="sel-empty sub">{searching ? t("sel.nothing", { query: query.trim() }) : t("sel.none")}</div>}
        {sections.map(({ group, items: rows }) => {
          const ids = rows.map((r) => r.id);
          const on = ids.filter((x) => chosen.has(x)).length;
          return (
            <div key={group?.id ?? ""} className="sel-section" role="presentation">
              {group && (
                <div className="sel-group" role="presentation">
                  {group.mark}
                  <span className="sel-group-name">{group.label}</span>
                  <span className="sel-group-count">{on ? t("sel.group.some", { n: on, of: ids.length }) : ids.length}</span>
                  <button type="button" className="linkbtn accent" tabIndex={-1} disabled={disabled} onClick={() => change(setMany(value, on === takeable(ids).length ? ids : takeable(ids), on !== takeable(ids).length))}
                    aria-label={on === takeable(ids).length ? t("sel.group.clear.of", { group: group.label }) : t("sel.group.select.of", { group: group.label })}>
                    {on === takeable(ids).length ? t("sel.group.clear") : t("sel.group.select")}
                  </button>
                </div>
              )}
              {rows.map((item) => {
                index += 1;
                const i = index;
                const isOn = chosen.has(item.id);
                return (
                  <div
                    key={item.id}
                    id={`${base}-o${i}`}
                    data-index={i}
                    data-id={item.id}
                    role="option"
                    aria-selected={isOn}
                    aria-disabled={item.disabled && !isOn ? true : undefined}
                    className={`sel-row ${isOn ? "on" : ""} ${i === active ? "active" : ""} ${item.disabled ? "off" : ""}`}
                    onMouseDown={(e) => { if (e.shiftKey) e.preventDefault(); }}
                    onClick={(e) => onRowClick(e, i)}
                  >
                    <span className="sel-check" aria-hidden>{isOn && <Icon name="check" size={12} />}</span>
                    {item.mark}
                    <RowText item={item} />
                    <Facts facts={item.facts} />
                  </div>
                );
              })}
            </div>
          );
        })}
      </div>
      <div className="sel-chosen">
        <div className="sel-chosen-head">
          <span>{chosenLabel ?? t("sel.chosen")}</span>
          <span className="sel-group-count">{value.length}</span>
          {ordered && value.length > 1 && <span className="sub faint">{t("sel.ordered")}</span>}
        </div>
        {value.length === 0
          ? <div className="sel-empty sub">{emptyChosen ?? t("sel.chosen.none")}</div>
          : (
            <ol className={`sel-chosen-list ${ordered ? "ordered" : ""}`}>
              {chosenItems.map((item, i) => (
                <li key={item.id} className={`sel-chip-row ${missing.includes(item.id) ? "missing" : ""}`} data-id={item.id}>
                  {ordered && <span className="sel-order">{i + 1}</span>}
                  {item.mark}
                  {/* Name and detail only: the facts are in the list above, and repeated here they
                      made the summary as tall as the list it summarises. */}
                  <RowText item={item} missing={missing.includes(item.id)} />
                  {ordered && (
                    <>
                      <button type="button" className="iconbtn small flat sel-move" disabled={disabled || i === 0} aria-label={t("sel.up", { name: item.label })} title={t("sel.up", { name: item.label })}
                        onClick={() => { const next = [...value]; [next[i - 1], next[i]] = [next[i], next[i - 1]]; change(next); }}>
                        <Icon name="up" size={14} />
                      </button>
                      <button type="button" className="iconbtn small flat sel-move" disabled={disabled || i === value.length - 1} aria-label={t("sel.down", { name: item.label })} title={t("sel.down", { name: item.label })}
                        onClick={() => { const next = [...value]; [next[i + 1], next[i]] = [next[i], next[i + 1]]; change(next); }}>
                        <Icon name="down" size={14} />
                      </button>
                    </>
                  )}
                  <button type="button" className="iconbtn small flat sel-remove" disabled={disabled} aria-label={t("sel.remove", { name: item.label })} title={t("sel.remove", { name: item.label })}
                    onClick={() => change(value.filter((x) => x !== item.id))}>
                    <Icon name="close" size={14} />
                  </button>
                </li>
              ))}
            </ol>
          )}
      </div>
    </div>
  );
}

/** An open list as a layer of its own: Escape closes the list before anything under it. */
function EscapeLayer({ onEscape }: { onEscape: () => void }) {
  useLayer(onEscape);
  return null;
}

/**
 * One of a list. The choice is drawn as its full row, with what it is and its facts, rather than a
 * name cut short in a button; "Change" opens the same searchable, grouped list in place, and picking
 * closes it. `defaultOption` adds a first row meaning "no choice of its own" (the value ""), for a
 * setting that follows something else until set.
 */
export function SelectionSingle({ label, items, groups, value, onChange, defaultOption, placeholder, disabled, id }: {
  label: string;
  items: SelectionItem[];
  groups?: SelectionGroup[];
  value: string;
  onChange: (next: string) => void;
  defaultOption?: { label: string; detail?: string };
  placeholder?: string;
  disabled?: boolean;
  id?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(-1);
  const list = useRef<HTMLDivElement>(null);
  const press = usePress();
  const changeButton = useRef<HTMLButtonElement>(null);
  const base = useId();
  const listId = `${id ?? base}-list`;
  const all: SelectionItem[] = useMemo(() => defaultOption ? [{ id: "", label: defaultOption.label, detail: defaultOption.detail }, ...items] : items, [items, defaultOption]);
  const visible = useMemo(() => matchItems(all, query), [all, query]);
  const sections = useMemo(() => grouped(visible, groups), [visible, groups]);
  const flat = useMemo(() => sections.flatMap((s) => s.items), [sections]);
  const current = all.find((item) => item.id === value);
  // Opening is for finding: the search field takes the focus, and ArrowDown goes on into the list.
  useEffect(() => {
    if (open) list.current?.parentElement?.querySelector<HTMLInputElement>(".sel-search-field")?.focus({ preventScroll: true });
  }, [open]);
  useEffect(() => {
    if (!open || active < 0) return;
    list.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView?.({ block: "nearest" });
  }, [open, active]);

  function close() { setOpen(false); setQuery(""); setActive(-1); requestAnimationFrame(() => changeButton.current?.focus()); }
  function pick(item: SelectionItem | undefined) {
    if (!item || item.disabled || disabled) return;
    if (item.id !== value) onChange(item.id);
    close();
  }
  function onListKey(e: KeyboardEvent<HTMLDivElement>) {
    const last = flat.length - 1;
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(Math.min(last, active + 1)); }
    else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (active <= 0) { setActive(-1); list.current?.parentElement?.querySelector<HTMLInputElement>(".sel-search-field")?.focus(); } else setActive(active - 1);
    } else if (e.key === "Home") { e.preventDefault(); setActive(0); }
    else if (e.key === "End") { e.preventDefault(); setActive(last); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(flat[active]); }
  }

  let index = -1;
  const shown = current ?? (value ? { id: value, label: value } : null);
  return (
    <div id={id} className={`sel single ${disabled ? "disabled" : ""}`} role="group" aria-label={label} data-value={value} data-missing={!!value && !current ? "" : undefined}>
      <div className={`sel-current ${shown && !current ? "missing" : ""}`}>
        {shown?.mark}
        {shown ? <RowText item={shown} missing={!current} /> : <span className="sel-text sub">{t("sel.single.none")}</span>}
        <Facts facts={shown?.facts} />
        <button ref={changeButton} type="button" className="btn small" disabled={disabled} aria-expanded={open} aria-controls={open ? listId : undefined}
          onClick={() => (open ? close() : setOpen(true))}>
          {open ? t("sel.single.done") : t("sel.single.change")}
        </button>
      </div>
      {open && (
        <>
          {/* Escape empties the search first and then closes the list. It is a layer rather than a key
              handler on the field: the sheet that holds Settings on a phone listens for Escape before
              the field hears it, and closed the whole of Settings with the list still open. */}
          <EscapeLayer onEscape={() => { if (query) { setQuery(""); setActive(-1); } else close(); }} />
          <Search value={query} onChange={(v) => { setQuery(v); setActive(v.trim() ? 0 : -1); }} placeholder={placeholder ?? t("sel.search")} label={t("sel.search.in", { what: label })} controls={listId}
            onDown={() => { list.current?.focus(); setActive(0); }} />
          <div ref={list} id={listId} className="sel-list" role="listbox" aria-label={label} tabIndex={0}
            aria-activedescendant={active >= 0 ? `${base}-o${active}` : undefined}
            onKeyDown={onListKey}
            {...press.handlers}
            onFocus={() => { if (!press.pressing.current && active < 0 && flat.length) setActive(Math.max(0, flat.findIndex((item) => item.id === value))); }}>
            {flat.length === 0 && <div className="sel-empty sub">{t("sel.nothing", { query: query.trim() })}</div>}
            {sections.map(({ group, items: rows }) => (
              <div key={group?.id ?? ""} className="sel-section" role="presentation">
                {group && (
                  <div className="sel-group" role="presentation">
                    {group.mark}
                    <span className="sel-group-name">{group.label}</span>
                    <span className="sel-group-count">{rows.length}</span>
                  </div>
                )}
                {rows.map((item) => {
                  index += 1;
                  const i = index;
                  const isOn = item.id === value;
                  return (
                    <div key={item.id || "-default"} id={`${base}-o${i}`} data-index={i} data-id={item.id} role="option" aria-selected={isOn}
                      aria-disabled={item.disabled ? true : undefined}
                      className={`sel-row sel-radio ${isOn ? "on" : ""} ${i === active ? "active" : ""} ${item.disabled ? "off" : ""} ${item.id === "" ? "default" : ""}`}
                      onClick={() => pick(item)}>
                      <span className="sel-check" aria-hidden>{isOn && <span className="sel-dot" />}</span>
                      {item.mark}
                      <RowText item={item} />
                      <Facts facts={item.facts} />
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
