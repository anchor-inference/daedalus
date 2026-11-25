import { useMemo, useState } from "react";
import { Skeleton } from "../ui/index";
import { absTime, clock, dayLabel, fmtMs, fmtRate, int, planName, tokens, untilShort, usd } from "../format";
import { PageHeader, screenTitle } from "../ui/index";
import { Page } from "../shell";
import { Button, Segmented, Select } from "../ui/primitives";
import { Icon } from "../icons";
import { navigate, newAgentPath } from "../router";
import { useQuery } from "../store";
import { DICT, plural, t } from "../i18n";
import { cliAccount, forecastFor, forecastText, forecastWarns, type QuotaForecast } from "../quotaforecast";
import { HARNESS_NAMES } from "../team/team";

type Daily = { day: string; provider_id: string; model: string; calls: number; input_tokens: number; output_tokens: number; cache_read_tokens: number; reasoning_tokens: number; cost_usd: number | null; unmetered: number };
type Recent = {
  at: string;
  provider_id: string;
  model: string;
  purpose: string;
  session_id: string | null;
  session_title: string | null;
  run_id: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  cost_usd: number | null;
  duration_ms: number;
  /** The call's speed: empty for a call answered whole, or made before the host timed them. */
  first_byte_ms?: number | null;
  first_token_ms?: number | null;
  output_tokens_per_second?: number | null;
  raw: Record<string, unknown>;
};
/** One provider and model's speed over the window, as `GET /api/usage/speed` gives it. */
export type ModelSpeed = { provider_id: string; model: string; calls: number; rated: number; first_token_p50_ms: number | null; first_token_p90_ms: number | null; first_byte_p50_ms: number | null; tokens_per_second_p50: number | null; last_at: string };
type SpeedData = { hours: number; models: ModelSpeed[] };
type BySession = { session_id: string | null; title: string | null; calls: number; input_tokens: number; output_tokens: number; cost_usd: number | null; unmetered: number };
type SubWindow = { name: string; used_percent: number; resets_at?: number | string | null };
type Subscription = { provider: string; logged_in: boolean; plan?: string; limit_reached?: boolean; windows?: SubWindow[]; products?: { product: string; used_percent?: number }[]; error?: string };
type UsageData = { daily: Daily[]; recent: Recent[]; sessions: BySession[]; subscriptions?: Record<string, Subscription>; forecasts?: QuotaForecast[] };
type Balance = { balances: Record<string, number | null>; thresholds: number[] };

const SUB_LABEL: Record<string, string> = { codex: "Codex · ChatGPT", claude: "Claude", antigravity: "Google (Antigravity)", opencode: "OpenCode Go", grok: "SuperGrok" };
// The call kinds of a run, then the helpers around the operator's messages, which the host books
// under their own names so their cost can be read apart.
export const PURPOSES = ["stream", "structured", "text", "compaction", "next_message", "meeting_notes", "memory", "memory_edit", "memory_classify"];
const purposeWord = (p: string) => (PURPOSES.includes(p) ? t(`usage.purpose.${p}`) : p);

function level(used: number): string {
  return used >= 85 ? "bad" : used >= 60 ? "attn" : "";
}

const SPEED_WINDOWS = [24, 24 * 7, 24 * 30] as const;

/** How fast each provider and model answered over a window of its own: a slow start shows over a day,
 *  a provider's habit over a month, and neither is the spend window above. */
