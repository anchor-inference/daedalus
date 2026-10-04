import { describe, expect, it } from "vitest";
import { wallInput, wallInstant } from "./calendarTime";

describe("calendar event time zones", () => {
  it("converts the event's wall time rather than the browser's time zone", () => {
    expect(wallInstant("2026-01-15T09:00", "America/New_York")).toBe("2026-01-15T14:00:00.000Z");
    expect(wallInstant("2026-07-15T09:00", "America/New_York")).toBe("2026-07-15T13:00:00.000Z");
    expect(wallInput("2026-07-15T13:00:00Z", "America/New_York")).toBe("2026-07-15T09:00");
  });

  it("rejects a clock time skipped by daylight saving", () => {
    expect(wallInstant("2026-03-08T02:30", "America/New_York")).toBeNull();
  });
});
