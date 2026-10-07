// Adding a model: the one thing a fresh installation has to do before anything else works, and the
// same three steps when a second model is added later from Settings. A provider endpoint is an
// address, not a choice of model, so nothing is picked on the operator's behalf — but everything the
// endpoint is willing to say about a model (its window, whether it sees pictures, whether it
// reasons, what it costs) fills the form in, so the choice is one click and a glance, not research.
//
// What an endpoint's card says about its key comes from the key proxy, which is the only process
// that knows. The configuration says where an endpoint is, never whether anything behind it can
// authenticate, and reading readiness out of it is how an installation with one key came to offer
// six ready endpoints and fail on the first one picked.

import { ReactNode, useEffect, useMemo, useState } from "react";
import { api, Preset, Settings } from "../api";
import { Icon } from "../icons";
import { plural, t, useLang } from "../i18n";
import { LangPicker, Switch } from "../ui/index";
import { errorText, numInput } from "../ui";
import { BLANK, ModelEntry, Picked, PricingDraft, REASONING_EFFORTS, prefilled, presetIdFor, priceFor, pricingFromDraft, retyped } from "../models";
import { FreeModels } from "./FreeModels";
import { Billing, OPENCODE_KEY_URL, OPENCODE_PLANS, OpencodePlan, missingPlans, opencodePlanOf, opencodeProvider } from "../opencode";

export type { ModelEntry, Picked } from "../models";
export { presetIdFor } from "../models";

/** What a missing credential would be: a key the proxy holds, a CLI login, or nothing at all. */
export type KeyKind = "api_key" | "cli_login" | "endpoint";

export type ProviderCard = {
  id: string;
  kind: string;
  name?: string;
  base_url: string;
  billing?: Billing;
  via_proxy: boolean;
  /** Whether a credential really exists. `null` only when nothing could answer — the proxy is down. */
  key_held: boolean | null;
  key_kind: KeyKind;
  ready: boolean;
};

export type OnboardingState = {
  has_model: boolean;
  presets: number;
  default_preset: string;
  providers: ProviderCard[];
  /** Where an endpoint the app adds itself is reached: the key proxy, or "" for the vendor directly. */
  keyproxy_base?: string;
  needs: string[];
  message: string;
};

/** Not a provider id: the provider ids the server accepts cannot contain a space. */
const CUSTOM = "a new endpoint";
const LLAMACPP = "a new llama.cpp endpoint";
/** An OpenCode plan no endpoint is yet, picked to be added; the id after the prefix is the plan's. */
const OPENCODE_NEW = "a new OpenCode endpoint: ";
const KIND_NAMES: Record<string, string> = {
  deepseek: "DeepSeek",
  openrouter: "OpenRouter",
  opencode: "OpenCode",
  vllm: "vLLM",
  llamacpp: "llama.cpp",
};
/** Endpoints named after the tool whose login they borrow: the kind says nothing, the id does. */
const ID_NAMES: Record<string, string> = { codex: "Codex", grok: "Grok", claude: "Claude", openai: "OpenAI" };

function providerName(id: string, kind: string, name = "", billing?: Billing): string {
  return name || ID_NAMES[id] || opencodePlanOf(kind, billing)?.name || KIND_NAMES[kind] || id;
}

function money(usd: number): string {
  if (usd === 0) return t("add.pill.free");
  return usd < 1 ? `$${usd.toFixed(usd < 0.1 ? 3 : 2)}` : `$${usd.toFixed(2)}`;
}

