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
import { api, Preset, PROVIDER_KEYS_CHANGED, Settings } from "../api";
import { Icon } from "../icons";
import { plural, t, useLang } from "../i18n";
import { LangPicker, Segmented, Switch } from "../ui/index";
import { Badge, Button, Field } from "../ui/primitives";
import { errorText, numInput } from "../ui";
import { BLANK, ModelEntry, Picked, PricingDraft, REASONING_EFFORTS, RouteListing, prefilled, presetIdFor, priceFor, pricingFromDraft, retyped, routeBill } from "../models";
import { FreeModels } from "./FreeModels";
import { OpenRouterProviders } from "./OpenRouterProviders";
import { ProviderMark } from "../ui/provider-mark";
import { useProviderTest } from "../providertest";
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
const ID_NAMES: Record<string, string> = { codex: "Codex", grok: "Grok", claude: "Claude", antigravity: "Google (Antigravity)", openai: "OpenAI" };

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

/** A step of the flow: its label in the page's section-label voice, then what it holds. The number says
 *  which step is being worked on, in ink; a step that is not the current one keeps every colour it had. */
function Step({ n, title, sub, active, done, children }: { n: number; title: string; sub: string; active: boolean; done: boolean; children: ReactNode }) {
  return (
    <section className={`step ${active ? "on" : "waiting"}`}>
      <div className="step-head">
        <span className={`step-n ${done ? "done" : ""}`}>{done ? <Icon name="check" size={12} /> : n}</span>
        <b>{title}</b>
        <span className="step-sub">{sub}</span>
      </div>
      {children}
    </section>
  );
}

/** A plan's line on its row, without the name the row already shows ("OpenCode Go — subscription…"). */
function planLine(plan: OpencodePlan): string {
  const hint = t(plan.hint);
  const rest = hint.startsWith(plan.name) ? hint.slice(plan.name.length).replace(/^\s*[—–-]\s*/, "") : hint;
  return rest.charAt(0).toLocaleUpperCase() + rest.slice(1);
}

/** The kinds an operator runs on its own machine or network, listed under "Your own". */
const OWN_KINDS = new Set(["llamacpp", "vllm", "openai_compat"]);

/**
 * Step 1: the endpoints as rows in three groups — the OpenCode plans (a subscription and a gateway),
 * the providers, and the ones of the operator's own — each with its mark, what it costs or what it
 * needs, and one word about its key. The same rows at every width: the phone's list and the desktop's
 * cards said the same things in two shapes. An endpoint's address is not on its row (an internal
 * address such as the key proxy's told a newcomer nothing); it is under the connection test of the
 * endpoint picked, where it matters.
 */
