import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { setLang } from "./i18n";
import { resetDate, resetWords, windowLabel } from "./quotarow";

beforeEach(() => setLang("en"));
afterEach(() => setLang("en"));

describe("windowLabel", () => {
  it("writes a time window's length in words, in either language", () => {
    expect(["5h", "7d", "90m", "1h"].map(windowLabel)).toEqual(["5 hours", "7 days", "90 minutes", "1 hour"]);
    setLang("ru");
    expect(["5h", "7d", "2h"].map(windowLabel)).toEqual(["5 часов", "7 дней", "2 часа"]);
  });

  it("names a period and keeps any other name whole", () => {
    expect(["weekly", "Daily", "monthly"].map(windowLabel)).toEqual(["Weekly", "Daily", "Monthly"]);
    expect(windowLabel("Gemini 4 Argon (high)")).toBe("Gemini 4 Argon (high)");
    expect(windowLabel("Fable")).toBe("Fable");
  });
});

describe("resetWords", () => {
  const now = Date.parse("2026-11-26T10:00:00Z");

  it("reads hours and minutes up to two days ahead", () => {
    expect(resetWords("2026-11-26T12:14:00Z", now)?.text).toBe("in 2h 14m");
    expect(resetWords("2026-11-26T10:40:00Z", now)?.text).toBe("in 40m");
    expect(resetWords("2026-11-26T13:00:00Z", now)?.text).toBe("in 3h");
    setLang("ru");
    expect(resetWords("2026-11-26T12:14:00Z", now)?.text).toBe("через 2 ч 14 мин");
  });

  it("gives the date for a reset further away, and the time in the title", () => {
    const words = resetWords("2026-11-30T09:00:00Z", now);
    expect(words?.text).not.toMatch(/^in /);
    expect(words?.text).toMatch(/30/);
    expect(words?.title).toMatch(/^Resets /);
  });

  it("takes seconds since the epoch as the host may send them, and says nothing without a reset", () => {
    expect(resetDate(Date.parse("2026-11-26T12:14:00Z") / 1000)?.toISOString()).toBe("2026-11-26T12:14:00.000Z");
    expect(resetWords(null, now)).toBeNull();
    expect(resetWords("not a date", now)).toBeNull();
    expect(resetWords("2026-11-26T09:00:00Z", now)?.text).toBe("now");
  });
});
