// A model's price in its settings card: the three rates, how it is paid for, and where the numbers come
// from. Self-contained (it saves through its own endpoint and hands the new settings back), so it can sit
// in the card today and move with the model's settings wherever they go.

import { useEffect, useState } from "react";
import { api, PriceView, Settings } from "./api";
import { t } from "./i18n";
import { Row } from "./settingsrow";
import { Segmented } from "./ui/index";
import { priceBody, priceChanged, priceDraft } from "./models";

const RATES = ["input", "output", "cache_hit"] as const;

export function ModelPrice({ id, view, onSaved, toast }: {
  id: string;
  view: PriceView | undefined;
  onSaved: (next: Settings) => void;
  toast: (text: string) => void;
}) {
  const [draft, setDraft] = useState(() => priceDraft(view));
  const [invalid, setInvalid] = useState(false);
  const [busy, setBusy] = useState(false);
  useEffect(() => { setDraft(priceDraft(view)); setInvalid(false); }, [view]);
  if (!view) return null;
  const url = `/api/presets/${encodeURIComponent(id)}/price`;

  async function send(plan: boolean) {
    const { body } = priceBody(draft, plan);
    if (!body) { setInvalid(true); return; }
    setBusy(true);
    try { onSaved(await api.put<Settings>(url, body)); } catch (e) { toast((e as Error).message); } finally { setBusy(false); }
  }
  async function reset() {
    setBusy(true);
    try { onSaved(await api.delete<Settings>(url)); } catch (e) { toast((e as Error).message); } finally { setBusy(false); }
  }

  const fallback = view.default.source ? t(`price.source.${view.default.source}`) : t("price.source.none");
  const source = view.manual ? t("price.source.under", { source: fallback }) : fallback;
  const changed = priceChanged(draft, view);
  const lockedWhy = view.plan_source === "local" ? t("price.plan.local") : t("price.plan.provider");
  return (
    <div className="model-price" data-setting={`presets.${id}.price`}>
      <Row title={t("price.title")} desc={t(view.states_bill ? "price.order.bill" : "price.order")} stack>
        {view.plan_locked
          ? <span className="pill">{t("price.plan")}</span>
          : <Segmented
              label={t("price.billing")}
              value={view.plan ? "plan" : "metered"}
              options={[{ id: "plan", label: t("price.plan") }, { id: "metered", label: t("price.metered") }]}
              onChange={(choice) => { if (!busy) void send(choice === "plan"); }}
            />}
      </Row>
      {view.plan_locked && <div className="sub faint">{lockedWhy}</div>}
      <div className="mfields model-price-rates">
        {RATES.map((rate) => (
          <label key={rate} className="mfield">
            <span>{t(`price.${rate}`)}</span>
            <input
              className={`field num ${invalid ? "invalid" : ""}`}
              type="number"
              min={0}
              step="any"
              inputMode="decimal"
              disabled={view.plan || busy}
              value={draft[rate]}
              placeholder={rate === "cache_hit" ? t("price.cache.same") : t("price.unknown")}
              aria-invalid={invalid || undefined}
              onChange={(e) => { setDraft({ ...draft, [rate]: e.target.value }); setInvalid(false); }}
            />
          </label>
        ))}
      </div>
      {invalid && <p className="result-warning" role="alert">{t("price.invalid")}</p>}
      <div className="btnrow model-price-foot">
        <span className={`pill ${view.manual ? "accent" : ""}`}>{view.manual ? t("price.overridden") : t("price.default")}</span>
        <span className="sub">{source}</span>
        <span className="grow" />
        {changed && !view.plan && <button className="btn small primary" disabled={busy} onClick={() => void send(false)}>{t("common.save")}</button>}
        {view.manual && <button className="btn small" disabled={busy} onClick={() => void reset()}>{t("price.reset")}</button>}
      </div>
    </div>
  );
}