function SpeedSection() {
  const [hours, setHours] = useState<(typeof SPEED_WINDOWS)[number]>(24);
  const { data, error } = useQuery<SpeedData>(`/api/usage/speed?hours=${hours}`, { staleMs: 30000, pollMs: 120000 });
  const dash = "—";
  return (
    <>
      <div className="pg-label">
        <span>{t("usage.speed")}</span>
        <span data-speed-windows>
          <Segmented
            label={t("usage.speed")}
            value={String(hours) as `${(typeof SPEED_WINDOWS)[number]}`}
            options={SPEED_WINDOWS.map((h) => ({ id: String(h) as `${typeof h}`, label: h < 48 ? t("usage.speed.hours", { n: h }) : t("usage.days", { n: h / 24 }) }))}
            onChange={(id) => setHours(Number(id) as (typeof SPEED_WINDOWS)[number])}
          />
        </span>
      </div>
      <div className="pg-card usage-table" data-speed>
        <table>
          <thead>
            <tr>
              <th>{t("usage.col.model")}</th>
              <th className="num">{t("usage.col.calls")}</th>
              <th className="num">{t("usage.col.ttft")}</th>
              <th className="num">{t("usage.col.ttft90")}</th>
              <th className="num">{t("usage.col.tps")}</th>
            </tr>
          </thead>
          <tbody>
            {error && !data && <tr><td colSpan={5} className="sub usage-none">{t("usage.speed.error", { error })}</td></tr>}
            {data && data.models.length === 0 && <tr><td colSpan={5} className="sub usage-none">{t("usage.speed.none")}</td></tr>}
            {(data?.models ?? []).map((m) => (
              <tr key={`${m.provider_id}/${m.model}`} data-speed-model={`${m.provider_id}/${m.model}`}>
                <td className="modelc">
                  <span className="sub">{m.provider_id}/</span>
                  {m.model}
                </td>
                <td className="num callsc" data-label={t("usage.col.calls")}>{int(m.calls)}</td>
                <td className="num ttftc" data-label={t("usage.col.ttft")} data-speed-ttft>{m.first_token_p50_ms === null ? dash : fmtMs(m.first_token_p50_ms)}</td>
                <td className="num ttft90c" data-label={t("usage.col.ttft90")}>{m.first_token_p90_ms === null ? dash : fmtMs(m.first_token_p90_ms)}</td>
                <td className="num tpsc" data-label={t("usage.col.tps")} data-speed-tps>{m.tokens_per_second_p50 === null ? dash : fmtRate(m.tokens_per_second_p50)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="sub speed-hint">{t("usage.speed.hint")}</div>
      </div>
    </>
  );
}

/** One quota window: label, bar, what is left, when it resets, and under it the pace it is spent
 *  at and when it runs out, when the host has readings enough to say. */
function QuotaLine({ w, forecast }: { w: SubWindow; forecast?: QuotaForecast | null }) {
  const used = Math.min(100, Math.max(0, w.used_percent));
  return (
    <div className="pg-row quota-line" title={w.resets_at ? t("usage.resetsat", { when: absTime(typeof w.resets_at === "number" ? w.resets_at * 1000 : w.resets_at) }) : undefined}>
      <span className="quota-name">{w.name}</span>
      <div className={`bar ${level(used)}`} style={{ ["--v" as string]: used }}><i /></div>
      <b className="num">{Math.round(100 - used)}{t("usage.left")}</b>
      <span className="sub num">{w.resets_at ? t("usage.resetsat", { when: untilShort(typeof w.resets_at === "number" ? w.resets_at * 1000 : w.resets_at) }) : ""}</span>
      {forecast && (
        <span className={`quota-fc ${forecastWarns(forecast) ? "warn" : ""}`} data-forecast={forecast.status}>{forecastText(forecast)}</span>
      )}
    </div>
  );
}

/** The window's days, oldest first, each with what was spent on it. A day nothing happened on is a
 *  day with no bar rather than a gap, so a quiet week reads as one. */
function windowDays(days: number): string[] {
  const out: string[] = [];
  for (let back = days - 1; back >= 0; back--) {
    const d = new Date();
    d.setDate(d.getDate() - back);
    out.push(d.toLocaleDateString("en-CA"));
  }
  return out;
}

/** Tokens per day as bars: one series, so no legend, and the scale is three lines from `--line`.
 *  A bar is a button: the pressed one is the day the card under the chart lists by model. */
function DayChart({ days, perDay, picked, onPick }: { days: string[]; perDay: Map<string, Daily[]>; picked: string; onPick: (day: string) => void }) {
  const total = (day: string) => (perDay.get(day) ?? []).reduce((a, r) => a + r.input_tokens + r.output_tokens, 0);
  const peak = Math.max(1, ...days.map(total));
  return (
    <div className="usage-chart" data-usage-chart>
      <div className="usage-scale" aria-hidden="true">
        {[peak, peak / 2, 0].map((v, i) => <span key={i} className="num">{tokens(Math.round(v))}</span>)}
      </div>
      <div className="usage-plot">
        <div className="usage-grid" aria-hidden="true"><i /><i /><i /></div>
        <div className="usage-bars" role="group" aria-label={t("usage.chart")}>
          {days.map((day) => {
            const n = total(day);
            return (
              <button key={day} type="button" className={`usage-bar ${day === picked ? "on" : ""}`} data-day={day} aria-pressed={day === picked}
                aria-label={`${dayLabel(day)}: ${tokens(n)}`} title={`${dayLabel(day)} · ${tokens(n)}`} onClick={() => onPick(day)}>
                <i style={{ height: `${n === 0 ? 0 : Math.max(2, (100 * n) / peak)}%` }} />
              </button>
            );
          })}
        </div>
        <div className="usage-axis" aria-hidden="true">
          <span>{dayLabel(days[0])}</span>
          {days.length > 6 && <span>{dayLabel(days[Math.floor(days.length / 2)])}</span>}
          <span>{dayLabel(days[days.length - 1])}</span>
        </div>
      </div>
    </div>
  );
}

export function UsageScreen({ onOpen }: { onOpen?: (id: string) => void }) {
  const [days, setDays] = useState<7 | 14 | 30>(14);
  const { data: usage, loading, error, refresh } = useQuery<UsageData>(`/api/usage?days=${days}`, { staleMs: 30000, pollMs: 120000 });
  const balance = useQuery<Balance>("/api/balance", { staleMs: 60000 });
  const [provider, setProvider] = useState<string>("all");
  const [open, setOpen] = useState<number | null>(null);
  const [showAllCalls, setShowAllCalls] = useState(false);
  const [pickedDay, setPickedDay] = useState<string | null>(null);

  const today = new Date().toLocaleDateString("en-CA");
  const todayRows = useMemo(() => (usage?.daily ?? []).filter((d) => d.day === today), [usage, today]);
  const providers = useMemo(() => Array.from(new Set((usage?.recent ?? []).map((r) => r.provider_id))), [usage]);
  const sum = (rows: Daily[], key: keyof Daily) => rows.reduce((a, r) => a + ((r[key] as number) ?? 0), 0);
  // "free" is for calls that cost nothing, on a subscription; a day with no calls at all is $0, a number
  // like every other state of this card, not a word that read as the name of a plan.
  const todayCost = todayRows.length === 0 ? 0 : todayRows.some((r) => r.cost_usd !== null) ? sum(todayRows, "cost_usd") : null;
  const todayIn = sum(todayRows, "input_tokens");
  const todayCached = sum(todayRows, "cache_read_tokens");
  const hottest = useMemo(() => {
    let best: { name: string; used: number; resets?: number | string | null } | null = null;
    for (const [pid, s] of Object.entries(usage?.subscriptions ?? {})) {
      for (const w of s.windows ?? []) if (!best || w.used_percent > best.used) best = { name: `${SUB_LABEL[pid] ?? pid} · ${w.name}`, used: w.used_percent, resets: w.resets_at };
    }
    return best;
  }, [usage]);
  const members = useMemo(() => {
    const out = new Map<string, QuotaForecast[]>();
    for (const f of usage?.forecasts ?? []) if (cliAccount(f.account)) out.set(f.account, [...(out.get(f.account) ?? []), f]);
    return Array.from(out.entries());
  }, [usage]);
  const recent = (usage?.recent ?? []).filter((r) => provider === "all" || r.provider_id === provider);
  const perDay = useMemo(() => {
    const out = new Map<string, Daily[]>();
    for (const d of usage?.daily ?? []) out.set(d.day, [...(out.get(d.day) ?? []), d]);
    return out;
  }, [usage]);
  const chartDays = useMemo(() => windowDays(days), [days, today]);
  // The window summed by model: what the period cost where, which the days alone do not say.
  const models = useMemo(() => {
    const out = new Map<string, Daily>();
    for (const d of usage?.daily ?? []) {
      const key = `${d.provider_id}/${d.model}`;
      const at = out.get(key);
      out.set(key, !at ? { ...d } : {
        ...at,
        calls: at.calls + d.calls,
        input_tokens: at.input_tokens + d.input_tokens,
        output_tokens: at.output_tokens + d.output_tokens,
        cache_read_tokens: at.cache_read_tokens + d.cache_read_tokens,
        reasoning_tokens: at.reasoning_tokens + d.reasoning_tokens,
        cost_usd: at.cost_usd === null && d.cost_usd === null ? null : (at.cost_usd ?? 0) + (d.cost_usd ?? 0),
        unmetered: at.unmetered + d.unmetered,
      });
    }
    return Array.from(out.values()).sort((a, b) => b.input_tokens + b.output_tokens - (a.input_tokens + a.output_tokens));
  }, [usage]);
  const shownDay = pickedDay && chartDays.includes(pickedDay) ? pickedDay : chartDays[chartDays.length - 1];
  const dayRows = perDay.get(shownDay) ?? [];
  const nothing = !!usage && usage.daily.length === 0 && usage.recent.length === 0 && usage.sessions.length === 0;

  return (
    <>
      <PageHeader title={screenTitle("usage")} subtitle={t("usage.range", { n: days })}
        filters={
          <Segmented label={t("usage.period")} value={String(days) as "7" | "14" | "30"} options={([7, 14, 30] as const).map((d) => ({ id: String(d) as "7" | "14" | "30", label: t("usage.days", { n: d }) }))} onChange={(id) => setDays(Number(id) as 7 | 14 | 30)} />
        }
      />
      <Page width="list" className="usage">
        {loading && !error && !usage && <Skeleton rows={4} />}
        {error && !usage && <div className="empty"><b>{t("usage.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {nothing && (
          <div className="empty usage-empty">
            <Icon name="gauge" size={20} />
            <b>{t("usage.empty")}</b>
            <div>{t("usage.empty.sub")}</div>
            <Button variant="primary" icon="compose" onClick={() => navigate(newAgentPath())}>{t("ph.newchat")}</Button>
          </div>
        )}
        {usage && !nothing && (
          <>
            <div className="pg-card usage-hero">
              <div className="usage-fig" data-fig="metered">
                <div className="usage-fig-label">{t("usage.metered")}</div>
                <div className="usage-fig-value">{usd(todayCost)}</div>
                <div className="usage-fig-sub">{plural("usage.calls", sum(todayRows, "calls"))}{sum(todayRows, "unmetered") > 0 ? t("usage.onsubs", { n: int(sum(todayRows, "unmetered")) }) : ""}</div>
              </div>
              {hottest && (
                <div className="usage-fig" data-fig="tightest">
                  <div className="usage-fig-label">{t("usage.tightest")}</div>
                  <div className="usage-fig-value">{Math.round(100 - Math.min(100, hottest.used))}<small>{t("usage.left")}</small></div>
                  <div className="usage-fig-sub">{hottest.name}{hottest.resets ? t("usage.resets", { t: untilShort(typeof hottest.resets === "number" ? hottest.resets * 1000 : hottest.resets) }) : ""}</div>
                  <div className={`bar ${level(hottest.used)}`} style={{ ["--v" as string]: Math.min(100, hottest.used) }}><i /></div>
                </div>
              )}
              <div className="usage-fig" data-fig="tokens">
                <div className="usage-fig-label">{t("usage.tokens")}</div>
                <div className="usage-fig-value">{tokens(todayIn + sum(todayRows, "output_tokens"))}</div>
                <div className="usage-fig-sub">{todayIn ? t("usage.cache", { n: Math.round((100 * todayCached) / todayIn) }) : t("usage.noinput")}{sum(todayRows, "reasoning_tokens") ? t("usage.reasoning", { n: tokens(sum(todayRows, "reasoning_tokens")) }) : ""}</div>
              </div>
            </div>

            <div className="pg-label"><span>{t("usage.chart")}</span><span className="pg-label-note">{t("usage.range", { n: days })}</span></div>
            <div className="pg-card usage-chartcard">
              <DayChart days={chartDays} perDay={perDay} picked={shownDay} onPick={setPickedDay} />
              <div className="usage-day" data-day-readout={shownDay}>
                <div className="pg-row usage-day-head">
                  <b>{dayLabel(shownDay)}</b>
                  <span className="sub num">{t("usage.day.sum", { tokens: tokens(sum(dayRows, "input_tokens") + sum(dayRows, "output_tokens")), calls: plural("usage.calls", sum(dayRows, "calls")) })}</span>
                </div>
                {dayRows.length === 0 && <div className="pg-row sub">{t("usage.day.none")}</div>}
                {dayRows.map((d) => (
                  <div key={`${d.provider_id}/${d.model}`} className="pg-row usage-day-row">
                    <span className="pg-row-title"><span className="sub">{d.provider_id}/</span>{d.model}</span>
                    <span className="sub num">{plural("usage.calls", d.calls)}</span>
                    <span className="sub num">{t("usage.inout", { in: tokens(d.input_tokens), out: tokens(d.output_tokens) })}</span>
                    <span className="num usage-cost">{usd(d.cost_usd)}</span>
                  </div>
                ))}
              </div>
            </div>

            {usage.subscriptions && Object.keys(usage.subscriptions).length > 0 && (
              <>
                <div className="pg-label"><span>{t("usage.subs")}</span></div>
                <div className="usage-cards">
                  {Object.entries(usage.subscriptions).map(([name, s]) => (
                    <div key={name} className="pg-card" data-subscription={name}>
                      <div className="pg-row">
                        <span className="pg-row-title"><b>{SUB_LABEL[name] ?? name}</b></span>
                        {s.plan && <span className="badge">{planName(s.plan)}</span>}
                        {s.limit_reached && <span className="badge attention">{t("usage.limitreached")}</span>}
                      </div>
                      {!s.logged_in && <div className="pg-row sub">{t(name === "opencode" ? "usage.nokey" : "usage.notloggedin")}</div>}
                      {s.error && <div className="pg-row usage-error">{s.error}</div>}
                      {(s.windows ?? []).map((w) => <QuotaLine key={w.name} w={w} forecast={forecastFor(usage.forecasts, name, w.name)} />)}
                      {(s.products ?? []).filter((p) => p.used_percent !== undefined && p.used_percent !== null).map((p) => <QuotaLine key={p.product} w={{ name: p.product, used_percent: p.used_percent ?? 0 }} />)}
                    </div>
                  ))}
                </div>
              </>
            )}

            {members.length > 0 && (
              <>
                {/* A member's CLI reports its own login's windows, which need not be the key proxy's. */}
                <div className="pg-label"><span>{t("usage.fc.members")}</span></div>
                <div className="usage-cards">
                  {members.map(([account, rows]) => {
                    const who = cliAccount(account)!;
                    return (
                      <div key={account} className="pg-card" data-cli-account={account}>
                        <div className="pg-row">
                          <span className="pg-row-title"><b>{t("usage.fc.cli", { cli: HARNESS_NAMES[who.harness as keyof typeof HARNESS_NAMES] ?? who.harness, env: `team.env.${who.env}` in DICT ? t(`team.env.${who.env}`) : who.env })}</b></span>
                        </div>
                        {rows.map((f) => <QuotaLine key={f.window} w={{ name: f.window, used_percent: f.used_percent, resets_at: f.resets_at }} forecast={f} />)}
                      </div>
                    );
                  })}
                </div>
              </>
            )}

            {balance.data && Object.keys(balance.data.balances).length > 0 && (
              <>
                <div className="pg-label"><span>{t("usage.balances")}</span></div>
                <div className="pg-card">
                  {Object.entries(balance.data.balances).map(([p, b]) => (
                    <div key={p} className="pg-row">
                      <span className="pg-row-title">{p}</span>
                      <b className="num">{b === null ? t("usage.unavailable") : usd(b)}</b>
                    </div>
                  ))}
                  <div className="pg-row sub">{t("usage.alerts", { list: balance.data.thresholds.map((v) => usd(v)).join(", ") })}</div>
                </div>
              </>
            )}

            {models.length > 0 && (
              <>
                <div className="pg-label"><span>{t("usage.bymodel")}</span><span className="pg-label-note">{t("usage.range", { n: days })}</span></div>
                <div className="pg-card usage-table" data-models>
                  <table>
                    <thead>
                      <tr>
                        <th>{t("usage.col.model")}</th>
                        <th className="num">{t("usage.col.calls")}</th>
                        <th className="num">{t("usage.col.in")}</th>
                        <th className="num">{t("usage.col.out")}</th>
                        <th className="num">{t("usage.col.cached")}</th>
                        <th className="num">{t("usage.col.usd")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {models.map((d) => (
                        <tr key={`${d.provider_id}/${d.model}`} data-model={`${d.provider_id}/${d.model}`}>
                          <td className="modelc">
                            <span className="sub">{d.provider_id}/</span>
                            {d.model}
                          </td>
                          {/* The labels are for a phone, where the row becomes a card and the header is gone. */}
                          <td className="num callsc" data-label={t("usage.col.calls")}>{int(d.calls)}</td>
                          <td className="num inc" data-label={t("usage.col.in")}>{tokens(d.input_tokens)}</td>
                          <td className="num outc" data-label={t("usage.col.out")}>{tokens(d.output_tokens)}</td>
                          <td className="num cachedc" data-label={t("usage.col.cached")}>{tokens(d.cache_read_tokens)}</td>
                          <td className="num usdc">
                            {usd(d.cost_usd)}
                            {/* Said in words, as the session card says it: a bare "+12" beside "$0" read as money owed. */}
                            {d.unmetered > 0 && <div className="sub">{t("usage.onsubs", { n: int(d.unmetered) }).replace(/^ · /, "")}</div>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}

            {usage.sessions.length > 0 && (
              <>
                <div className="pg-label"><span>{t("usage.bysession")}</span></div>
                <div className="pg-card">
                  {usage.sessions.map((s, i) => (
                    <button key={i} type="button" className="usage-session" disabled={!s.session_id || !onOpen} onClick={() => s.session_id && onOpen?.(s.session_id)}>
                      <span className="usage-session-what">
                        <span className="usage-session-name">{s.title ?? (s.session_id ? s.session_id : t("usage.outside"))}</span>
                        {/* The calls on a subscription belong with the count they are part of: under the
                            price they made this one row two lines tall among rows of one. */}
                        <span className="usage-session-meta">
                          <span>{plural("usage.calls", s.calls)}{s.unmetered > 0 ? t("usage.onsubs", { n: int(s.unmetered) }) : ""}</span>
                          <span>{t("usage.inout", { in: tokens(s.input_tokens), out: tokens(s.output_tokens) })}</span>
                        </span>
                      </span>
                      <span className="num usage-cost">{usd(s.cost_usd)}</span>
                    </button>
                  ))}
                </div>
              </>
            )}

            <SpeedSection />

            <div className="pg-label">
              <span>{t("usage.recent")}</span>
              {providers.length > 1 && (
                <Select size="sm" label={t("usage.provider")} value={provider} options={[{ id: "all", label: t("common.all") }, ...providers.map((p) => ({ id: p, label: p }))]} onChange={(id) => { setProvider(id); setOpen(null); }} />
              )}
            </div>
            <div className="pg-card">
              {recent.slice(0, showAllCalls ? 200 : 30).map((r, i) => (
                <div key={i}>
                  <div className={`usage-row ${open === i ? "open" : ""}`} aria-expanded={open === i} onClick={() => setOpen(open === i ? null : i)}>
                    <div className="when">
                      {clock(r.at)}
                      <br />
                      <span>{dayLabel(r.at)}</span>
                    </div>
                    <div className="what">
                      <div className="l1">
                        <span className={`badge ${r.purpose}`}>{purposeWord(r.purpose)}</span>
                        <span className="name">{r.session_title ?? (r.session_id ? r.session_id : "—")}</span>
                      </div>
                      <div className="l2">
                        <span>{r.provider_id}/{r.model}</span>
                        <span>
                          {t("usage.inout", { in: tokens(r.input_tokens), out: tokens(r.output_tokens) })}{r.cache_read_tokens ? t("usage.cached", { n: tokens(r.cache_read_tokens) }) : ""}
                          {r.reasoning_tokens ? t("usage.reasoning", { n: tokens(r.reasoning_tokens) }) : ""}
                        </span>
                      </div>
                    </div>
                    <div className="cost">
                      {usd(r.cost_usd)}
                      <br />
                      <span className="ms">{fmtMs(r.duration_ms)}</span>
                      {/* The call's own speed beside its length, for the call that felt slow. */}
                      {typeof r.first_token_ms === "number" && <><br /><span className="ms" data-call-ttft>{t("usage.call.ttft", { t: fmtMs(r.first_token_ms) })}</span></>}
                      {typeof r.output_tokens_per_second === "number" && <><br /><span className="ms" data-call-tps>{t("usage.call.tps", { n: fmtRate(r.output_tokens_per_second) })}</span></>}
                    </div>
                  </div>
                  {open === i && <pre className="usage-raw">{JSON.stringify(r.raw, null, 2)}</pre>}
                </div>
              ))}
              {recent.length === 0 && <div className="pg-row sub">{t("usage.nocalls")}</div>}
              {recent.length > 30 && !showAllCalls && <div className="pg-row"><button className="btn small ghost" onClick={() => setShowAllCalls(true)}>{t("usage.showmore", { n: Math.min(200, recent.length) - 30 })}</button></div>}
            </div>
          </>
        )}
      </Page>
    </>
  );
}