function tokens(n: number): string {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

/** The one word on the card about its key, and the one line under it about what to do. */
function keyWords(p: ProviderCard): { pill: string; tone: string; note: string } {
  if (p.ready) {
    const pill = p.key_kind === "cli_login" ? t("add.key.signedin") : p.key_kind === "endpoint" ? t("add.key.free") : t("add.key.ready");
    return { pill, tone: "done", note: p.via_proxy ? t("add.key.held") : p.key_kind === "endpoint" ? t("add.key.free") : t("add.key.own") };
  }
  if (p.key_held === null) return { pill: t("add.key.unknown"), tone: "", note: "" };
  // An endpoint with nowhere to reach is not a key problem, whatever the key says: advice about a
  // key sends the reader to add one to something that still points nowhere.
  if (!p.base_url) return { pill: t("add.key.none"), tone: "pending", note: t("add.noaddress.hint") };
  const note = p.key_kind === "cli_login" ? t("add.key.hint.cli") : p.via_proxy ? t("add.key.hint.proxy") : t("add.key.hint.settings");
  return { pill: t("add.key.none"), tone: "pending", note };
}

/** A step of the flow: a card that lightens when it is the one being worked on. */
function Step({ n, title, sub, active, done, children }: { n: number; title: string; sub: string; active: boolean; done: boolean; children: ReactNode }) {
  return (
    <section className={`card step reveal ${active ? "on" : "waiting"}`} style={{ animationDelay: `${(n - 1) * 70}ms` }}>
      <div className="step-head">
        <span className={`step-n ${done ? "done" : ""}`}>{done ? <Icon name="check" size={14} /> : n}</span>
        <div className="grow">
          <b>{title}</b>
          <div className="sub">{sub}</div>
        </div>
      </div>
      {children}
    </section>
  );
}

/** Step 1: which endpoint the model runs on. */
function ProviderStep({ state, chosen, onPick }: { state: OnboardingState | null; chosen: string; onPick: (id: string) => void }) {
  const providers = state?.providers ?? [];
  const [filter, setFilter] = useState("");
  const query = filter.trim().toLocaleLowerCase();
  const ordered = [...providers.filter((p) => p.ready), ...providers.filter((p) => !p.ready)]
    .filter((p) => !query || `${providerName(p.id, p.kind, p.name, p.billing)} ${p.id}`.toLocaleLowerCase().includes(query));
  // Both ways of paying OpenCode stay on offer: a plan with no endpoint yet is a card that adds one.
  const absent = missingPlans(providers).filter((plan) => !query || plan.name.toLocaleLowerCase().includes(query));
  return (
    <>
    {providers.length > 8 && <label className="mfield" style={{ marginBottom: 12 }}>
      <span>{t("add.provider.search")}</span>
      <input className="field" value={filter} onChange={(e) => setFilter(e.target.value)} placeholder={t("add.provider.search")} />
    </label>}
    <div className="pickgrid">
      {ordered.map((p) => {
        const { pill, tone, note } = keyWords(p);
        const plan = opencodePlanOf(p.kind, p.billing);
        // An endpoint with no credential is not a choice: picking it produced a list of URLs and
        // HTTP codes, and the operator had to work backwards from those to "there is no key".
        const blocked = p.key_held === false;
        return (
          // `disabled` would take it out of the tab order, and the sentence under it — why this
          // endpoint cannot be used — is exactly what a keyboard or screen-reader user needs.
          // `aria-disabled` says the same thing and keeps the card reachable.
          <button key={p.id} className={`pick ${chosen === p.id ? "on" : ""} ${blocked ? "blocked" : ""}`} aria-disabled={blocked} onClick={() => !blocked && onPick(p.id)} aria-pressed={chosen === p.id}>
            <span className="pick-top">
              <b className="truncate">{providerName(p.id, p.kind, p.name, p.billing)}</b>
              <span className={`pill ${tone}`}>{pill}</span>
            </span>
            {plan && <span className="sub opencode-hint">{t(plan.hint)}</span>}
            <span className="sub mono truncate">{p.base_url || t("add.noaddress")}</span>
            {note && <span className="sub faint">{note}</span>}
          </button>
        );
      })}
      {absent.map((plan) => (
        <button key={plan.id} className={`pick dashed ${chosen === OPENCODE_NEW + plan.id ? "on" : ""}`} onClick={() => onPick(OPENCODE_NEW + plan.id)} aria-pressed={chosen === OPENCODE_NEW + plan.id}>
          <span className="pick-top">
            <b>{plan.name}</b>
            <span className="pill">{t("add.custom.new")}</span>
          </span>
          <span className="sub opencode-hint">{t(plan.hint)}</span>
        </button>
      ))}
      {(!query || "llama.cpp".includes(query)) && <button className={`pick dashed ${chosen === LLAMACPP ? "on" : ""}`} onClick={() => onPick(LLAMACPP)} aria-pressed={chosen === LLAMACPP}>
        <span className="pick-top">
          <b>{t("add.llamacpp")}</b>
          <span className="pill">{t("add.custom.new")}</span>
        </span>
        <span className="sub">{t("add.llamacpp.sub")}</span>
      </button>}
      {(!query || `${t("add.custom")} OpenAI API`.toLocaleLowerCase().includes(query)) && <button className={`pick dashed ${chosen === CUSTOM ? "on" : ""}`} onClick={() => onPick(CUSTOM)} aria-pressed={chosen === CUSTOM}>
        <span className="pick-top">
          <b>{t("add.custom")}</b>
          <span className="pill">{t("add.custom.new")}</span>
        </span>
        <span className="sub">{t("add.custom.sub")}</span>
      </button>}
    </div>
    </>
  );
}

/** Step 1b: the address and key of an endpoint this installation does not know yet. */
function CustomProvider({ kind, busy, onCreate }: { kind: "llamacpp" | "openai_compat"; busy: boolean; onCreate: (id: string, name: string, baseUrl: string, apiKey: string, kind: string) => void }) {
  const [id, setId] = useState("");
  const [baseUrl, setBaseUrl] = useState(kind === "llamacpp" ? "http://127.0.0.1:8080/v1" : "");
  const [apiKey, setApiKey] = useState("");
  const clean = id.trim().replace(/[^A-Za-z0-9._-]+/g, "-");
  return (
    <div className="mfields reveal" style={{ marginTop: 12 }}>
      <label className="mfield">
        <span>{t("add.custom.name")}</span>
        <input className="field" value={id} placeholder="workshop" onChange={(e) => setId(e.target.value)} />
      </label>
      <label className="mfield wide">
        <span>{t("add.custom.url")}</span>
        <input className="field mono" value={baseUrl} placeholder="http://&lt;host&gt;:&lt;port&gt;/v1" onChange={(e) => setBaseUrl(e.target.value)} spellCheck={false} />
      </label>
      <label className="mfield">
        <span>{t("add.custom.key")}</span>
        <input className="field" type="password" value={apiKey} placeholder="sk-…" onChange={(e) => setApiKey(e.target.value)} autoComplete="off" />
      </label>
      <div className="mfield end">
        <button className="btn primary" disabled={busy || !clean || !baseUrl.trim()} onClick={() => onCreate(clean, id.trim(), baseUrl.trim(), apiKey, kind)}>
          {busy ? t("add.custom.saving") : t("add.custom.save")}
        </button>
      </div>
    </div>
  );
}

/** Step 1b for OpenCode: the plan's endpoint, wired to its route; a key only when there is no key proxy to hold it. */
function OpencodeProvider({ plan, keyproxyBase, busy, onCreate }: { plan: OpencodePlan; keyproxyBase: string; busy: boolean; onCreate: (body: Record<string, string>) => void }) {
  const [apiKey, setApiKey] = useState("");
  const body = opencodeProvider(plan, keyproxyBase, apiKey.trim());
  return (
    <div className="mfields reveal" style={{ marginTop: 12 }}>
      <div className="mfield wide">
        <span className="sub">{t(plan.hint)}</span>
        <span className="sub mono truncate">{body.base_url}</span>
      </div>
      {!keyproxyBase && <label className="mfield wide">
        <span>{t("opencode.key")}</span>
        <input className="field" type="password" value={apiKey} placeholder="sk-…" onChange={(e) => setApiKey(e.target.value)} autoComplete="off" />
        <span className="sub">{t("free.key.get")} <a href={OPENCODE_KEY_URL} target="_blank" rel="noopener noreferrer">opencode.ai/auth ↗</a></span>
      </label>}
      {keyproxyBase && <div className="mfield wide"><span className="sub">{t("free.key.get")} <a href={OPENCODE_KEY_URL} target="_blank" rel="noopener noreferrer">opencode.ai/auth ↗</a></span></div>}
      <div className="mfield end">
        <button className="btn primary" disabled={busy || (!keyproxyBase && !apiKey.trim())} onClick={() => onCreate(body)}>
          {busy ? t("add.custom.saving") : t("opencode.save", { name: plan.name })}
        </button>
      </div>
    </div>
  );
}

/** Step 2: the models the endpoint serves, or a model id typed by hand. */
function ModelStep({ entries, loading, error, chosen, typed, onPick, onType, onRetry }: {
  entries: ModelEntry[] | null;
  loading: boolean;
  error: string;
  chosen: string;
  typed: string;
  onPick: (entry: ModelEntry) => void;
  onType: (id: string) => void;
  onRetry: () => void;
}) {
  const [filter, setFilter] = useState("");
  const q = filter.trim().toLowerCase();
  const shown = useMemo(
    () => (entries ?? []).filter((e) => !q || e.id.toLowerCase().includes(q) || (e.name ?? "").toLowerCase().includes(q)),
    [entries, q],
  );
  return (
    <>
      <div className="modelbar">
        <label className="mfield">
          {/* The field's name, then how many there are, apart: run together as "Filter 6 models" it
              read as an instruction to filter six of them. */}
          <span>{t("add.filter.plain")}{entries && <span className="faint"> · {plural("add.filter.count", entries.length)}</span>}</span>
          <input className="field" placeholder={t("add.filter.plain")} value={filter} onChange={(e) => setFilter(e.target.value)} disabled={!entries} />
        </label>
        <label className="mfield">
          <span>{t("add.typed")}</span>
          {/* Prefilled with whatever was picked from the list, and editable from there: an id the
              endpoint did not list is typed over it, not into an empty box beside it. */}
          <input className="field mono" placeholder={t("add.typed.hint")} value={typed} onChange={(e) => onType(e.target.value)} spellCheck={false} />
        </label>
      </div>
      {loading && <div className="empty calm">{t("add.asking")}</div>}
      {!loading && error && (
        <div className="empty calm reveal">
          <b>{t("add.nolist")}</b>
          <div className="sub">{error}</div>
          <div className="sub faint">{t("add.nolist.sub")}</div>
          <button className="btn small" onClick={onRetry}>{t("common.retry")}</button>
        </div>
      )}
      {!loading && !error && entries && shown.length === 0 && <div className="empty calm">{t("add.nomatch")}</div>}
      {!loading && shown.length > 0 && (
        <div className="modelgrid reveal">
          {shown.map((e) => (
            <button key={e.id} className={`pick model ${chosen === e.id ? "on" : ""}`} onClick={() => onPick(e)} aria-pressed={chosen === e.id}>
              <span className="pick-top">
                <b className="truncate">{e.name ?? e.id}</b>
                {chosen === e.id && <Icon name="check" size={16} />}
              </span>
              <span className="sub mono truncate">{e.id}</span>
              <span className="mtags">
                {e.context_length ? <span className="pill">{t("add.pill.context", { n: tokens(e.context_length) })}</span> : null}
                {e.images ? <span className="pill">{t("add.pill.images")}</span> : null}
                {e.reasoning ? <span className="pill">{t("add.pill.reasoning")}</span> : null}
                {e.pricing?.input !== undefined && e.pricing?.output !== undefined ? (
                  <>
                  {/* The price always starts its own line: left to wrap, it sat beside the tags on
                      one card and under them on its neighbour, and the cards stopped matching. */}
                  <span className="mtags-break" />
                  <span className="pill num">{t("add.pill.price", { in: money(e.pricing.input), out: money(e.pricing.output) })}</span>
                  </>
                ) : null}
              </span>
            </button>
          ))}
        </div>
      )}
    </>
  );
}

/**
 * The flow itself. `onSaved` receives the preset id and the settings that came back; the caller
 * decides what happens next — the first model ends onboarding, a later one closes the sheet.
 */
export function AddModel({ onSaved, onCancel, toast }: { onSaved: (presetId: string, settings: Settings) => void; onCancel?: () => void; toast: (t: string) => void }) {
  useLang();
  const [state, setState] = useState<OnboardingState | null>(null);
  const [provider, setProvider] = useState("");
  const [entries, setEntries] = useState<ModelEntry[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [lookupError, setLookupError] = useState("");
  const [typed, setTyped] = useState("");
  const [preset, setPreset] = useState<Preset>(BLANK);
  const [pricing, setPricing] = useState<ModelEntry["pricing"] | null>(null);
  const [priceDraft, setPriceDraft] = useState<PricingDraft | null>(null);
  const [priceError, setPriceError] = useState("");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<"regular" | "free">("regular");

  useEffect(() => {
    api.get<OnboardingState>("/api/onboarding").then(setState).catch((e) => toast(errorText(e)));
    api.get<Settings>("/api/settings").then(setSettings).catch(() => setSettings(null));
  }, [toast]);

  async function lookup(id: string) {
    setLoading(true);
    setLookupError("");
    setEntries(null);
    try {
      const r = await api.post<{ models: string[]; entries?: ModelEntry[]; discovery?: Record<string, unknown> }>("/api/providers/lookup-models", { provider: id });
      const found = r.entries ?? r.models.map((m) => ({ id: m }));
      setEntries(found);
      if (r.discovery && found.length === 1) pickModel(found[0]);
    } catch (e) {
      setLookupError(errorText(e));
    } finally {
      setLoading(false);
    }
  }

  function pickProvider(id: string) {
    setProvider(id);
    setEntries(null);
    setLookupError("");
    setTyped("");
    setPricing(null);
    setPriceDraft(null);
    setPriceError("");
    const adding = id === CUSTOM || id === LLAMACPP || id.startsWith(OPENCODE_NEW);
    setPreset({ ...BLANK, provider: adding ? "" : id });
    if (!adding) void lookup(id);
  }

  async function createProvider(id: string, name: string, baseUrl: string, apiKey: string, kind: string) {
    await putProvider(id, { kind, name, base_url: baseUrl, api_key: apiKey });
  }

  async function putProvider(id: string, body: Record<string, string>) {
    setBusy(true);
    try {
      await api.put<Settings>(`/api/providers/${encodeURIComponent(id)}`, body);
      setState(await api.get<OnboardingState>("/api/onboarding"));
      pickProvider(id);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  function pickModel(entry: ModelEntry) {
    setTyped(entry.id);
    setPricing(entry.pricing ?? null);
    setPriceDraft({
      model: entry.id,
      input: entry.pricing?.input === undefined ? "" : String(entry.pricing.input),
      output: entry.pricing?.output === undefined ? "" : String(entry.pricing.output),
      cache_hit: entry.pricing?.cache_hit === undefined ? "" : String(entry.pricing.cache_hit),
      input_limit: "",
      limit_source: "",
    });
    setPriceError("");
    setPreset((p) => prefilled(entry, p));
  }

  function typeModel(value: string) {
    setTyped(value);
    if (value.trim() !== typed.trim()) {
      setPriceDraft(null);
      setPriceError("");
    }
    const next = retyped(value, { preset, pricing });
    if (next.preset === preset) return;
    setPreset(next.preset);
    setPricing(next.pricing);
  }

  const model = (typed.trim() || preset.model).trim();
  const newPlan = OPENCODE_PLANS.find((plan) => OPENCODE_NEW + plan.id === provider) ?? null;
  const onEndpoint = !!provider && provider !== CUSTOM && provider !== LLAMACPP && !newPlan;
  const ready = onEndpoint && !!model;
  const providerCard = state?.providers.find((entry) => entry.id === provider);
  const providerKind = providerCard?.kind;
  const prepaid = providerCard?.billing === "subscription";
  const hasSpendCap = !!settings && (settings.limits.usd_per_run > 0 || settings.limits.usd_total > 0
    || (settings.limits.usd_total_per_provider?.[provider] ?? 0) > 0);
  const visibleDraft = priceDraft?.model === model ? priceDraft : null;

  function editPrice(field: Exclude<keyof PricingDraft, "model">, value: string) {
    if (!model) return;
    setPriceDraft((current) => ({
      ...(current?.model === model ? current : { model, input: "", output: "", cache_hit: "", input_limit: "", limit_source: "" }),
      [field]: value,
    }));
    setPriceError("");
  }

  async function save() {
    if (!ready) return;
    const parsed = pricingFromDraft(model, priceDraft);
    if (parsed.error) {
      setPriceError(t(`add.pricing.error.${parsed.error}`));
      return;
    }
    setBusy(true);
    const id = presetIdFor(provider, model);
    try {
      // Discovery rates and an operator-declared provider ceiling belong to the exact model id;
      // an editable preset context window cannot bound the provider's billable input.
      const price = parsed.entry ?? priceFor(model, { preset, pricing });
      if (price) {
        const current = (await api.get<Settings>("/api/settings")).providers[provider]?.pricing ?? {};
        const previous = current[model];
        const priorEntry = previous && typeof previous === "object" && !Array.isArray(previous) ? previous as Record<string, unknown> : {};
        await api.put<Settings>(`/api/providers/${encodeURIComponent(provider)}`, { pricing: { ...current, [model]: { ...priorEntry, ...price } } });
      }
      const next = await api.put<Settings>(`/api/presets/${encodeURIComponent(id)}`, { ...preset, provider, model });
      onSaved(id, next);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="addmodel">
      <div className="segmented" role="tablist" aria-label={t("add.step1")} style={{ marginBottom: 18 }}>
        <button role="tab" aria-selected={mode === "regular"} className={mode === "regular" ? "on" : ""} onClick={() => setMode("regular")}>{t("add.step1")}</button>
        <button role="tab" aria-selected={mode === "free"} className={mode === "free" ? "on" : ""} onClick={() => setMode("free")}>{t("free.tab")}</button>
      </div>
      {mode === "free" ? <FreeModels onSaved={onSaved} toast={toast} /> : <>
      <Step n={1} title={t("add.step1")} sub={t("add.step1.sub")} active={!provider} done={!!provider}>
        <ProviderStep state={state} chosen={provider} onPick={pickProvider} />
        {provider === CUSTOM && <CustomProvider key={CUSTOM} kind="openai_compat" busy={busy} onCreate={createProvider} />}
        {provider === LLAMACPP && <CustomProvider key={LLAMACPP} kind="llamacpp" busy={busy} onCreate={createProvider} />}
        {newPlan && <OpencodeProvider key={newPlan.id} plan={newPlan} keyproxyBase={state?.keyproxy_base ?? ""} busy={busy} onCreate={(body) => void putProvider(newPlan.id, body)} />}
      </Step>

      <Step n={2} title={t("add.step2")} sub={t("add.step2.sub")} active={onEndpoint && !model} done={!!model}>
        {onEndpoint ? (
          <ModelStep entries={entries} loading={loading} error={lookupError} chosen={model} typed={typed} onPick={pickModel} onType={typeModel} onRetry={() => void lookup(provider)} />
        ) : (
          <div className="sub faint">{t("add.step2.wait")}</div>
        )}
      </Step>

      <Step n={3} title={t("add.step3")} sub={t("add.step3.sub")} active={ready} done={ready}>
        <div className="mfields">
          <label className="mfield">
            <span>{t("add.label")}</span>
            <input className="field" value={preset.label} placeholder={model ? `${provider}/${model}` : t("add.label.hint")} onChange={(e) => setPreset({ ...preset, label: e.target.value })} />
          </label>
          <label className="mfield">
            <span>{t("add.window")}</span>
            <input className="field num" type="number" min={8000} step={1000} value={preset.context_window} onChange={(e) => { const v = numInput(e.target.value); if (v !== null) setPreset({ ...preset, context_window: v }); }} />
          </label>
          <label className="mfield">
            <span>{t("add.output")}</span>
            <input className="field num" type="number" min={1024} step={1000} value={preset.max_output_tokens} onChange={(e) => { const v = numInput(e.target.value); if (v !== null) setPreset({ ...preset, max_output_tokens: v }); }} />
          </label>
        </div>
        {/* Two switches and one picker, each with its own word. They were two filled "thinking on"
            and "images on" buttons around a segmented picker: two visual languages for "on" in one
            row, and a word on a button that could be read as the state or as the action. */}
        <div className="btnrow addmodel-run">
          <label className="addmodel-opt">
            <Switch checked={preset.thinking} onChange={(thinking) => setPreset({ ...preset, thinking })} label={t("add.thinking")} />
            <span>{t("add.thinking")}</span>
          </label>
          <div className="segmented inline" role="group" aria-label={t("add.effort")}>
            {REASONING_EFFORTS.map((e) => (
              <button key={e} className={preset.reasoning_effort === e ? "on" : ""} disabled={!preset.thinking} onClick={() => setPreset({ ...preset, reasoning_effort: e })}>
                {t(`add.effort.${e}`)}
              </button>
            ))}
          </div>
          <label className="addmodel-opt" title={t("add.images.title")}>
            <Switch checked={preset.images} onChange={(images) => setPreset({ ...preset, images })} label={t("add.images")} />
            <span>{t("add.images")}</span>
          </label>
        </div>
        {/* A prepaid plan has no per-token price to record, and a rate typed here would be counted as dollars spent. */}
        {onEndpoint && prepaid && <p className="sub addmodel-prepaid">{t("add.pricing.subscription")}</p>}
        {onEndpoint && providerKind !== "llamacpp" && !prepaid && <>
          {hasSpendCap && <p className="sub">{t("add.pricing.capHint")}</p>}
          <details className="sheet-section addmodel-pricing">
            <summary>{t("add.pricing.title")}</summary>
            <p className="sub">{t("add.pricing.help")}</p>
            <div className="mfields">
              <label className="mfield"><span>{t("add.pricing.input")}</span>
                <input className="field num" type="number" min={0} step="any" value={visibleDraft?.input ?? ""} onChange={(e) => editPrice("input", e.target.value)} /></label>
              <label className="mfield"><span>{t("add.pricing.output")}</span>
                <input className="field num" type="number" min={0} step="any" value={visibleDraft?.output ?? ""} onChange={(e) => editPrice("output", e.target.value)} /></label>
              <label className="mfield"><span>{t("add.pricing.cache")}</span>
                <input className="field num" type="number" min={0} step="any" value={visibleDraft?.cache_hit ?? ""} onChange={(e) => editPrice("cache_hit", e.target.value)} /></label>
              <label className="mfield"><span>{t("add.pricing.ceiling")}</span>
                <input className="field num" type="number" min={1} step={1} value={visibleDraft?.input_limit ?? ""} onChange={(e) => editPrice("input_limit", e.target.value)} /></label>
              <label className="mfield wide"><span>{t("add.pricing.source")}</span>
                <input className="field" value={visibleDraft?.limit_source ?? ""} onChange={(e) => editPrice("limit_source", e.target.value)} maxLength={500} placeholder={t("add.pricing.sourceHint")} /></label>
            </div>
            <p className="sub">{t("add.pricing.windowSeparate")}</p>
          </details>
          {priceError && <p className="result-warning" role="alert">{priceError}</p>}
        </>}
      </Step>

      <div className="addmodel-foot">
        {/* The name this model will be known by, once there is one to show; a sentence until then,
            and a sentence is not monospaced. It wraps rather than truncates: this is the last look
            at what is about to be created, and a phone cut it to "openrouter.anthropi…". */}
        <div className={`sub addmodel-name ${ready ? "mono" : ""}`}>{ready ? presetIdFor(provider, model) : t("add.foot.empty")}</div>
        <span className="grow" />
        {onCancel && (
          <button className="btn" onClick={onCancel}>
            {t("common.cancel")}
          </button>
        )}
        <button className="btn primary" disabled={!ready || busy} onClick={() => void save()}>
          {busy ? t("add.saving") : state?.has_model ? t("add.save") : t("add.save.first")}
        </button>
      </div>
      </>}
    </div>
  );
}

/**
 * The first-run page: the app opens here until a model exists, because nothing else can work yet.
 *
 * Outside the shell on purpose, the way the login page is. The wide shell is a grid whose first
 * column is the rail's, and a page drawn inside it without a rail sits in the second column with the
 * rail's width of nothing beside it — which is exactly what a 2000 px window showed.
 */
export function OnboardingScreen({ onDone, toast }: { onDone: () => void; toast: (t: string) => void }) {
  useLang();
  return (
    <div className="gate tall">
      <div className="onboard">
        <header className="onboard-head">
          <img className="onboard-logo" src="/app/icons/icon-192.png" alt="" width={44} height={44} />
          <div className="grow">
            <h1>{t("add.title")}</h1>
            <p className="sub">{t("add.sub")}</p>
          </div>
          <LangPicker />
        </header>
        <AddModel toast={toast} onSaved={onDone} />
      </div>
    </div>
  );
}
