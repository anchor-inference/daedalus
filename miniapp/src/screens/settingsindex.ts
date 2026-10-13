// The settings index: every section of Settings and the fields worth finding in it, each with its
// titles in both languages, the words people use for it, and the row it is anchored to.
//
// It is one table for three readers. The Settings page takes its sections from it; its search box
// finds a field by any of its names, not only a section by its title; and the host serves the same
// file to the agent's documentation search, which answers "how do I turn on dictation" with the
// address of the row rather than a guess at a page. A field's `anchor` is the `data-setting` its row
// carries, and its address is `/app/settings/<section>#<anchor>`.

import index from "./settings-index.json";
import type { IconName } from "../icons";
import type { Lang } from "../i18n";

export type SectionGroup = "you" | "work" | "system";
type Words = Record<Lang, string>;

export type IndexSection = { id: string; group: SectionGroup; icon: IconName; title: Words; about: Words; synonyms: string[] };
export type IndexField = { section: string; anchor: string; config: string | null; tab?: string; title: Words; about: Words; synonyms: string[] };

export const INDEX_SECTIONS = index.sections as IndexSection[];
export const INDEX_FIELDS = index.fields as IndexField[];

const ORDER: SectionGroup[] = ["you", "work", "system"];

/** The sections grouped the way the page lists them, in the order the table gives. */
export const SECTION_GROUPS: { id: SectionGroup; sections: { id: string; icon: IconName }[] }[] = ORDER.map((group) => ({
  id: group,
  sections: INDEX_SECTIONS.filter((section) => section.group === group).map(({ id, icon }) => ({ id, icon })),
}));

/** The address that opens a field's section, on its tab when it has one, and names its row. */
export function fieldPath(field: IndexField): string {
  const tab = field.tab ? `?tab=${encodeURIComponent(field.tab)}` : "";
  return `/app/settings/${encodeURIComponent(field.section)}${tab}#${field.anchor}`;
}

/** The fields of the page's search box: those whose title or any name contains what was typed, in either
 *  language, because a reader of the Russian page may well type "STT". */
export function matchFields(needle: string, sectionId?: string): IndexField[] {
  const want = needle.trim().toLowerCase();
  if (!want) return [];
  return INDEX_FIELDS.filter((field) => {
    if (sectionId && field.section !== sectionId) return false;
    const words = [field.title.en, field.title.ru, ...field.synonyms].join("\n").toLowerCase();
    return words.includes(want);
  });
}
