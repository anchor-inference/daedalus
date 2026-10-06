import { describe, expect, it } from "vitest";
import { addDays, addMonths, monthWeeks, parseClock, rangeTitle, snap, startOfWeek, step, visibleRange, weekdayNames } from "./dates";

describe("planner dates", () => {
  it("counts days across a daylight-saving change as whole days", () => {
    expect(addDays("2026-10-24", 2)).toBe("2026-10-26");
    expect(addDays("2026-03-01", -1)).toBe("2026-02-28");
  });

  it("starts the week on the day the settings name", () => {
    expect(startOfWeek("2026-10-07", 1)).toBe("2026-10-05");
    expect(startOfWeek("2026-10-07", 0)).toBe("2026-10-04");
    expect(startOfWeek("2026-10-05", 1)).toBe("2026-10-05");
  });

  it("clamps a month step to the shorter month", () => {
    expect(addMonths("2026-01-31", 1)).toBe("2026-02-28");
    expect(addMonths("2026-03-15", -3)).toBe("2025-12-15");
  });

  it("draws a month as the whole weeks it touches", () => {
    const weeks = monthWeeks("2026-10-14", 1);
    expect(weeks[0][0]).toBe("2026-09-28");
    expect(weeks[weeks.length - 1][6]).toBe("2026-11-01");
    expect(weeks).toHaveLength(5);
    expect(monthWeeks("2026-02-01", 0)).toHaveLength(4);
  });

  it("gives each view its days and its step", () => {
    const opts = { weekStart: 1, weekends: true };
    expect(visibleRange("week", "2026-10-07", opts).days).toHaveLength(7);
    const work = visibleRange("week", "2026-10-07", { weekStart: 1, weekends: false });
    expect(work.days).toEqual(["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]);
    expect(work.end).toBe("2026-10-12");
    expect(visibleRange("3day", "2026-10-07", opts).days).toEqual(["2026-10-07", "2026-10-08", "2026-10-09"]);
    expect(step("week", "2026-10-07", 1)).toBe("2026-10-14");
    expect(step("month", "2026-10-31", 1)).toBe("2026-11-01");
    expect(step("3day", "2026-10-07", -1)).toBe("2026-10-04");
  });

  it("titles the range in the reader's language", () => {
    const range = visibleRange("week", "2026-10-07", { weekStart: 1, weekends: true });
    expect(rangeTitle("week", range, "2026-10-07", "en-GB")).toMatch(/^5\s*–\s*11 October 2026$/);
    expect(rangeTitle("month", range, "2026-10-07", "ru-RU")).toMatch(/октябрь 2026/i);
    expect(rangeTitle("month", range, "2026-10-07", "en-GB", true, "2026-01-01")).toBe("October");
  });

  it("snaps to quarter hours and reads a clock", () => {
    expect(snap(9 * 60 + 7)).toBe(9 * 60);
    expect(snap(9 * 60 + 8)).toBe(9 * 60 + 15);
    expect(parseClock("09:30")).toBe(570);
    expect(parseClock("25:00")).toBeNull();
    expect(weekdayNames("en-GB", 1)[0]).toBe("Mon");
  });
});
