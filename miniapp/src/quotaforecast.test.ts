import { afterEach, describe, expect, it } from "vitest";
import { setLang } from "./i18n";
import { cliAccount, forecastFor, forecastText, forecastWarns, type QuotaForecast } from "./quotaforecast";

const NOW = Date.parse("2026-10-15T08:00:00Z");
const at = (minutes: number) => new Date(NOW + minutes * 60_000).toISOString();
const base: QuotaForecast = {
  account: "codex", window: "5h", status: "ok", used_percent: 40, samples: 6, sampled_at: at(0), burn_per_hour: 12,
  eta_at: at(300), eta_early_at: at(240), eta_late_at: at(420), resets_at: at(600), exhausts_before_reset: true,
};

describe("quota forecast words", () => {
  afterEach(() => setLang("en"));

  it("gives the median with its band and the pace", () => {
    expect(forecastText(base, NOW)).toBe("runs out in ~5h (4h–7h) · 12.0%/h");
    expect(forecastText({ ...base, eta_late_at: null }, NOW)).toBe("runs out in ~5h (not before 4h) · 12.0%/h");
    expect(forecastWarns(base)).toBe(true);
    setLang("ru");
    expect(forecastText(base, NOW)).toBe("закончится через ~5 ч (4 ч–7 ч) · 12.0%/ч");
  });

  it("says a window lasts when it resets first, and why there is no estimate otherwise", () => {
    const lasts = { ...base, exhausts_before_reset: false };
    expect(forecastText(lasts, NOW)).toBe("lasts until the reset at this pace · 12.0%/h");
    expect(forecastWarns(lasts)).toBe(false);
    expect(forecastText({ ...base, status: "insufficient", samples: 2 }, NOW)).toBe("not enough readings for a forecast (2 of 3)");
    expect(forecastText({ ...base, status: "idle" }, NOW)).toBe("not being spent now");
    expect(forecastText({ ...base, eta_at: at(-5) }, NOW)).toMatch(/any moment/);
  });

  it("finds a window's forecast and reads a member's own account", () => {
    expect(forecastFor([base], "codex", "5h")).toBe(base);
    expect(forecastFor([base], "codex", "weekly")).toBeNull();
    expect(cliAccount("cli:codex@container")).toEqual({ harness: "codex", env: "container" });
    expect(cliAccount("codex")).toBeNull();
  });
});
