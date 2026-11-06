// The providers of one model on OpenRouter: a row for each endpoint, a tick to limit the model to it.
// Add a model and a model's settings show the same table, so what is ticked reads the same in both.
//
// One row is an endpoint and not a company. OpenRouter lists DeepInfra three times for one model, at
// three prices, and the slug is what a request names; rolled up by name the table could not say which
// of the three a tick had bought.

import { useMemo, useState } from "react";
import { t } from "../i18n";
import { RouteListing, RouteRow, routeBill } from "../models";

function rate(usd: number | null): string {
  if (usd === null) return "—";
  if (usd === 0) return t("add.pill.free");
  return `$${usd < 0.01 ? usd.toFixed(4) : usd < 1 ? usd.toFixed(3) : usd.toFixed(2)}`;
}

function size(tokens: number | null): string {
  if (!tokens) return "—";
  return tokens >= 1_000_000 ? `${Number((tokens / 1_000_000).toFixed(2))}M` : `${Math.round(tokens / 1000)}k`;
}

/** What is left of a slug once the provider's name is on the row: `deepinfra/fp8/flex` → `fp8/flex`. */
function variant(slug: string): string {
  return slug.split("/").slice(1).join("/");
}

function Row({ row, on, gone, onToggle }: { row: RouteRow; on: boolean; gone: boolean; onToggle: () => void }) {
  const price = (value: number | null) => (gone ? "—" : row.varies && value !== null ? `≤${rate(value)}` : rate(value));
  return (
    <label className={`orp-row ${on ? "on" : ""}`} data-slug={row.slug}>
      <input type="checkbox" checked={on} onChange={onToggle} />
      <span className="orp-name">
        <b>{row.name}</b>
        {variant(row.slug) && <span className="mono faint">{variant(row.slug)}</span>}
      </span>
      <span className="orp-cells">
        <span className="orp-cell num" data-label={t("add.routes.col.in")} title={row.varies ? t("add.routes.varies") : undefined}>{price(row.input)}</span>
        <span className="orp-cell num" data-label={t("add.routes.col.out")} title={row.varies ? t("add.routes.varies") : undefined}>{price(row.output)}</span>
        <span className="orp-cell num" data-label={t("add.routes.col.cached")}>{price(row.cache_hit)}</span>
        <span className="orp-cell num" data-label={t("add.routes.col.context")}>{size(row.context_length)}</span>
        <span className="orp-cell num" data-label={t("add.routes.col.output")}>{size(row.max_output_tokens)}</span>
        <span className="orp-cell num" data-label={t("add.routes.col.uptime")}>{row.uptime === null ? "—" : `${Math.round(row.uptime * 10) / 10}%`}</span>
        <span className="orp-tags">
          {gone && <span className="pill failed">{t("add.routes.gone")}</span>}
          {!gone && row.tool_choice === "none" && <span className="pill failed">{t("add.routes.tools.none")}</span>}
          {row.tool_choice === "partial" && <span className="pill pending" title={t("add.routes.tools.partial.title")}>{t("add.routes.tools.partial")}</span>}
          {row.tool_choice === "full" && <span className="pill">{t("add.routes.tools")}</span>}
          {row.reasoning && <span className="pill">{t("add.routes.reasoning")}</span>}
          {row.caching && <span className="pill">{t("add.routes.cache")}</span>}
          {row.zdr && <span className="pill done" title={t("add.routes.zdr.title")}>{t("add.routes.zdr")}</span>}
          {row.tier && <span className="pill">{row.tier}</span>}
        </span>
      </span>
    </label>
  );
}

/** A slug in the choice that the listing no longer has, shown so it can be unticked. */
function goneRow(slug: string): RouteRow {
  return {
    slug, name: slug.split("/")[0], quantization: null, tier: null, input: 0, output: 0, cache_hit: null, varies: false, context_length: null, max_output_tokens: null,
    tools: false, tool_choice: "none", reasoning: false, caching: false, uptime: null, zdr: null, bill: { input: 0, output: 0, cache_hit: null },
  };
}

/** `saved` is the choice as stored: a row for a departed provider stays until the choice is saved, so a mistaken untick can be taken back. */
export function OpenRouterProviders({ listing, chosen, saved = [], onChange }: { listing: RouteListing; chosen: string[]; saved?: string[]; onChange: (next: string[]) => void }) {
  const [filter, setFilter] = useState("");
  const query = filter.trim().toLowerCase();
  const missing = [...new Set([...saved, ...chosen])].filter((slug) => !listing.rows.some((row) => row.slug === slug));
  const shown = useMemo(
    () => listing.rows.filter((row) => !query || row.name.toLowerCase().includes(query) || row.slug.includes(query)),
    [listing.rows, query],
  );
  const bill = routeBill(listing.rows, chosen);
  const noTools = listing.rows.filter((row) => chosen.includes(row.slug) && !row.tools).map((row) => row.name);
  const toggle = (slug: string) => onChange(chosen.includes(slug) ? chosen.filter((other) => other !== slug) : [...chosen, slug]);
  const priceWords = { in: rate(bill?.input ?? null), out: rate(bill?.output ?? null), cached: rate(bill?.cache_hit ?? null) };
  return (
    <div className="orp">
      {listing.rows.length > 8 && (
        <label className="mfield orp-filter">
          <span>{t("add.filter.plain")}</span>
          <input className="field" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder={t("add.filter.plain")} />
        </label>
      )}
      <div className="orp-list">
        <div className="orp-head" aria-hidden="true">
          <span />
          <span>{t("add.routes.col.provider")}</span>
          <span className="num">{t("add.routes.col.in")}</span>
          <span className="num">{t("add.routes.col.out")}</span>
          <span className="num">{t("add.routes.col.cached")}</span>
          <span className="num">{t("add.routes.col.context")}</span>
          <span className="num">{t("add.routes.col.output")}</span>
          <span className="num">{t("add.routes.col.uptime")}</span>
          <span>{t("add.routes.col.features")}</span>
        </div>
        {missing.map((slug) => <Row key={slug} row={goneRow(slug)} on={chosen.includes(slug)} gone onToggle={() => toggle(slug)} />)}
        {shown.map((row) => <Row key={row.slug} row={row} on={chosen.includes(row.slug)} gone={false} onToggle={() => toggle(row.slug)} />)}
      </div>
      <p className="sub orp-note">{t(chosen.length ? "add.routes.some" : "add.routes.none")}</p>
      {bill && <p className="sub orp-note" data-testid="route-price">
        {bill.cache_hit === null ? `${t("add.routes.price", priceWords)} ${t("add.routes.price.nocache")}` : t("add.routes.price.cached", priceWords)}
      </p>}
      {noTools.length > 0 && <p className="result-warning" role="alert">{t("add.routes.notools", { names: noTools.join(", ") })}</p>}
      {listing.images && <p className="sub faint orp-note">{t("add.routes.images")}</p>}
    </div>
  );
}
