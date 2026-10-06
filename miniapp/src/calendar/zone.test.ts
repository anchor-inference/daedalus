import { describe, expect, it } from "vitest";
import { fromWall, offsetLabel, toWall, todayIn, wallInput, wallInstant } from "./zone";

describe("calendar event time zones", () => {
  it("converts the event's wall time rather than the browser's time zone", () => {
    expect(wallInstant("2026-01-15T09:00", "America/New_York")).toBe("2026-01-15T14:00:00.000Z");
    expect(wallInstant("2026-07-15T09:00", "America/New_York")).toBe("2026-07-15T13:00:00.000Z");
    expect(wallInput("2026-07-15T13:00:00Z", "America/New_York")).toBe("2026-07-15T09:00");
  });

  it("rejects a clock time skipped by daylight saving", () => {
    expect(wallInstant("2026-03-08T02:30", "America/New_York")).toBeNull();
  });

  it("puts an instant on the planner's day and minute, not the browser's", () => {
    expect(toWall("2026-10-06T22:30:00Z", "Europe/Moscow")).toEqual({ day: "2026-10-07", minutes: 90 });
    expect(toWall("2026-10-06T22:30:00Z", "UTC")).toEqual({ day: "2026-10-06", minutes: 1350 });
    expect(todayIn("Asia/Tokyo", Date.parse("2026-10-06T16:00:00Z"))).toBe("2026-10-07");
  });

  it("turns a dragged slot back into an instant, past midnight and over a skipped hour", () => {
    expect(fromWall("2026-10-06", 9 * 60, "Europe/Moscow")).toBe("2026-10-06T06:00:00.000Z");
    expect(fromWall("2026-10-06", 1440 + 30, "UTC")).toBe("2026-10-07T00:30:00.000Z");
    // 02:30 does not exist in New York on 8 March 2026; the slot lands on the first time that does.
    expect(fromWall("2026-03-08", 150, "America/New_York")).toBe("2026-03-08T07:00:00.000Z");
  });

  it("labels a zone with its offset", () => {
    expect(offsetLabel("Europe/Moscow", Date.parse("2026-10-06T12:00:00Z"))).toBe("+03:00");
    expect(offsetLabel("America/New_York", Date.parse("2026-01-06T12:00:00Z"))).toBe("−05:00");
  });
});
