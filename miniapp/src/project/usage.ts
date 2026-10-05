// What a project spends, as `GET /api/projects/{id}/usage` answers it, and the words for it. Pure, so the
// formatting is tested without a browser: a member on a subscription is shown by the share of the
// window used, not as "$0.00", which would read as "free".

import { num, plural, t } from "../i18n";

export type Spend = { usd: number; tokens: number; unpriced: number; subscriptions: number };
export type UsageLine = { today: Spend; week: Spend; all: Spend; subscription: { window_used_pct: number | null; source: string } | null };
export type StaffUsage = UsageLine & { staff_id: string; name: string; harness: string; archived: boolean };
export type ProjectUsage = { project_id: string; staff: StaffUsage[]; orchestrator: UsageLine; other: UsageLine; total: UsageLine };

export const usageKey = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/usage`;

/** Dollars as the operator reads them: cents below a hundred, whole dollars above. */
export function usd(value: number): string {
  if (value >= 100) return `$${num(Math.round(value))}`;
  return `$${value.toFixed(2)}`;
}

/** 412k, 1.5M, 900. */
export function tokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1000) return `${Math.round(value / 1000)}k`;
  return String(value);
}

/** One member's (or the orchestrator's) spend today, in words; null when there is nothing to say. */
export function spendLine(line: UsageLine | undefined | null): string | null {
  if (!line) return null;
  const sub = line.subscription;
  const today = line.today;
  if (today.usd === 0 && today.tokens === 0 && today.unpriced === 0 && today.subscriptions === 0) return null;
  const parts = today.usd > 0 || (today.unpriced === 0 && today.subscriptions === 0)
    ? [t("pusage.today", { usd: usd(today.usd) })] : [];
  if (today.tokens && today.subscriptions === 0) parts.push(t("pusage.tokens", { n: tokens(today.tokens) }));
  if (today.unpriced) parts.push(plural("pusage.unpriced", today.unpriced));
  if (today.subscriptions) parts.push(sub?.source === "subscription" && sub.window_used_pct !== null
    ? t("pusage.subscription", { pct: Math.round(sub.window_used_pct) })
    : plural("pusage.subscription.sessions", today.subscriptions));
  return parts.join(" · ");
}

/** The project's totals over the three windows, for the top of the journal. */
export function totalsLine(usage: ProjectUsage): string {
  const total = usage.total;
  const parts = [
    t(total.today.unpriced || total.today.subscriptions ? "pusage.window.today.known" : "pusage.window.today", { usd: usd(total.today.usd) }),
    t(total.week.unpriced || total.week.subscriptions ? "pusage.window.week.known" : "pusage.window.week", { usd: usd(total.week.usd) }),
    t(total.all.unpriced || total.all.subscriptions ? "pusage.window.all.known" : "pusage.window.all", { usd: usd(total.all.usd) }),
    t("pusage.tokens", { n: tokens(total.all.tokens) }),
  ];
  if (total.all.unpriced) parts.push(plural("pusage.unpriced", total.all.unpriced));
  if (total.all.subscriptions) parts.push(plural("pusage.subscription.sessions", total.all.subscriptions));
  return parts.join(" · ");
}

/** The header chip: today's total, and nothing at all on a day nothing was spent. */
export function chipText(usage: ProjectUsage | null | undefined): string | null {
  if (!usage) return null;
  const today = usage.total.today;
  if (today.usd === 0 && today.tokens === 0 && today.unpriced === 0 && today.subscriptions === 0) return null;
  if (today.unpriced || today.subscriptions) return t("pusage.today.unknown", { usd: usd(today.usd) });
  return t("pusage.today", { usd: usd(today.usd) });
}

export function staffUsage(usage: ProjectUsage | null | undefined, staffId: string): StaffUsage | undefined {
  return usage?.staff.find((row) => row.staff_id === staffId);
}
