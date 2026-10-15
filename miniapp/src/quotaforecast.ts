// The quota forecast in words: how fast a subscription window is spent and when it runs out at that
// pace (`daedalus/stores/quota.py`). Pure, so each wording is tested without a browser.

import { duration } from "./format";
import { t } from "./i18n";

/** One window's forecast as the host computes it. */
export type QuotaForecast = {
  account: string;
  window: string;
  status: "ok" | "insufficient" | "idle" | "exhausted";
  used_percent: number;
  samples: number;
  sampled_at: string | null;
  burn_per_hour: number | null;
  eta_at: string | null;
  eta_early_at: string | null;
  eta_late_at: string | null;
  resets_at: string | null;
  exhausts_before_reset: boolean | null;
};

/** The forecast of one window, found by its account and name. */
export function forecastFor(forecasts: QuotaForecast[] | undefined, account: string, window: string): QuotaForecast | null {
  return (forecasts ?? []).find((f) => f.account === account && f.window === window) ?? null;
}

function until(iso: string | null, now: number): string {
  const at = iso ? Date.parse(iso) : NaN;
  return Number.isFinite(at) ? duration(Math.max(0, at - now)) : "";
}

/** "runs out in ~2h 10m (1h 30m–3h 40m) · 6.0%/h", or why there is no such line. */
export function forecastText(f: QuotaForecast, now = Date.now()): string {
  const rate = (f.burn_per_hour ?? 0).toFixed(1);
  switch (f.status) {
    case "insufficient":
      return t("usage.fc.insufficient", { n: f.samples });
    case "idle":
      return t("usage.fc.idle");
    case "exhausted":
      return t("usage.fc.exhausted");
  }
  // A window that resets before it would run out is fine, whatever the date the pace points at.
  if (f.exhausts_before_reset === false) return t("usage.fc.lasts", { rate });
  const eta = f.eta_at ? Date.parse(f.eta_at) : NaN;
  if (!Number.isFinite(eta) || eta <= now) return t("usage.fc.soon", { rate });
  const early = until(f.eta_early_at, now);
  return f.eta_late_at
    ? t("usage.fc.ok", { eta: until(f.eta_at, now), early, late: until(f.eta_late_at, now), rate })
    : t("usage.fc.ok.open", { eta: until(f.eta_at, now), early, rate });
}

/** Whether the forecast warns: it runs out before its window resets. */
export function forecastWarns(f: QuotaForecast | null): boolean {
  return !!f && (f.status === "exhausted" || (f.status === "ok" && f.exhausts_before_reset !== false));
}

/** A member CLI's own account ("cli:codex@container") as its CLI and environment; null for the key proxy's. */
export function cliAccount(account: string): { harness: string; env: string } | null {
  const found = /^cli:([^@]+)@(.+)$/.exec(account);
  return found ? { harness: found[1], env: found[2] } : null;
}
