import { describe, expect, it } from "vitest";
import { commandPreview, cronFor, describeCron, describeSchedule, duration, middleTruncate, plainPreview, planName, relTime, tokens, untilShort, usd } from "./format";
import { setLang } from "./i18n";

const NOW = Date.parse("2026-09-11T12:00:00Z");

describe("relTime", () => {
  it("reads as a short age", () => {
    expect(relTime("2026-09-11T11:59:50Z", NOW)).toBe("now");
    expect(relTime("2026-09-11T11:25:00Z", NOW)).toBe("35m");
    expect(relTime("2026-09-11T09:00:00Z", NOW)).toBe("3h");
    expect(relTime("2026-09-09T12:00:00Z", NOW)).toBe("2d");
  });
  it("counts forward for a future moment", () => {
    expect(relTime("2026-09-11T14:00:00Z", NOW)).toBe("in 2h");
    expect(untilShort("2026-09-11T12:12:00Z", NOW)).toBe("in 12m");
    expect(untilShort("2026-09-11T11:00:00Z", NOW)).toBe("now");
  });
  it("is empty for nothing", () => {
    expect(relTime(null)).toBe("");
    expect(relTime("garbage")).toBe("");
  });
});

describe("numbers", () => {
  it("formats durations, tokens and money", () => {
    expect(duration(4000)).toBe("4s");
    expect(duration(134000)).toBe("2m 14s");
    expect(duration(6240000)).toBe("1h 44m");
    expect(tokens(711_500_000)).toBe("711.5M");
    expect(tokens(28_400)).toBe("28k");
    expect(usd(3.9)).toBe("$3.90");
    expect(usd(0.004)).toBe("< $0.01");
    expect(usd(null)).toBe("free");
  });
  it("keeps both ends of a long path", () => {
    expect(middleTruncate("/srv/workspaces/62d62b5f668d/round8_probe.py", 28)).toBe("/srv/worksp…/round8_probe.py");
    expect(middleTruncate("short", 28)).toBe("short");
  });
  it("names a plan", () => {
    expect(planName("default_claude_max_20x")).toBe("Max 20×");
    expect(planName("plus")).toBe("Plus");
    expect(planName("")).toBe("");
  });
});

describe("previews", () => {
  it("drops the workspace cd prefix from a command", () => {
    expect(commandPreview("cd /srv/workspaces/abc && pytest -q\necho done", "/srv/workspaces/abc")).toBe("pytest -q");
    expect(commandPreview("cd /tmp; ls")).toBe("ls");
    expect(commandPreview("python round8_probe.py")).toBe("python round8_probe.py");
  });
  it("strips markdown for a one-line preview", () => {
    expect(plainPreview("Results: 1. **Their correction** does `x`\n- item")).toBe("Results: 1. Their correction does x item");
  });
});

describe("schedules", () => {
  it("reads a cron line in the reader's zone", () => {
    expect(describeCron("0 4 * * 1-5", 300)).toBe("Weekdays at 09:00");
    expect(describeCron("30 9 * * *", 300)).toBe("Every day at 14:30");
    expect(describeCron("0 */2 * * *", 300)).toBe("Every 2 hours");
    expect(describeCron("*/15 * * * *", 300)).toBe("Every 15 min");
    expect(describeCron("0 4 * * 1,3,5", 300)).toBe("Mon, Wed, Fri at 09:00");
  });
  it("moves the day when the shift crosses midnight", () => {
    expect(describeCron("0 22 * * 1-5", 300)).toBe("Tue, Wed, Thu, Fri, Sat at 03:00");
    expect(describeCron("0 2 * * 1", -300)).toBe("Sun at 21:00");
  });
  it("leaves the exotic lines alone", () => {
    expect(describeCron("0 4 1-7 * *", 300)).toBe("0 4 1-7 * * (UTC)");
    expect(describeCron("0 9-17/2 * * *", 300)).toBe("0 9-17/2 * * * (UTC)");
    expect(describeCron("nonsense")).toBe("nonsense");
  });
  // The shapes the agent writes, which the Schedules screen once showed as raw cron.
  it("says the hourly, several-times-a-day and monthly shapes in words, in the reader's zone", () => {
    expect(describeCron("5 * * * *", 300)).toBe("Every hour at :05");
    expect(describeCron("10 2 * * *", 300)).toBe("Every day at 07:10");
    expect(describeCron("50 5,11,17,23 * * *", 300)).toBe("Every day at 04:50, 10:50, 16:50, 22:50");
    expect(describeCron("15 */3 * * *", 300)).toBe("Every 3 hours at :15");
    expect(describeCron("0 4 1 * *", 300)).toBe("Every month on day 1 at 09:00");
    // A zone half an hour off the hour moves the minute past it, and a shift back across the first
    // of the month is left as cron rather than guessed.
    expect(describeCron("5 * * * *", 330)).toBe("Every hour at :35");
    expect(describeCron("0 22 1 * *", -300)).toBe("Every month on day 1 at 17:00");
    expect(describeCron("0 2 1 * *", -300)).toBe("0 2 1 * * (UTC)");
  });
  it("says them in Russian too", () => {
    setLang("ru");
    try {
      expect(describeCron("5 * * * *", 300)).toBe("Каждый час в :05");
      expect(describeCron("10 2 * * *", 300)).toBe("Каждый день в 07:10");
      expect(describeCron("15 */2 * * *", 300)).toBe("Каждые 2 часа в :15");
      expect(describeCron("0 4 1 * *", 300)).toBe("Каждый месяц, 1-го числа в 09:00");
      expect(describeSchedule({ cron: null, run_at: "2026-10-27T13:50:00Z" })).toMatch(/^Однократно /);
    } finally {
      setLang("en");
    }
  });
  it("builds a UTC cron from a local time", () => {
    expect(cronFor("weekdays", 9, 0, [], 1, 300)).toBe("0 4 * * 1,2,3,4,5");
    expect(cronFor("daily", 1, 30, [], 1, 300)).toBe("30 20 * * *");
    expect(cronFor("weekly", 2, 0, [1], 1, 300)).toBe("0 21 * * 0");
    expect(cronFor("hours", 0, 15, [], 3)).toBe("15 */3 * * *");
    expect(describeCron(cronFor("weekly", 2, 0, [1], 1, 300), 300)).toBe("Mon at 02:00");
  });
});
