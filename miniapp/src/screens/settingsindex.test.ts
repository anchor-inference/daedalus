// The settings index is one table read three ways — the Settings page's sections and search, and the
// agent's documentation search — so what it claims about the page has to be true of the page: each
// section is one the page draws, under the title the page gives it, and each field's anchor is a row
// the page carries, or a link the agent hands out would open the section and find nothing to show.

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { DICT } from "../i18n";
import { INDEX_FIELDS, INDEX_SECTIONS, SECTION_GROUPS, fieldPath, matchFields } from "./settingsindex";

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

  it("holds every section the page draws a body for, once, grouped as the page lists them", () => {
    const ids = INDEX_SECTIONS.map((section) => section.id);
    expect(new Set(ids).size).toBe(ids.length);
    const drawn = [...SETTINGS.matchAll(/case "([a-z]+)":/g)].map((m) => m[1]).filter((id) => id !== "language");
    for (const id of drawn) expect(ids, id).toContain(id);
    expect(SECTION_GROUPS.map((group) => group.id)).toEqual(["you", "work", "system"]);
    expect(SECTION_GROUPS.flatMap((group) => group.sections.map((section) => section.id)).sort()).toEqual([...ids].sort());
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

  it("gives a field the address of its row, on its tab when it has one", () => {
    const stt = INDEX_FIELDS.find((field) => field.anchor === "asr.transcriber")!;
    expect(fieldPath(stt)).toBe("/app/settings/voice#asr.transcriber");
    const keys = INDEX_FIELDS.find((field) => field.tab === "providers")!;
    expect(fieldPath(keys)).toMatch(/^\/app\/settings\/models\?tab=providers#/);
  });

  it("finds a row by a name that is not its title, in either language", () => {
    expect(matchFields("STT").map((field) => field.anchor)).toContain("asr.transcriber");
    expect(matchFields("диктовка").map((field) => field.anchor)).toContain("asr.transcriber");
    expect(matchFields("quiet hours", "notifications").map((field) => field.anchor)).toEqual(["notifications.quiet_hours"]);
    expect(matchFields("   ")).toEqual([]);
  });
});
