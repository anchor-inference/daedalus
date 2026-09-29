import { useMemo, useState } from "react";
import { Skeleton } from "../components";
import { absTime, clock, dayLabel, int, planName, tokens, untilShort, usd } from "../format";
import { PageHeader, screenTitle } from "../shell";
import { useQuery } from "../store";
import { plural, t } from "../i18n";

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
  raw: Record<string, unknown>;
};
type BySession = { session_id: string | null; title: string | null; calls: number; input_tokens: number; output_tokens: number; cost_usd: number | null; unmetered: number };
type SubWindow = { name: string; used_percent: number; resets_at?: number | string | null };
type Subscription = { provider: string; logged_in: boolean; plan?: string; limit_reached?: boolean; windows?: SubWindow[]; products?: { product: string; used_percent?: number }[]; error?: string };
type UsageData = { daily: Daily[]; recent: Recent[]; sessions: BySession[]; subscriptions?: Record<string, Subscription> };
type Balance = { balances: Record<string, number | null>; thresholds: number[] };

const SUB_LABEL: Record<string, string> = { codex: "Codex · ChatGPT", claude: "Claude", opencode: "OpenCode Go", grok: "SuperGrok" };
const PURPOSES = ["stream", "structured", "text"];
const purposeWord = (p: string) => (PURPOSES.includes(p) ? t(`usage.purpose.${p}`) : p);

function level(used: number): string {
  return used >= 85 ? "bad" : used >= 60 ? "attn" : "";
}

function fmtMs(ms: number): string {
  return ms < 1000 ? `${ms} ms` : t("fmt.dur.s", { n: (ms / 1000).toFixed(ms < 10000 ? 1 : 0) });
}

