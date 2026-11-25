// The settings index is one table read three ways — the Settings page's sections and search, and the
// agent's documentation search — so what it claims about the page has to be true of the page: each
// section is one the page draws, under the title the page gives it, and each field's anchor is a row
// the page carries, or a link the agent hands out would open the section and find nothing to show.

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { DICT } from "../i18n";
import { ICON_NAMES } from "../icons";
import { INDEX_FIELDS, INDEX_SECTIONS, SECTION_GROUPS, fieldPath, matchFields, sectionOfAnchor } from "./settingsindex";

const SOURCES = import.meta.glob("../**/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const SETTINGS = SOURCES["./Settings.tsx"] ?? SOURCES["../screens/Settings.tsx"];

describe("the settings index", () => {
  it("names each section by the title the page gives it, in both languages", () => {
    for (const section of INDEX_SECTIONS) {
      const words = DICT[`settings.sec.${section.id}`];
      expect(words, section.id).toBeDefined();
      expect(section.title, section.id).toEqual({ en: words.en, ru: words.ru });
    }
  });

  it("gives every section a line about it, a hint in the list, an icon of the set and words to find it by", () => {
    for (const section of INDEX_SECTIONS) {
      expect(section.about.en && section.about.ru, section.id).toBeTruthy();
      expect(DICT[`settings.sec.${section.id}.hint`], section.id).toBeDefined();
      expect(ICON_NAMES, section.id).toContain(section.icon);
      expect(section.synonyms.length, section.id).toBeGreaterThan(0);
    }
  });

  it("holds every section the page draws a body for, once, grouped as the page lists them", () => {
    const ids = INDEX_SECTIONS.map((section) => section.id);
    expect(new Set(ids).size).toBe(ids.length);
    const drawn = [...SETTINGS.matchAll(/case "([a-z-]+)":/g)].map((m) => m[1]).filter((id) => id !== "language");
    for (const id of drawn) expect(ids, id).toContain(id);
    expect(SECTION_GROUPS.map((group) => group.id)).toEqual(["you", "models", "agent", "tools", "integrations", "system"]);
    expect(SECTION_GROUPS.flatMap((group) => group.sections.map((section) => section.id)).sort()).toEqual([...ids].sort());
    for (const group of SECTION_GROUPS) expect(DICT[`settings.group.${group.id}`], group.id).toBeDefined();
  });

  // One section, one subject: the operator asked for many short sections over a few long ones. A
  // section that collects more than ten rows has become two subjects again. Web search is the one
  // exception, and only on paper: most of its rows belong to one engine each and show only with it.
  it("keeps each section to one subject of a handful of rows", () => {
    for (const section of INDEX_SECTIONS) {
      if (section.id === "search") continue;
      const rows = INDEX_FIELDS.filter((field) => field.section === section.id).length;
      expect(rows, section.id).toBeLessThanOrEqual(10);
    }
  });

  it("anchors every field to a row the page carries", () => {
    const text = Object.values(SOURCES).join("\n");
    const anchors = INDEX_FIELDS.map((field) => field.anchor);
    expect(new Set(anchors).size).toBe(anchors.length);
    for (const field of INDEX_FIELDS) {
      expect(INDEX_SECTIONS.some((section) => section.id === field.section), field.anchor).toBe(true);
      const carried = text.includes(`setting="${field.anchor}"`) || text.includes(`setting: "${field.anchor}"`);
      expect(carried, `no row carries data-setting="${field.anchor}"`).toBe(true);
      expect(field.title.en && field.title.ru && field.about.en && field.about.ru, field.anchor).toBeTruthy();
    }
  });

  it("gives a field the address of its row", () => {
    const stt = INDEX_FIELDS.find((field) => field.anchor === "asr.transcriber")!;
    expect(fieldPath(stt)).toBe("/app/settings/dictation#asr.transcriber");
    const keys = INDEX_FIELDS.find((field) => field.anchor === "providers.list")!;
    expect(fieldPath(keys)).toBe("/app/settings/providers#providers.list");
  });

  // An address written before a row moved names the old section; the page asks this where the row is
  // now, so the bookmark still opens on it.
  it("knows the section of every row it lists, and of the rows drawn once per model, provider or account", () => {
    for (const field of INDEX_FIELDS) expect(sectionOfAnchor(field.anchor), field.anchor).toBe(field.section);
    expect(sectionOfAnchor("presets.main.images")).toBe("models");
    expect(sectionOfAnchor("providers.openrouter.api_key")).toBe("providers");
    expect(sectionOfAnchor("limits.usd_total_per_provider.openrouter")).toBe("budget");
    expect(sectionOfAnchor("accounts.claude.fallover")).toBe("accounts");
    expect(sectionOfAnchor("keyproxy.clients")).toBe("keyproxy");
    expect(sectionOfAnchor("m12")).toBeNull();
    expect(sectionOfAnchor("terminal-sessions")).toBeNull();
  });

  it("finds a row by a name that is not its title, in either language", () => {
    expect(matchFields("STT").map((field) => field.anchor)).toContain("asr.transcriber");
    expect(matchFields("диктовка").map((field) => field.anchor)).toContain("asr.transcriber");
    expect(matchFields("quiet hours", "notifications").map((field) => field.anchor)).toEqual(["notifications.quiet_hours"]);
    expect(matchFields("   ")).toEqual([]);
  });

  // Translation was once a card at the foot of another section, and typing "translate" found nothing:
  // "translation" does not contain "translate", so each row names the words outright.
  it("finds every row of Translation, and the section, by any word for it in either language", () => {
    const rows = INDEX_FIELDS.filter((field) => field.section === "translation").map((field) => field.anchor).sort();
    expect(rows).toEqual(["answer_language", "translation.agent_language", "translation.blocking_timeout_seconds", "translation.fallback_preset", "translation.operator_language", "translation.preset", "translation.timeout_seconds"]);
    const section = INDEX_SECTIONS.find((item) => item.id === "translation")!;
    for (const word of ["translate", "translation", "translator", "language", "перевод", "переводчик", "язык"]) {
      expect(matchFields(word, "translation").map((field) => field.anchor).sort(), word).toEqual(rows);
      expect([section.title.en, section.title.ru, ...section.synonyms].join("\n").toLowerCase(), word).toContain(word);
    }
  });
});
