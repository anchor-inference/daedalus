import { describe, expect, it } from "vitest";
import { buildRule, describeRule, parseRule, presetOf, presetRule } from "./rrule";

describe("repeat presets", () => {
  it("writes each preset as an RRULE anchored on the first day", () => {
    expect(presetRule("weekly", "2026-10-06")).toBe("FREQ=WEEKLY;BYDAY=TU");
    expect(presetRule("monthly", "2026-10-06")).toBe("FREQ=MONTHLY;BYMONTHDAY=6");
    expect(presetRule("weekdays", "2026-10-06")).toBe("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR");
    expect(presetRule("none", "2026-10-06")).toBe("");
  });

  it("recognises a stored rule as the preset that wrote it", () => {
    expect(presetOf("", "2026-10-06")).toBe("none");
    expect(presetOf("RRULE:FREQ=WEEKLY;BYDAY=TU", "2026-10-06")).toBe("weekly");
    expect(presetOf("FREQ=WEEKLY;BYDAY=FR,MO,TU,WE,TH", "2026-10-06")).toBe("weekdays");
    expect(presetOf("FREQ=WEEKLY;INTERVAL=2;BYDAY=TU", "2026-10-06")).toBe("custom");
    expect(presetOf("FREQ=DAILY;COUNT=5", "2026-10-06")).toBe("custom");
    expect(presetOf("FREQ=YEARLY", "2026-10-06")).toBe("yearly");
  });

  it("reads and writes a custom rule", () => {
    const rule = parseRule("FREQ=WEEKLY;INTERVAL=2;BYDAY=WE,MO;UNTIL=20261231T235959Z");
    expect(rule).toEqual({ freq: "WEEKLY", interval: 2, byday: ["WE", "MO"], until: "2026-12-31", count: null });
    expect(buildRule(rule!)).toBe("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;UNTIL=20261231T235959Z");
    expect(buildRule({ freq: "DAILY", interval: 1, byday: [], until: null, count: 10 })).toBe("FREQ=DAILY;COUNT=10");
    expect(parseRule("FREQ=HOURLY")).toBeNull();
  });

  it("describes a rule in words", () => {
    const words = {
      every: (n: number, unit: string) => (n === 1 ? `Every ${unit.toLowerCase()}` : `Every ${n} ${unit.toLowerCase()}`),
      on: (days: string) => `on ${days}`,
      until: (day: string) => `until ${day}`,
      times: (n: number) => `${n} times`,
      dayName: (code: string) => code,
    };
    expect(describeRule("FREQ=WEEKLY;INTERVAL=2;BYDAY=WE,MO;COUNT=4", words, (d) => d)).toBe("Every 2 weekly on MO, WE · 4 times");
  });
});
