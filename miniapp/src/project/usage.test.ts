import { afterEach, describe, expect, it } from "vitest";
import { setLang } from "../i18n";
import { ProjectUsage, UsageLine, chipText, spendLine, tokens, totalsLine, usd } from "./usage";

const none = { usd: 0, tokens: 0, unpriced: 0, subscriptions: 0 };
const line = (today: Partial<typeof none>, subscription: UsageLine["subscription"] = null): UsageLine => ({ today: { ...none, ...today }, week: { ...none, ...today }, all: { ...none, ...today }, subscription });

describe("project spend in words", () => {
  afterEach(() => setLang("en"));
  it("writes dollars and tokens the short way", () => {
    expect(usd(1.2)).toBe("$1.20");
    expect(usd(0)).toBe("$0.00");
    expect(usd(1234.4)).toBe("$1,234");
    expect(tokens(412_000)).toBe("412k");
    expect(tokens(1_500_000)).toBe("1.5M");
    expect(tokens(900)).toBe("900");
  });
  it("says what a member spent today, and nothing for a quiet day", () => {
    expect(spendLine(line({ usd: 1.2, tokens: 412_000 }))).toBe("$1.20 today · 412k tokens");
    expect(spendLine(line({ usd: 0.3, tokens: 1000, unpriced: 2 }))).toBe("$0.30 today · 1k tokens · 2 unpriced calls");
    expect(spendLine(line({}))).toBeNull();
    expect(spendLine(null)).toBeNull();
  });
  it("shows a subscription by the window used, never as free", () => {
    expect(spendLine(line({ tokens: 412_000, subscriptions: 1 }, { window_used_pct: 23.4, source: "subscription" }))).toBe("subscription · window 23 %");
  });
  it("sums the project over the three windows, in both languages", () => {
    const usage: ProjectUsage = { project_id: "p", staff: [], orchestrator: line({}), other: line({}), total: { today: { ...none, usd: 3.05, tokens: 2000 }, week: { ...none, usd: 12.4, tokens: 9000 }, all: { ...none, usd: 40.1, tokens: 1_200_000, unpriced: 1 }, subscription: null } };
    expect(totalsLine(usage)).toBe("Today $3.05 · 7 days $12.40 · All known $40.10 · 1.2M tokens · 1 unpriced call");
    expect(chipText(usage)).toBe("$3.05 today");
    expect(chipText({ ...usage, total: { ...usage.total, today: none } })).toBeNull();
    setLang("ru");
    expect(totalsLine(usage)).toBe("Сегодня $3.05 · 7 дней $12.40 · Всего известно $40.10 · токенов: 1.2M · 1 вызов без цены");
  });
  it("shows priced and subscription activity together without claiming the subscription was free", () => {
    const member = line({ usd: 0.4, tokens: 1000, subscriptions: 1 }, { window_used_pct: 40, source: "subscription" });
    const usage: ProjectUsage = { project_id: "p", staff: [], orchestrator: line({}), other: line({}), total: member };
    expect(spendLine(member)).toBe("$0.40 today · subscription · window 40 %");
    expect(totalsLine(usage)).toContain("Today known $0.40");
    expect(totalsLine(usage)).toContain("1 subscription session (USD unavailable)");
    expect(chipText(usage)).toBe("Known $0.40 today + unknown price");
    expect(chipText({ ...usage, total: line({ tokens: 1000, subscriptions: 1 }) })).toBe("Known $0.00 today + unknown price");
  });
});