function ProviderStep({ state, chosen, onPick }: { state: OnboardingState | null; chosen: string; onPick: (id: string) => void }) {
  const providers = state?.providers ?? [];
  const [filter, setFilter] = useState("");
  const query = filter.trim().toLocaleLowerCase();
  const ordered = [...providers.filter((p) => p.ready), ...providers.filter((p) => !p.ready)]
    .filter((p) => !query || `${providerName(p.id, p.kind, p.name, p.billing)} ${p.id}`.toLocaleLowerCase().includes(query));
  // An OpenAI-compatible endpoint is the operator's own only when it needs no credential; one that
  // borrows a CLI's login (Codex, Claude, Grok) is a provider like any other.
  const ownKind = (p: ProviderCard) => OWN_KINDS.has(p.kind) && (p.kind !== "openai_compat" || p.key_kind === "endpoint");
  const gateways = ordered.filter((p) => opencodePlanOf(p.kind, p.billing));
  const own = ordered.filter((p) => !opencodePlanOf(p.kind, p.billing) && ownKind(p));
  const rest = ordered.filter((p) => !opencodePlanOf(p.kind, p.billing) && !ownKind(p));
  // Both ways of paying OpenCode stay on offer: a plan with no endpoint yet is a row that adds one.
  const absent = missingPlans(providers).filter((plan) => !query || plan.name.toLocaleLowerCase().includes(query));
  const showLlama = !query || "llama.cpp".includes(query);
  const showCustom = !query || `${t("add.custom")} OpenAI API`.toLocaleLowerCase().includes(query);
  const row = (p: ProviderCard) => {
    const { pill, tone, note } = keyWords(p);
    const plan = opencodePlanOf(p.kind, p.billing);
    const name = providerName(p.id, p.kind, p.name, p.billing);
    // An endpoint with no credential is not a choice: picking it produced a list of URLs and HTTP
    // codes, and the operator had to work backwards from those to "there is no key". `disabled` would
    // take it out of the tab order, and the sentence under it — why this endpoint cannot be used — is
    // exactly what a keyboard or screen-reader user needs: `aria-disabled` keeps the row reachable.
    const blocked = p.key_held === false;
    return (
      <button key={p.id} type="button" className={`pick ${chosen === p.id ? "on" : ""} ${blocked ? "blocked" : ""}`} aria-disabled={blocked} onClick={() => !blocked && onPick(p.id)} aria-pressed={chosen === p.id}>
        <ProviderMark id={p.id} kind={p.kind} name={name} />
        <span className="pick-main">
          <b className="truncate">{name}</b>
          <span className="pick-line">{plan ? planLine(plan) : note || t(`add.kind.${ownKind(p) ? "own" : "provider"}`)}</span>
        </span>
        <span className="pick-state" data-tone={tone === "done" ? "live" : tone === "pending" ? "attention" : "idle"}><span className="pick-dot" aria-hidden />{pill}</span>
      </button>
    );
  };
  // A new endpoint of a known family (an OpenCode plan, a llama.cpp server) shows the mark it will
  // have once added; only the blank custom address keeps a plain glyph, as it has no name yet.
  const fresh = (id: string, title: string, sub: string, mark: string | null) => (
    <button key={id} type="button" className={`pick dashed ${chosen === id ? "on" : ""}`} onClick={() => onPick(id)} aria-pressed={chosen === id}>
      {mark ? <ProviderMark id={mark} name={title} /> : <span className="provider-mark"><Icon name="link" size={16} /></span>}
      <span className="pick-main">
        <b className="truncate">{title}</b>
        <span className="pick-line">{sub}</span>
      </span>
      <span className="pick-state" data-tone="idle">{t("add.custom.new")}</span>
    </button>
  );
  const group = (label: string, rows: ReactNode[]) => rows.length > 0 && (
    <div className="pick-group">
      <div className="pick-label">{label}</div>
      <div className="pickgrid">{rows}</div>
    </div>
  );
  return (
    <>
      {providers.length > 8 && <Field label={t("add.provider.search")} value={filter} onChange={(e) => setFilter(e.target.value)} placeholder={t("add.provider.search")} />}
      {group(t("add.group.gateways"), [...gateways.map(row), ...absent.map((plan) => fresh(OPENCODE_NEW + plan.id, plan.name, planLine(plan), "opencode"))])}
      {group(t("add.group.providers"), rest.map(row))}
      {group(t("add.group.own"), [...own.map(row), ...(showLlama ? [fresh(LLAMACPP, t("add.llamacpp"), t("add.llamacpp.sub"), "llamacpp")] : []), ...(showCustom ? [fresh(CUSTOM, t("add.custom"), t("add.custom.sub"), null)] : [])])}
    </>
  );
}

/** The connection test of the endpoint picked: one press, and what it answered or why it did not. The
 *  address it asks is here and nowhere else in the flow. */
function ConnectionTest({ card }: { card: ProviderCard }) {
  const { test, run, note } = useProviderTest(card.id);
  return (
    <div className="pick-test">
      <Button size="sm" icon="loop" busy={test?.state === "running"} onClick={() => void run()}>{t("prov.test")}</Button>
      <span className="pick-test-note" role="status" data-tone={note?.tone} data-test={test?.state === "done" ? (test.ok ? "ok" : "failed") : test?.state}>{note?.text ?? ""}</span>
      {card.base_url && <span className="pick-test-addr mono truncate" title={card.base_url}>{card.base_url}</span>}
    </div>
  );
}

