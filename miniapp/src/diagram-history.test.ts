import { afterEach, describe, expect, it } from "vitest";
import { changeSummary, dayLabel, groupByDay } from "./diagram-history";
import { TEMPLATES, templateSkeleton } from "./diagram-templates";
import { setLang } from "./i18n";

afterEach(() => setLang("en"));

describe("a revision's change summary", () => {
  it("counts items in words, never element ids", () => {
    setLang("en");
    expect(changeSummary({ added: { shape: 3, connector: 1 }, removed: {}, changed: 2, renamed: true })).toBe("+3 shapes, +1 connector, 2 changed, renamed");
    expect(changeSummary({ added: {}, removed: { text: 2 }, changed: 0, renamed: false })).toBe("2 texts removed");
    expect(changeSummary({ added: {}, removed: {}, changed: 0, renamed: false })).toBe("No visible changes");
    expect(changeSummary({ created: true }, "create")).toBe("Created");
    expect(changeSummary({ oldest: true })).toBe("Earliest kept version");
    expect(changeSummary({ added: {}, removed: {}, changed: 4, restored_from: 3 }, "restore")).toBe("Restored an earlier version, 4 changed");
  });

  it("uses Russian plural forms", () => {
    setLang("ru");
    expect(changeSummary({ added: { shape: 2 }, removed: { connector: 5 }, changed: 1 })).toBe("+2 фигуры, удалено: 5 связей, 1 изменён");
  });
});

describe("history days", () => {
  const now = new Date(2026, 9, 6, 15, 0);
  it("groups newest first under Today, Yesterday and the date", () => {
    setLang("en");
    const at = (day: number, hour: number) => new Date(2026, 9, day, hour, 5).toISOString();
    const groups = groupByDay([{ saved_at: at(4, 9) }, { saved_at: at(6, 14) }, { saved_at: at(5, 23) }, { saved_at: at(6, 8) }], now);
    expect(groups.map((group) => group.label)).toEqual(["Today", "Yesterday", dayLabel(at(4, 9), now)]);
    expect(groups[0].items.map((item) => item.saved_at)).toEqual([at(6, 14), at(6, 8)]);
    expect(groups[2].label).not.toMatch(/2026/);
    expect(dayLabel(new Date(2025, 0, 2).toISOString(), now)).toMatch(/2025/);
  });
});

describe("starter templates", () => {
  it("connect only shapes they contain", () => {
    for (const id of TEMPLATES) {
      const skeleton = templateSkeleton(id);
      const ids = new Set(skeleton.filter((item) => item.id).map((item) => item.id));
      expect(ids.size).toBe(skeleton.filter((item) => item.id).length);
      for (const arrow of skeleton.filter((item) => item.type === "arrow")) {
        expect(ids.has(arrow.start!.id)).toBe(true);
        expect(ids.has(arrow.end!.id)).toBe(true);
      }
    }
    expect(templateSkeleton("blank")).toEqual([]);
  });
});
