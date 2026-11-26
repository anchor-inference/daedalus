// The two lists a note offers at the caret: the files to show (`[[`) and the blocks to insert (`/`).
// Each is a Combobox in a popover hung from the caret. The words typed after the trigger seed its
// field; from there on the field is the list's own, so Escape gives the focus back to the note with
// what was typed left where it was, and a choice replaces the trigger and everything after it.

import { useMemo, useState } from "react";
import { Icon, type IconName } from "../icons";
import { t } from "../i18n";
import { Popover } from "../ui/dialogs";
import { Combobox, type ComboboxGroup, type VirtualAnchor } from "../ui/primitives";
import { FileGlyph } from "./FileGlyph";
import { caretPoint } from "./textarea";
import type { NoteFile } from "./model";

export type PickGroup = { label: string; files: NoteFile[] };
export type PickItem =
  | { kind: "file"; file: NoteFile; group: string }
  | { kind: "upload" }
  | { kind: "command"; id: string; icon: IconName; label: string; hint: string; run: () => void };

/** The most files listed at once: a project that kept hundreds narrows by typing. */
const FILES_SHOWN = 40;

export function pickItems(kind: "file" | "slash", query: string, groups: PickGroup[], commands: PickItem[]): PickItem[] {
  const q = query.toLowerCase();
  if (kind === "file") {
    const found: PickItem[] = [];
    for (const group of groups) for (const file of group.files) if (!q || file.name.toLowerCase().includes(q)) found.push({ kind: "file", file, group: group.label });
    return [...found.slice(0, FILES_SHOWN), { kind: "upload" }];
  }
  return commands.filter((item) => item.kind === "command" && (!q || item.id.includes(q) || item.label.toLowerCase().includes(q)));
}

const keyOf = (item: PickItem) => (item.kind === "file" ? `file:${item.file.id}` : item.kind === "command" ? `cmd:${item.id}` : "upload");

export type NotePickerProps = {
  kind: "file" | "slash";
  field: HTMLTextAreaElement;
  /** Where the trigger starts in the field's text. */
  from: number;
  /** What was typed after the trigger before the list took the keyboard. */
  query: string;
  groups: PickGroup[];
  commands: PickItem[];
  onChoose: (item: PickItem) => void;
  onClose: () => void;
};

export function NotePicker({ kind, field, from, query: seed, groups, commands, onChoose, onClose }: NotePickerProps) {
  const [query, setQuery] = useState(seed);
  const items = useMemo(() => pickItems(kind, query, groups, commands), [kind, query, groups, commands]);
  // The popover stands under the character that opened the list, as the menu always did.
  const anchor = useMemo<VirtualAnchor>(() => ({
    contextElement: field,
    getBoundingClientRect: () => {
      const box = field.getBoundingClientRect();
      const point = caretPoint(field, from);
      const left = box.left + point.left;
      const top = box.top + point.top;
      return { x: left, y: top, left, top, width: 1, height: point.height, right: left + 1, bottom: top + point.height };
    },
  }), [field, from]);
  const label = kind === "file" ? t("notes.picker") : t("notes.slash");
  // Files are listed under the scope they belong to, blocks in one run.
  const grouped: ComboboxGroup<PickItem>[] | undefined = kind === "file"
    ? [...groups.map((group) => ({ label: group.label, items: items.filter((item) => item.kind === "file" && item.group === group.label) })).filter((g) => g.items.length), { label: t("notes.picker.new"), note: items.some((i) => i.kind === "file") ? undefined : t("notes.picker.none"), items: items.filter((i) => i.kind === "upload") }]
    : undefined;
  return (
    <Popover anchor={anchor} onClose={onClose} label={label} role="dialog" width={320} side="bottom" className="ne-picker">
      <Combobox<PickItem>
        inline
        autoFocus
        items={items}
        groups={grouped}
        query={query}
        onQuery={setQuery}
        label={label}
        placeholder={kind === "file" ? t("notes.picker.search") : t("notes.slash.search")}
        toLabel={(item) => keyOf(item)}
        render={(item) => (item.kind === "file"
          ? <><FileGlyph file={item.file} size="sm" /><span className="ne-pick-label">{item.file.name}</span></>
          : item.kind === "upload"
            ? <><span className="ne-pick-ico"><Icon name="plus" size={16} /></span><span className="ne-pick-label">{t("notes.picker.upload")}</span></>
            : <><span className="ne-pick-ico"><Icon name={item.icon} size={16} /></span><span className="ne-pick-label">{item.label}</span>{item.hint && <kbd>{item.hint}</kbd>}</>)}
        empty={t("notes.slash.none")}
        onChoose={onChoose}
      />
    </Popover>
  );
}