/** Step 1b: the address and key of an endpoint this installation does not know yet. */
function CustomProvider({ kind, busy, onCreate }: { kind: "llamacpp" | "openai_compat"; busy: boolean; onCreate: (id: string, name: string, baseUrl: string, apiKey: string, kind: string) => void }) {
  const [id, setId] = useState("");
  const [baseUrl, setBaseUrl] = useState(kind === "llamacpp" ? "http://127.0.0.1:8080/v1" : "");
  const [apiKey, setApiKey] = useState("");
  const clean = id.trim().replace(/[^A-Za-z0-9._-]+/g, "-");
  return (
    <div className="addmodel-form mfields">
      <Field label={t("add.custom.name")} value={id} placeholder="workshop" onChange={(e) => setId(e.target.value)} />
      <Field label={t("add.custom.url")} mono value={baseUrl} placeholder="http://<host>:<port>/v1" onChange={(e) => setBaseUrl(e.target.value)} spellCheck={false} />
      <Field label={t("add.custom.key")} type="password" value={apiKey} placeholder="sk-…" onChange={(e) => setApiKey(e.target.value)} autoComplete="off" />
      <div className="addmodel-form-end">
        <Button variant="primary" disabled={!clean || !baseUrl.trim()} busy={busy} onClick={() => onCreate(clean, id.trim(), baseUrl.trim(), apiKey, kind)}>
          {busy ? t("add.custom.saving") : t("add.custom.save")}
        </Button>
      </div>
    </div>
  );
}

