// A list narrowed by what is typed: the model search, the slash palettes, the command palette, Quick
// Chat's pickers. The caller owns the query and the narrowing (each list matches in its own way, the
// models by provider and alias, the commands by their words in two languages), Base UI the keyboard:
// the arrows walk the matches with the cursor still in the field, Enter takes the highlighted one.
// `inline` draws the list in place, under the field, for a palette that is itself the popup.

import { Combobox as Base } from "@base-ui/react/combobox";
import { useState, type ReactNode } from "react";
import { closeHandler, useLayer } from "./layers";

/** `note` is a short word at the label's far end: the state of the search that fills the group. */
export type ComboboxGroup<T> = { label: string; items: T[]; note?: ReactNode };

export type ComboboxProps<T> = {
  items: T[];
  /** The items under labels instead (then `items` is ignored). */
  groups?: ComboboxGroup<T>[];
  value?: T | null;
  /** The press or key that chose it comes along, so a caller can read Ctrl or Shift on it. */
  onChoose: (item: T, event?: Event) => void;
  query: string;
  onQuery: (q: string) => void;
  label: string;
  placeholder?: string;
  /** How a row draws its item; its words otherwise. */
  render?: (item: T) => ReactNode;
  /** An item's words, for the field and for typing to an item. */
  toLabel?: (item: T) => string;
  empty?: ReactNode;
  inline?: boolean;
  className?: string;
  /** Added to every row's own class. */
  itemClassName?: string;
  /** Drawn in the field's row, before the field and after it: a glyph, the key that closes. */
  lead?: ReactNode;
  trail?: ReactNode;
  autoFocus?: boolean;
};

export function Combobox<T>({ items, groups, value = null, onChoose, query, onQuery, label, placeholder, render, toLabel = String, empty, inline, className = "", itemClassName = "", lead, trail, autoFocus }: ComboboxProps<T>) {
  const [open, setOpen] = useState(false);
  const isTop = useLayer(() => setOpen(false), open && !inline);
  const close = closeHandler(isTop, () => setOpen(false));
  const shown = groups ?? items;
  const row = (item: T) => (
    <Base.Item key={toLabel(item)} value={item} className={`combo-item ${itemClassName}`.trim()}>
      {render ? render(item) : toLabel(item)}
    </Base.Item>
  );
  const list = (
    <Base.List className="combo-list">
      {groups
        ? (group: ComboboxGroup<T>) => (
          <Base.Group key={group.label} items={group.items} className="combo-group">
            <Base.GroupLabel className="combo-group-label">{group.label}{group.note && <span className="combo-group-note">{group.note}</span>}</Base.GroupLabel>
            <Base.Collection>{(item: T) => row(item)}</Base.Collection>
          </Base.Group>
        )
        : (item: T) => row(item)}
    </Base.List>
  );
  return (
    <Base.Root
      items={shown}
      filteredItems={shown}
      filter={null}
      value={value}
      onValueChange={(next, details) => { if (next !== null && next !== undefined) onChoose(next as T, details.event); }}
      inputValue={query}
      // Choosing a row writes the row's words into the field; the caller owns the query and a
      // chosen row is not a new question.
      onInputValueChange={(next, details) => { if (details.reason !== "item-press") onQuery(next); }}
      itemToStringLabel={(item) => toLabel(item as T)}
      autoHighlight
      inline={inline}
      open={inline ? true : open}
      onOpenChange={inline ? undefined : (next, details) => (next ? setOpen(true) : close(next, details))}
    >
      <div className={`combo ${inline ? "inline" : ""} ${className}`.replace(/\s+/g, " ").trim()}>
        {lead || trail ? (
          <div className="combo-bar">
            {lead}
            <Base.Input className="field combo-input" aria-label={label} placeholder={placeholder} autoFocus={autoFocus} />
            {trail}
          </div>
        ) : (
          <Base.Input className="field combo-input" aria-label={label} placeholder={placeholder} autoFocus={autoFocus} />
        )}
        {inline ? (
          <>
            {list}
            {empty && <Base.Empty className="combo-empty">{empty}</Base.Empty>}
          </>
        ) : (
          <Base.Portal>
            <Base.Positioner className="layer-pos combo-pos" side="bottom" align="start" sideOffset={4} collisionPadding={8}>
              <Base.Popup className="menu combo-popup" aria-label={label}>
                {list}
                {empty && <Base.Empty className="combo-empty">{empty}</Base.Empty>}
              </Base.Popup>
            </Base.Positioner>
          </Base.Portal>
        )}
      </div>
    </Base.Root>
  );
}