/** One quota window: label · bar · what is left · when it resets. */
function QuotaLine({ w }: { w: SubWindow }) {
  const used = Math.min(100, Math.max(0, w.used_percent));
  return (
    <div className="quota-line" title={w.resets_at ? t("usage.resetsat", { when: absTime(typeof w.resets_at === "number" ? w.resets_at * 1000 : w.resets_at) }) : undefined}>
      <span className="quota-name">{w.name}</span>
      <div className={`bar ${level(used)}`} style={{ ["--v" as string]: used }}><i /></div>
      <b className="num">{Math.round(100 - used)}{t("usage.left")}</b>
      <span className="sub num">{w.resets_at ? t("usage.resetsat", { when: untilShort(typeof w.resets_at === "number" ? w.resets_at * 1000 : w.resets_at) }) : ""}</span>
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

  const today = new Date().toLocaleDateString("en-CA");
  const todayRows = useMemo(() => (usage?.daily ?? []).filter((d) => d.day === today), [usage, today]);
  const providers = useMemo(() => Array.from(new Set((usage?.recent ?? []).map((r) => r.provider_id))), [usage]);
  const sum = (rows: Daily[], key: keyof Daily) => rows.reduce((a, r) => a + ((r[key] as number) ?? 0), 0);
  const todayCost = todayRows.some((r) => r.cost_usd !== null) ? sum(todayRows, "cost_usd") : null;
  const todayIn = sum(todayRows, "input_tokens");
  const todayCached = sum(todayRows, "cache_read_tokens");
  const hottest = useMemo(() => {
    let best: { name: string; used: number; resets?: number | string | null } | null = null;
    for (const [pid, s] of Object.entries(usage?.subscriptions ?? {})) {
      for (const w of s.windows ?? []) if (!best || w.used_percent > best.used) best = { name: `${SUB_LABEL[pid] ?? pid} · ${w.name}`, used: w.used_percent, resets: w.resets_at };
    }
    return best;
  }, [usage]);
  const recent = (usage?.recent ?? []).filter((r) => provider === "all" || r.provider_id === provider);
  const byDay = new Map<string, Daily[]>();
  for (const d of usage?.daily ?? []) byDay.set(d.day, [...(byDay.get(d.day) ?? []), d]);

  return (
    <>
      <PageHeader title={screenTitle("usage")} subtitle={t("usage.range", { n: days })}>
        <div className="chips">
          {([7, 14, 30] as const).map((d) => (
            <button key={d} className="chip select" aria-pressed={days === d} onClick={() => setDays(d)}>{t("usage.days", { n: d })}</button>
          ))}
        </div>
      </PageHeader>
      <div className="screen wide usage">
        {loading && !error && <Skeleton rows={4} />}
        {error && !usage && <div className="empty"><b>{t("usage.error")}</b><div>{error}</div><button className="btn primary" onClick={refresh}>{t("common.retry")}</button></div>}
        {usage && (
          <>
            <div className="kpi-grid">
              <div className="kpi">
                <div className="label">{t("usage.metered")}</div>
                <div className="value">{usd(todayCost)}</div>
                <div className="sub">{plural("usage.calls", sum(todayRows, "calls"))}{sum(todayRows, "unmetered") > 0 ? t("usage.onsubs", { n: int(sum(todayRows, "unmetered")) }) : ""}</div>
              </div>
              {hottest && (
                <div className="kpi">
                  <div className="label">{t("usage.tightest")}</div>
                  <div className="value">{Math.round(100 - Math.min(100, hottest.used))}<small>{t("usage.left")}</small></div>
                  <div className="sub">{hottest.name}{hottest.resets ? t("usage.resets", { t: untilShort(typeof hottest.resets === "number" ? hottest.resets * 1000 : hottest.resets) }) : ""}</div>
                  <div className={`bar ${level(hottest.used)}`} style={{ ["--v" as string]: Math.min(100, hottest.used) }}><i /></div>
                </div>
              )}
              <div className="kpi">
                <div className="label">{t("usage.tokens")}</div>
                <div className="value">{tokens(todayIn + sum(todayRows, "output_tokens"))}</div>
                <div className="sub">{todayIn ? t("usage.cache", { n: Math.round((100 * todayCached) / todayIn) }) : t("usage.noinput")}{sum(todayRows, "reasoning_tokens") ? t("usage.reasoning", { n: tokens(sum(todayRows, "reasoning_tokens")) }) : ""}</div>
              </div>
            </div>

            {usage.subscriptions && Object.keys(usage.subscriptions).length > 0 && (
              <>
                <div className="section-title">{t("usage.subs")}</div>
                <div className="usage-cards">
                  {Object.entries(usage.subscriptions).map(([name, s]) => (
                    <div key={name} className="card">
                      <div className="title-row">
                        <span className="title">{SUB_LABEL[name] ?? name}</span>
                        {s.plan && <span className="chip">{planName(s.plan)}</span>}
                        {s.limit_reached && <span className="chip bad">{t("usage.limitreached")}</span>}
                      </div>
                      {!s.logged_in && <div className="sub">{t(name === "opencode" ? "usage.nokey" : "usage.notloggedin")}</div>}
                      {s.error && <div className="sub" style={{ color: "var(--bad)" }}>{s.error}</div>}
                      {(s.windows ?? []).map((w) => <QuotaLine key={w.name} w={w} />)}
                      {(s.products ?? []).filter((p) => p.used_percent !== undefined && p.used_percent !== null).map((p) => <QuotaLine key={p.product} w={{ name: p.product, used_percent: p.used_percent ?? 0 }} />)}
                    </div>
                  ))}
                </div>
              </>
            )}

            {balance.data && Object.keys(balance.data.balances).length > 0 && (
              <>
                <div className="section-title">{t("usage.balances")}</div>
                <div className="card">
                  {Object.entries(balance.data.balances).map(([p, b]) => (
                    <div key={p} className="kv">
                      <span>{p}</span>
                      <b>{b === null ? t("usage.unavailable") : usd(b)}</b>
                    </div>
                  ))}
                  <div className="sub">{t("usage.alerts", { list: balance.data.thresholds.map((v) => usd(v)).join(", ") })}</div>
                </div>
              </>
            )}

            {usage.sessions.length > 0 && (
              <>
                <div className="section-title">{t("usage.bysession")}</div>
                <div className="card">
                  {usage.sessions.map((s, i) => (
                    <div key={i} className="usage-row" style={{ gridTemplateColumns: "1fr auto", cursor: s.session_id && onOpen ? "pointer" : "default" }} onClick={() => s.session_id && onOpen?.(s.session_id)}>
                      <div className="what">
                        <div className="l1"><span className="name">{s.title ?? (s.session_id ? s.session_id : t("usage.outside"))}</span></div>
                        <div className="l2">
                          <span>{plural("usage.calls", s.calls)}</span>
                          <span>{t("usage.inout", { in: tokens(s.input_tokens), out: tokens(s.output_tokens) })}</span>
                        </div>
                      </div>
                      <div className="cost">
                        {usd(s.cost_usd)}
                        {s.unmetered > 0 && <div className="sub">{t("usage.onsubs", { n: int(s.unmetered) }).replace(/^ · /, "")}</div>}
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}

            <div className="section-title">{t("usage.byday")}</div>
            <div className="card tablecard">
              <table>
                <thead>
                  <tr>
                    <th>{t("usage.col.day")}</th>
                    <th>{t("usage.col.model")}</th>
                    <th className="num">{t("usage.col.calls")}</th>
                    <th className="num">{t("usage.col.in")}</th>
                    <th className="num">{t("usage.col.out")}</th>
                    <th className="num">{t("usage.col.cached")}</th>
                    <th className="num">{t("usage.col.usd")}</th>
                  </tr>
                </thead>
                <tbody>
                  {Array.from(byDay.entries()).map(([day, rows]) =>
                    rows.map((d, i) => (
                      <tr key={`${day}-${i}`}>
                        <td className="dayc">{i === 0 ? dayLabel(day) : ""}</td>
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
                    )),
                  )}
                </tbody>
              </table>
            </div>

            <div className="section-title">{t("usage.recent")}</div>
            <div className="chips">
              {["all", ...providers].map((p) => (
                <button key={p} className="chip select" aria-pressed={provider === p} onClick={() => setProvider(p)}>{p === "all" ? t("common.all") : p}</button>
              ))}
            </div>
            <div className="card">
              {recent.slice(0, showAllCalls ? 200 : 30).map((r, i) => (
                <div key={i}>
                  <div className={`usage-row ${open === i ? "open" : ""}`} aria-expanded={open === i} onClick={() => setOpen(open === i ? null : i)}>
                    <div className="when">
                      {clock(r.at)}
                      <br />
                      <span style={{ fontSize: 11 }}>{dayLabel(r.at)}</span>
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
                    </div>
                  </div>
                  {open === i && <pre className="usage-raw">{JSON.stringify(r.raw, null, 2)}</pre>}
                </div>
              ))}
              {recent.length === 0 && <div className="empty">{t("usage.nocalls")}</div>}
              {recent.length > 30 && !showAllCalls && <button className="btn small ghost" onClick={() => setShowAllCalls(true)}>{t("usage.showmore", { n: Math.min(200, recent.length) - 30 })}</button>}
            </div>
          </>
        )}
      </div>
    </>
  );
}