/** Step 1b for OpenCode: the plan's endpoint, wired to its route; a key only when there is no key proxy to hold it. */
function OpencodeProvider({ plan, keyproxyBase, busy, onCreate }: { plan: OpencodePlan; keyproxyBase: string; busy: boolean; onCreate: (body: Record<string, string>) => void }) {
  const [apiKey, setApiKey] = useState("");
  const body = opencodeProvider(plan, keyproxyBase, apiKey.trim());
  return (
    <div className="addmodel-form mfields">
      <div className="mfield wide">
        <span className="sub">{t(plan.hint)}</span>
        <span className="sub mono truncate">{body.base_url}</span>
      </div>
      {!keyproxyBase && (
        <div className="mfield wide">
          <Field label={t("opencode.key")} type="password" value={apiKey} placeholder="sk-…" onChange={(e) => setApiKey(e.target.value)} autoComplete="off"
            hint={<>{t("free.key.get")} <a href={OPENCODE_KEY_URL} target="_blank" rel="noopener noreferrer">opencode.ai/auth ↗</a></>} />
        </div>
      )}
      {keyproxyBase && <div className="mfield wide"><span className="sub">{t("free.key.get")} <a href={OPENCODE_KEY_URL} target="_blank" rel="noopener noreferrer">opencode.ai/auth ↗</a></span></div>}
      <div className="addmodel-form-end">
        <Button variant="primary" disabled={!keyproxyBase && !apiKey.trim()} busy={busy} onClick={() => onCreate(body)}>
          {busy ? t("add.custom.saving") : t("opencode.save", { name: plan.name })}
        </Button>
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
        {/* The field's name, then how many there are, apart: run together as "Filter 6 models" it
            read as an instruction to filter six of them. */}
        <Field label={<>{t("add.filter.plain")}{entries && <span className="faint"> · {plural("add.filter.count", entries.length)}</span>}</>}
          placeholder={t("add.filter.plain")} value={filter} onChange={(e) => setFilter(e.target.value)} disabled={!entries} />
        {/* Prefilled with whatever was picked from the list, and editable from there: an id the
            endpoint did not list is typed over it, not into an empty box beside it. */}
        <Field label={t("add.typed")} mono placeholder={t("add.typed.hint")} value={typed} onChange={(e) => onType(e.target.value)} spellCheck={false} />
      </div>
      {loading && <div className="addmodel-note">{t("add.asking")}</div>}
      {!loading && error && (
        <div className="addmodel-note">
          <b>{t("add.nolist")}</b>
          <div className="sub">{error}</div>
          <div className="sub faint">{t("add.nolist.sub")}</div>
          <Button size="sm" onClick={onRetry}>{t("common.retry")}</Button>
        </div>
      )}
      {!loading && !error && entries && shown.length === 0 && <div className="addmodel-note">{t("add.nomatch")}</div>}
      {!loading && shown.length > 0 && (
        <div className="modelgrid pickgrid">
          {shown.map((e) => (
            <button key={e.id} type="button" className={`pick model ${chosen === e.id ? "on" : ""}`} onClick={() => onPick(e)} aria-pressed={chosen === e.id}>
              <span className="pick-main">
                <b className="truncate">{e.name ?? e.id}</b>
                <span className="pick-line mono truncate">{e.id}</span>
              </span>
              <span className="mtags">
                {e.context_length ? <Badge>{t("add.pill.context", { n: tokens(e.context_length) })}</Badge> : null}
                {e.images ? <Badge>{t("add.pill.images")}</Badge> : null}
                {e.reasoning ? <Badge>{t("add.pill.reasoning")}</Badge> : null}
                {e.pricing?.input !== undefined && e.pricing?.output !== undefined ? <span className="pick-price num">{t("add.pill.price", { in: money(e.pricing.input), out: money(e.pricing.output) })}</span> : null}
              </span>
              <span className="pick-check" aria-hidden>{chosen === e.id && <Icon name="check" size={16} />}</span>
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
  // The window is found by the host unless the operator types one; then it is theirs and kept.
  const [windowTyped, setWindowTyped] = useState(false);
  const [windowSource, setWindowSource] = useState<ModelEntry["context_source"] | null>(null);
  const [pricing, setPricing] = useState<ModelEntry["pricing"] | null>(null);
  const [priceDraft, setPriceDraft] = useState<PricingDraft | null>(null);
  const [priceError, setPriceError] = useState("");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<"regular" | "free">("regular");
  const [routes, setRoutes] = useState<RouteListing | null>(null);
  const [routesAsked, setRoutesAsked] = useState(false);
  const [chosenRoutes, setChosenRoutes] = useState<string[]>([]);

  useEffect(() => {
    api.get<OnboardingState>("/api/onboarding").then(setState).catch((e) => toast(errorText(e)));
    api.get<Settings>("/api/settings").then(setSettings).catch(() => setSettings(null));
  }, [toast]);

  // A key saved in Settings while this screen is open changes what its cards say; without asking
  // again the card went on saying "no key" for a key that had just been stored.
  useEffect(() => {
    const refresh = () => { api.get<OnboardingState>("/api/onboarding").then(setState).catch(() => undefined); };
    window.addEventListener(PROVIDER_KEYS_CHANGED, refresh);
    return () => window.removeEventListener(PROVIDER_KEYS_CHANGED, refresh);
  }, []);

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
    setWindowTyped(false);
    setWindowSource(null);
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
    setWindowSource(entry.context_source ?? null);
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
  const onRouter = providerKind === "openrouter";

  // A short wait, because a model id can also be typed: asking on every keystroke would ask about
  // every prefix of it. Nothing here can stop the flow: no list, an empty one or a failure all leave
  // the step out and the model is added as it always was, on OpenRouter's own routing.
  useEffect(() => {
    setRoutes(null);
    setChosenRoutes([]);
    if (!onRouter || !model.includes("/")) {
      setRoutesAsked(false);
      return;
    }
    let current = true;
    setRoutesAsked(true);
    const timer = setTimeout(() => {
      api.get<RouteListing>(`/api/providers/${encodeURIComponent(provider)}/model-providers?model=${encodeURIComponent(model)}`)
        .then((listing) => { if (current) setRoutes(listing.rows.length ? listing : null); })
        .catch(() => { if (current) setRoutes(null); })
        .finally(() => { if (current) setRoutesAsked(false); });
    }, 300);
    return () => { current = false; clearTimeout(timer); };
  }, [onRouter, provider, model]);

  /** A choice of providers is a price: the form shows the one that will be recorded. */
  function chooseRoutes(next: string[]) {
    setChosenRoutes(next);
    const bill = routeBill(routes?.rows ?? [], next) ?? pricing;
    const written = (value: number | null | undefined) => (value === undefined || value === null ? "" : String(value));
    setPriceDraft((current) => ({ model, input: written(bill?.input), output: written(bill?.output), cache_hit: written(bill?.cache_hit), input_limit: current?.input_limit ?? "", limit_source: current?.limit_source ?? "" }));
  }

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
      const next = await api.put<Settings>(`/api/presets/${encodeURIComponent(id)}`, { ...preset, provider, model, context_window_source: windowTyped ? "manual" : "auto", ...(onRouter ? { openrouter_providers: chosenRoutes } : {}) });
      onSaved(id, next);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const modes = [{ id: "regular" as const, label: t("add.tab.providers") }, { id: "free" as const, label: t("free.tab") }];
  return (
    <div className="addmodel">
      <Segmented value={mode} options={modes} onChange={setMode} label={t("add.title")} className="addmodel-mode" />
      {mode === "free" ? <FreeModels onSaved={onSaved} toast={toast} /> : <>
      <Step n={1} title={t("add.step1")} sub={t("add.step1.sub")} active={!provider} done={!!provider}>
        <ProviderStep state={state} chosen={provider} onPick={pickProvider} />
        {provider === CUSTOM && <CustomProvider key={CUSTOM} kind="openai_compat" busy={busy} onCreate={createProvider} />}
        {provider === LLAMACPP && <CustomProvider key={LLAMACPP} kind="llamacpp" busy={busy} onCreate={createProvider} />}
        {newPlan && <OpencodeProvider key={newPlan.id} plan={newPlan} keyproxyBase={state?.keyproxy_base ?? ""} busy={busy} onCreate={(body) => void putProvider(newPlan.id, body)} />}
        {providerCard && <ConnectionTest card={providerCard} />}
      </Step>

      <Step n={2} title={t("add.step2")} sub={t("add.step2.sub")} active={onEndpoint && !model} done={!!model}>
        {onEndpoint ? (
          <ModelStep entries={entries} loading={loading} error={lookupError} chosen={model} typed={typed} onPick={pickModel} onType={typeModel} onRetry={() => void lookup(provider)} />
        ) : (
          <div className="sub faint">{t("add.step2.wait")}</div>
        )}
      </Step>

      {(routes || routesAsked) && (
        <Step n={3} title={t("add.routes")} sub={t("add.routes.sub")} active={ready} done={chosenRoutes.length > 0}>
          {routes ? <OpenRouterProviders listing={routes} chosen={chosenRoutes} onChange={chooseRoutes} /> : <div className="sub faint">{t("add.routes.asking")}</div>}
        </Step>
      )}

      <Step n={routes || routesAsked ? 4 : 3} title={t("add.step3")} sub={t("add.step3.sub")} active={ready} done={ready}>
        <div className="mfields addmodel-fields">
          <Field label={t("add.label")} value={preset.label} placeholder={model ? `${provider}/${model}` : t("add.label.hint")} onChange={(e) => setPreset({ ...preset, label: e.target.value })} />
          <Field label={t("add.window")} className="num" type="number" min={8000} step={1000} value={preset.context_window}
            onChange={(e) => { const v = numInput(e.target.value); if (v !== null) { setPreset({ ...preset, context_window: v }); setWindowTyped(true); } }}
            hint={<span data-testid="add-window-source">{windowTyped ? t("window.typed") : windowSource ? t("window.found", { source: t(`window.by.${windowSource}`) }) : t("window.default")}</span>} />
          <Field label={t("add.output")} className="num" type="number" min={1024} step={1000} value={preset.max_output_tokens}
            onChange={(e) => { const v = numInput(e.target.value); if (v !== null) setPreset({ ...preset, max_output_tokens: v }); }} />
        </div>
        {/* Two switches and one picker, each with its own word, in one card of rows: the reasoning's
            effort is a choice that only means something while thinking is on. */}
        <div className="addmodel-card">
          <div className="addmodel-row">
            <label className="addmodel-opt">
              <Switch checked={preset.thinking} onChange={(thinking) => setPreset({ ...preset, thinking })} label={t("add.thinking")} />
              <span>{t("add.thinking")}</span>
            </label>
            <Segmented value={preset.reasoning_effort} options={REASONING_EFFORTS.map((e) => ({ id: e, label: t(`add.effort.${e}`), disabled: !preset.thinking }))}
              onChange={(reasoning_effort) => setPreset({ ...preset, reasoning_effort })} label={t("add.effort")} />
          </div>
          <div className="addmodel-row" title={t("add.images.title")}>
            <label className="addmodel-opt">
              <Switch checked={preset.images} onChange={(images) => setPreset({ ...preset, images })} label={t("add.images")} />
              <span>{t("add.images")}</span>
            </label>
          </div>
          {onEndpoint && providerKind !== "llamacpp" && !prepaid && (
            <details className="addmodel-pricing">
              <summary>{t("add.pricing.title")}</summary>
              <p className="sub">{t("add.pricing.help")}</p>
              <div className="mfields">
                <Field label={t("add.pricing.input")} className="num" type="number" min={0} step="any" disabled={chosenRoutes.length > 0} value={visibleDraft?.input ?? ""} onChange={(e) => editPrice("input", e.target.value)} />
                <Field label={t("add.pricing.output")} className="num" type="number" min={0} step="any" disabled={chosenRoutes.length > 0} value={visibleDraft?.output ?? ""} onChange={(e) => editPrice("output", e.target.value)} />
                <Field label={t("add.pricing.cache")} className="num" type="number" min={0} step="any" disabled={chosenRoutes.length > 0} value={visibleDraft?.cache_hit ?? ""} onChange={(e) => editPrice("cache_hit", e.target.value)} />
                <Field label={t("add.pricing.ceiling")} className="num" type="number" min={1} step={1} value={visibleDraft?.input_limit ?? ""} onChange={(e) => editPrice("input_limit", e.target.value)} />
                <div className="mfield wide">
                  <Field label={t("add.pricing.source")} value={visibleDraft?.limit_source ?? ""} onChange={(e) => editPrice("limit_source", e.target.value)} maxLength={500} placeholder={t("add.pricing.sourceHint")} />
                </div>
              </div>
              <p className="sub">{t("add.pricing.windowSeparate")}</p>
            </details>
          )}
        </div>
        {/* A prepaid plan has no per-token price to record, and a rate typed here would be counted as dollars spent. */}
        {onEndpoint && prepaid && <p className="sub addmodel-prepaid">{t("add.pricing.subscription")}</p>}
        {onEndpoint && providerKind !== "llamacpp" && !prepaid && hasSpendCap && <p className="sub">{t("add.pricing.capHint")}</p>}
        {priceError && <p className="result-warning" role="alert">{priceError}</p>}
      </Step>

      <div className="addmodel-foot">
        {/* The name this model will be known by, once there is one to show; a sentence until then,
            and a sentence is not monospaced. It wraps rather than truncates: this is the last look
            at what is about to be created, and a phone cut it to "openrouter.anthropi…". */}
        <div className={`sub addmodel-name ${ready ? "mono" : ""}`}>{ready ? presetIdFor(provider, model) : t("add.foot.empty")}</div>
        {onCancel && <Button onClick={onCancel}>{t("common.cancel")}</Button>}
        <Button variant="primary" disabled={!ready} busy={busy} onClick={() => void save()}>
          {busy ? t("add.saving") : state?.has_model ? t("add.save") : t("add.save.first")}
        </Button>
      </div>
      </>}
    </div>
  );
}

/**
 * The first-run page: the app opens here until a model exists, because nothing else can work yet.
 *
 * Outside the shell on purpose, the way the login page is: until a model exists the sidebar has
 * nothing to lead to, and a page drawn inside the shell sat beside a column of destinations that
 * could not work yet. One centred column, as the start page is: the question, then the steps under it.
 */
export function OnboardingScreen({ onDone, toast }: { onDone: () => void; toast: (t: string) => void }) {
  useLang();
  return (
    <div className="gate tall">
      <div className="onboard">
        <div className="onboard-bar"><LangPicker /></div>
        <header className="onboard-head">
          <h1>{t("add.title")}</h1>
          <p className="sub">{t("add.sub")}</p>
        </header>
        <AddModel toast={toast} onSaved={onDone} />
      </div>
    </div>
  );
}
