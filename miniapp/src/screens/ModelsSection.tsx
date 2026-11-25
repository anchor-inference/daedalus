// Settings → Models and providers: the endpoints models run on, each with its own models under it,
// the three defaults a session opens with, and the chain of models the agent falls back along.
//
// Providers and models were two sections once, and a model's row named its provider by an id the
// reader then had to find on the other page. A model belongs to exactly one provider, so it is drawn
// inside it; the provider's own fields (address, key, temperature, its spend cap) sit behind its
// Connection button, and a model's behind its row, so the list stays one line a thing to scan.

import { useEffect, useMemo, useRef, useState } from "react";
import { api, announceProviderKeys, type ContextWindowSource, type ContextWindowSources, type Preset, type PriceView, type ProviderConf, type Settings } from "../api";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { confirmAsync, numInput } from "../ui";
import { Select, SelectionList, Switch } from "../ui/index";
import { Row } from "../settingsrow";
import { tokens } from "../format";
import { ModelPrice } from "../modelprice";
import { modelItems } from "../modelitems";
import { ON_DEMAND_CHOICES, REASONING_EFFORTS, type RouteListing, onDemandGroups } from "../models";
import { OpenRouterProviders } from "./OpenRouterProviders";
import { ProviderLimit } from "./ProviderLimit";
import { ProviderMark, providerName } from "../ui/provider-mark";
import { OPENCODE_KEY_URL, missingPlans, opencodePlanOf, opencodeProvider } from "../opencode";
import { accountName, type SubscriptionAccounts, type SubscriptionUpstream } from "../accounts";
import { settingTarget } from "../settinglink";
import { useProviderTest } from "../providertest";

const DEFAULT_KINDS = ["deepseek", "openrouter", "opencode", "vllm", "llamacpp", "openai_compat"];
/** The generic protocol also serves remote vendors, so temperature is available there too. */
const TEMPERATURE_KINDS = new Set(["vllm", "llamacpp", "openai_compat"]);
const LOCAL_KINDS = new Set(["vllm", "llamacpp", "ollama"]);
const GATEWAY_KINDS = new Set(["openrouter"]);
const KEY_LINKS: Record<string, string> = {
  openrouter: "https://openrouter.ai/settings/keys",
  opencode_zen: "https://opencode.ai/auth",
  groq: "https://console.groq.com/keys",
  cerebras: "https://cloud.cerebras.ai/platform",
  gemini: "https://aistudio.google.com/apikey",
};

/**
 * Whether a client can answer: the key state the host reports, the same one Add a model shows.
 * Whether an adapter could be built said nothing about a key behind the key proxy, and read as
 * "ready" for an endpoint whose every call the proxy refused for want of one.
 */
export function providerReady(s: Pick<Settings, "provider_keys">, id: string): boolean {
  return s.provider_keys?.[id]?.ready ?? false;
}

/** How a provider is paid for and reached, in one word: a subscription (a plan or a CLI login), a
 *  local server, a gateway that routes to other vendors, or an account paid by its key. */
export function providerKind(conf: ProviderConf | undefined, s: Pick<Settings, "provider_keys">, id: string): "subscription" | "local" | "gateway" | "key" {
  const keys = s.provider_keys?.[id];
  if (conf?.billing === "subscription" || keys?.key_kind === "cli_login") return "subscription";
  if ((conf && LOCAL_KINDS.has(conf.kind)) || keys?.key_kind === "endpoint") return "local";
  if (conf && GATEWAY_KINDS.has(conf.kind)) return "gateway";
  return "key";
}

type Status = { word: string; tone: "live" | "attention" | "idle" };

/** What stands between a provider and an answer, as the key proxy and the subscription accounts say:
 *  nothing, a sign-in, a key, or a key proxy that does not answer. */
export function providerStatus(conf: ProviderConf | undefined, s: Pick<Settings, "provider_keys">, id: string, accounts?: SubscriptionUpstream): Status {
  if (!conf) return { word: t("prov.status.missing"), tone: "attention" };
  const keys = s.provider_keys?.[id];
  const signedOut = accounts && accounts.sign_in === "terminal" && accounts.accounts.length > 0 && !accounts.accounts.some((a) => a.configured);
  if (signedOut) return { word: t("prov.status.signin"), tone: "attention" };
  if (keys?.ready) return { word: t("prov.status.ready"), tone: "live" };
  if (!keys) return conf.api_key_set ? { word: t("settings.provider.keystored"), tone: "idle" } : { word: t("prov.status.unknown"), tone: "idle" };
  if (keys.key_held === null) return { word: t("prov.status.proxy"), tone: "attention" };
  if (keys.key_kind === "cli_login") return { word: t("prov.status.signin"), tone: "attention" };
  if (keys.key_kind === "endpoint") return { word: t("prov.status.down"), tone: "attention" };
  return { word: t("prov.status.nokey"), tone: "attention" };
}

/** A context window as the list prints it: 128K, 1M. */
function windowSize(n: number): string {
  if (n >= 1_000_000) return `${Math.round(n / 100_000) / 10}M`.replace(".0M", "M");
  return `${Math.round(n / 1000)}K`;
}

type Take = (next: Settings) => void;

/** "⟳ /models" button with a floating list over the card instead of inflating it. */
function ModelsMenu({ load, current, onPick }: { load: () => Promise<string[] | null>; current: string; onPick: (m: string) => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [models, setModels] = useState<string[] | null>(null);
  const [filter, setFilter] = useState("");
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!(e.target as HTMLElement).closest?.(".models-menu-wrap")) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  async function toggle() {
    if (open) return setOpen(false);
    setOpen(true);
    setBusy(true);
    setModels(await load());
    setBusy(false);
  }
  const shown = (models ?? []).filter((m) => m.toLowerCase().includes(filter.toLowerCase()));
  return (
    <span className="models-menu-wrap">
      <button className="btn small" onClick={toggle} disabled={busy}>
        {busy ? "…" : "⟳ /models"}
      </button>
      {open && (
        <div className="models-menu">
          <input className="field" autoFocus placeholder={t("settings.models.menu")} value={filter} onChange={(e) => setFilter(e.target.value)} />
          <div className="models-menu-list">
            {busy && <div className="sub" style={{ padding: 8 }}>{t("settings.models.menu.loading")}</div>}
            {!busy && models === null && <div className="sub" style={{ padding: 8 }}>{t("settings.models.menu.unreachable")}</div>}
            {!busy && models !== null && shown.length === 0 && <div className="sub" style={{ padding: 8 }}>{t("settings.models.menu.none")}</div>}
            {shown.map((m) => (
              <button key={m} className={`models-menu-item ${m === current ? "on" : ""}`} onClick={() => (setOpen(false), onPick(m))}>
                {m}
              </button>
            ))}
          </div>
        </div>
      )}
    </span>
  );
}

/** Where a preset's window came from, what every source says, and the way back to automatic. */
function WindowSource({ source, sources, onAuto, onRefresh }: { source: ContextWindowSource; sources?: ContextWindowSources; onAuto: () => void; onRefresh: () => void }) {
  const said = (n: number | null | undefined) => (n ? tokens(n) : t("window.unknown"));
  const detail = sources ? t("window.sources", { provider: said(sources.provider), catalog: said(sources.catalog), table: said(sources.table), error: said(sources.error) }) : undefined;
  const text = source === "manual" ? t("window.manual") : source === "default" ? t("window.default") : t("window.found", { source: t(`window.by.${source}`) });
  return (
    <span className="sub faint window-source" title={detail} data-testid="window-source">
      {text}{" "}
      {source === "manual"
        ? <button type="button" className="linkbtn" onClick={(e) => { e.preventDefault(); onAuto(); }}>{t("window.auto")}</button>
        : <button type="button" className="linkbtn" onClick={(e) => { e.preventDefault(); onRefresh(); }}>{t("window.refresh")}</button>}
    </span>
  );
}

/** Which OpenRouter providers a model is limited to, and the table to change it, read when opened. */
function PresetRoutes({ p, onPatch }: { p: Preset; onPatch: (patch: Partial<Preset>) => void }) {
  const [open, setOpen] = useState(false);
  const [listing, setListing] = useState<RouteListing | null>(null);
  const [failed, setFailed] = useState(false);
  const saved = p.openrouter_providers ?? [];
  const [chosen, setChosen] = useState(saved);
  const gone = p.openrouter_providers_gone ?? [];
  useEffect(() => setChosen(p.openrouter_providers ?? []), [p.openrouter_providers]);
  useEffect(() => {
    if (!open) return;
    let current = true;
    setListing(null);
    setFailed(false);
    api.get<RouteListing>(`/api/providers/${encodeURIComponent(p.provider)}/model-providers?model=${encodeURIComponent(p.model)}`)
      .then((found) => { if (!current) return; if (found.rows.length) setListing(found); else setFailed(true); })
      .catch(() => { if (current) setFailed(true); });
    return () => { current = false; };
  }, [open, p.provider, p.model]);
  const changed = chosen.join("\n") !== saved.join("\n");
  return (
    <>
      <Row title={t("settings.preset.routes")} desc={saved.length ? t("settings.preset.routes.only", { slugs: saved.join(", ") }) : t("settings.preset.routes.auto")}>
        <button className="btn small" aria-expanded={open} onClick={() => setOpen((o) => !o)}>{t(open ? "common.close" : "settings.preset.routes.choose")}</button>
      </Row>
      {gone.length > 0 && <p className="result-warning" role="alert">{t("settings.preset.routes.gone", { slugs: gone.join(", ") })}</p>}
      {open && !listing && !failed && <div className="sub faint">{t("settings.preset.routes.asking")}</div>}
      {open && failed && <p className="sub">{t("settings.preset.routes.unavailable")}</p>}
      {open && listing && <OpenRouterProviders listing={listing} chosen={chosen} saved={saved} onChange={setChosen} />}
      {open && (listing ? changed : failed && saved.length > 0) && (
        <div className="btnrow">
          {listing ? <button className="btn small primary" onClick={() => onPatch({ openrouter_providers: chosen })}>{t("common.save")}</button>
            : <button className="btn small" onClick={() => onPatch({ openrouter_providers: [] })}>{t("settings.preset.routes.clear")}</button>}
        </div>
      )}
    </>
  );
}

type ModelProps = {
  id: string;
  p: Preset;
  s: Settings;
  /** The subscription behind this model's client, when it has more than one account to choose from. */
  accounts?: SubscriptionUpstream;
  take: Take;
  toast: (text: string) => void;
  providers: string[];
  onPatch: (patch: Partial<Preset>) => void;
  onDefault: () => void;
  onDelete: () => void;
  onLookup: (provider: string) => Promise<string[] | null>;
  onRefreshWindow: () => void;
};

/** What a model can do, in the words the list prints after its name. */
function capabilities(p: Preset): string {
  const words = [t(p.thinking ? "model.caps.thinking" : "model.caps.nothink")];
  if (p.images) words.push(t("model.caps.images"));
  return words.join(" · ");
}

/** One model: a line to scan (name, what it can do, its window, the star), and its fields when opened. */
function ModelRow(props: ModelProps) {
  const { id, p, s, onPatch } = props;
  // Opened when a link to one of its rows brought the operator here (a vision refusal names the
  // session model's Images switch): those rows are inside the fold.
  const [open, setOpen] = useState(() => (settingTarget() ?? "").startsWith(`presets.${id}.`));
  const title = p.label || `${p.provider}/${p.model}`;
  const isDefault = s.model.preset === id;
  const inChain = !isDefault && (s.model.chain ?? []).includes(id);
  const favourite = !!p.favourite;
  return (
    <div className={`pmodel ${open ? "open" : ""}`} data-preset={id}>
      <div className="pmodel-row">
        <button type="button" className="pmodel-open" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
          <span className="pmodel-name">{title}</span>
          {isDefault && <span className="pmodel-tag accent">{t("settings.preset.default")}</span>}
          {inChain && <span className="pmodel-tag">{t("settings.preset.fallback")}</span>}
          {(p.openrouter_providers_gone?.length ?? 0) > 0 && <span className="pmodel-tag failed">{t("settings.preset.routes.gone.pill")}</span>}
          {!onDemandGroups(p, s.on_demand_defaults?.[id]) && <span className="pmodel-tag" title={t("settings.preset.ondemand.title")}>{t("settings.preset.ondemand.offpill")}</span>}
          <span className="pmodel-caps">{capabilities(p)}</span>
          <span className="pmodel-context">{windowSize(p.context_window)}</span>
        </button>
        <button type="button" className={`iconbtn small pmodel-star ${favourite ? "on" : ""}`} role="switch" aria-checked={favourite} aria-label={t("model.star", { name: title })} title={t("model.star", { name: title })} onClick={() => onPatch({ favourite: !favourite })}>
          <Icon name="star" size={16} />
        </button>
      </div>
      {open && <ModelEditor {...props} title={title} isDefault={isDefault} />}
    </div>
  );
}

/** A model's own fields: what the row's line cannot hold. */
function ModelEditor({ id, p, s, accounts, take, toast, providers, onPatch, onDefault, onDelete, onLookup, onRefreshWindow, title, isDefault }: ModelProps & { title: string; isDefault: boolean }) {
  const [label, setLabel] = useState(p.label);
  const [model, setModel] = useState(p.model);
  useEffect(() => setLabel(p.label), [p.label]);
  useEffect(() => setModel(p.model), [p.model]);
  const routed = s.providers?.[p.provider]?.kind === "openrouter";
  const onDemandByModel = s.on_demand_defaults?.[id];
  const price: PriceView | undefined = s.prices?.[id];
  return (
    <div className="mpanel pmodel-editor">
      <div className="mfields">
        <label className="mfield">
          <span>{t("settings.preset.label")}</span>
          <input className="field" value={label} placeholder={`${p.provider}/${p.model}`} onChange={(e) => setLabel(e.target.value)} onBlur={() => label.trim() !== p.label && onPatch({ label: label.trim() })} />
        </label>
        <div className="mfield">
          <span>{t("settings.preset.client")}</span>
          <Select
            id={`preset-provider-${id}`}
            label={t("settings.preset.client")}
            value={p.provider}
            onChange={(provider) => onPatch({ provider })}
            options={[...providers, ...(providers.includes(p.provider) ? [] : [p.provider])].map((x) => ({ id: x, label: providerName(x, s.providers?.[x]), hint: x }))}
          />
        </div>
        <label className="mfield wide">
          <span>{t("settings.preset.model")}</span>
          <div className="row" style={{ gap: 8 }}>
            <input className="field" style={{ flex: 1, minWidth: 0 }} value={model} placeholder={t("settings.preset.model.placeholder")} onChange={(e) => setModel(e.target.value)} onBlur={() => model.trim() && model.trim() !== p.model && onPatch({ model: model.trim() })} />
            <ModelsMenu load={() => onLookup(p.provider)} current={p.model} onPick={(m) => onPatch({ model: m })} />
          </div>
        </label>
        <label className="mfield">
          <span>{t("settings.preset.window")}</span>
          {/* Keyed by the number so a window the host found replaces what the uncontrolled field shows. */}
          <input key={p.context_window} className="field" type="number" min={8000} step={1000} defaultValue={p.context_window} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null && v !== p.context_window) onPatch({ context_window: v }); }} />
          <WindowSource source={p.context_window_source ?? "default"} sources={s.context_window_sources?.[id]} onAuto={() => onPatch({ context_window_source: "auto" } as unknown as Partial<Preset>)} onRefresh={onRefreshWindow} />
        </label>
        <label className="mfield">
          <span>{t("settings.preset.output")}</span>
          <input className="field" type="number" min={1024} step={1000} defaultValue={p.max_output_tokens} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null && v !== p.max_output_tokens) onPatch({ max_output_tokens: v }); }} />
        </label>
      </div>
      {/* The model's switches as rows like every other setting: "thinking on" and "images on"
          were buttons whose label was their own state, and nobody could tell the state from
          the action. */}
      <div className="mpanel-rows">
        <Row title={t("settings.preset.thinking")}>
          <Switch checked={p.thinking} onChange={(thinking) => onPatch({ thinking })} label={t("settings.preset.thinking")} />
        </Row>
        <Row title={t("settings.preset.effort")} desc={p.thinking ? undefined : t("settings.preset.effort.off")}>
          <div className="segmented inline" role="group" aria-label={t("settings.preset.effort")}>
            {REASONING_EFFORTS.map((e) => (
              <button key={e} className={p.reasoning_effort === e ? "on" : ""} disabled={!p.thinking} onClick={() => onPatch({ reasoning_effort: e })}>{t(`add.effort.${e}`)}</button>
            ))}
          </div>
        </Row>
        <Row title={t("settings.preset.imagestoggle")} desc={t("settings.preset.images.title")} data-setting={`presets.${id}.images`}>
          <Switch checked={p.images} onChange={(images) => onPatch({ images })} label={t("settings.preset.imagestoggle")} />
        </Row>
        <Row title={t("settings.preset.ondemand")} desc={t("settings.preset.ondemand.title")} stack>
          <span className="segmented inline" role="group" aria-label={t("settings.preset.ondemand")}>
            {ON_DEMAND_CHOICES.map((choice) => (
              <button
                key={String(choice)}
                className={(p.on_demand_tool_groups ?? null) === choice ? "on" : ""}
                title={choice === null ? t("settings.preset.ondemand.bymodel.title", { state: t(onDemandByModel ?? true ? "common.on" : "common.off") }) : undefined}
                onClick={() => onPatch({ on_demand_tool_groups: choice })}
              >
                {/* The model's own value goes in the title: written into the label it made the
                    row read "as the model: on · on · off", a second "on" that looked like a typo. */}
                {choice === null ? t("settings.preset.ondemand.bymodel") : t(choice ? "common.on" : "common.off")}
              </button>
            ))}
          </span>
        </Row>
        {routed && <PresetRoutes p={p} onPatch={onPatch} />}
        {accounts && accounts.accounts.length > 1 && (
          <Row title={t("settings.preset.account")} desc={t("settings.preset.account.sub")} stack data-setting={`presets.${id}.account`}>
            <Select
              id={`preset-account-${id}`}
              label={t("settings.preset.account")}
              value={accounts.accounts.some((a) => a.id === p.account) ? (p.account as string) : ""}
              onChange={(account) => onPatch({ account })}
              options={[
                { id: "", label: t("settings.preset.account.default", { name: accountName(accounts.accounts.find((a) => a.id === accounts.default_account) ?? { id: accounts.default_account, label: "" }) }) },
                ...accounts.accounts.map((a) => ({ id: a.id, label: accountName(a) })),
              ]}
            />
          </Row>
        )}
      </div>
      <ModelPrice id={id} view={price} onSaved={take} toast={toast} />
      <div className="btnrow mrow-foot">
        <span className="sub mono">{id}</span>
        {isDefault ? <span className="sub">{t("settings.preset.opens")}</span> : <button className="btn small primary" onClick={onDefault}>{t("settings.preset.makedefault")}</button>}
        <span className="grow" />
        <button className="btn small danger" disabled={isDefault} title={t(isDefault ? "settings.preset.cannotdelete" : "settings.preset.remove.title")} onClick={async () => { if (await confirmAsync(t("settings.preset.delete.title", { title }), { body: t("settings.preset.delete.body"), action: t("settings.preset.delete.action") })) onDelete(); }}>
          <Icon name="trash" size={14} /> {t("common.delete")}
        </button>
      </div>
    </div>
  );
}

type Patch = (id: string, patch: Record<string, unknown>) => Promise<Settings | undefined>;

/** A provider's own fields: its name, protocol, address, temperature, key and spend cap. */
function ProviderConnection({ id, p, kinds, viaProxy, onPatch }: { id: string; p: ProviderConf; kinds: string[]; viaProxy: boolean; onPatch: Patch }) {
  const [name, setName] = useState(p.name ?? "");
  const [baseUrl, setBaseUrl] = useState(p.base_url);
  const [keyDraft, setKeyDraft] = useState("");
  const plan = opencodePlanOf(p.kind, p.billing);
  const keyLink = KEY_LINKS[id] ?? (plan ? OPENCODE_KEY_URL : undefined);
  useEffect(() => setBaseUrl(p.base_url), [p.base_url]);
  useEffect(() => setName(p.name ?? ""), [p.name]);
  useEffect(() => setKeyDraft(""), [p.api_key_set]);
  return (
    <div className="mpanel prov-connection">
      {plan && <p className="sub opencode-hint">{t(plan.hint)}</p>}
      <div className="mfields">
        <label className="mfield">
          <span>{t("settings.provider.name")}</span>
          <input className="field" value={name} placeholder={id} onChange={(e) => setName(e.target.value)} onBlur={() => name.trim() !== (p.name ?? "") && onPatch(id, { name: name.trim() })} />
        </label>
        <div className="mfield">
          <span>{t("settings.provider.kind")}</span>
          <Select id={`provider-kind-${id}`} label={t("settings.provider.kind")} value={p.kind} onChange={(kind) => onPatch(id, { kind })} options={[...kinds, ...(kinds.includes(p.kind) ? [] : [p.kind])].map((k) => ({ id: k, label: k }))} />
        </div>
        <label className="mfield wide">
          <span>{t("settings.provider.baseurl")}</span>
          <input
            className="field"
            value={baseUrl}
            placeholder="http://<host>:<port>/v1"
            onChange={(e) => setBaseUrl(e.target.value)}
            onBlur={() => baseUrl.trim() !== p.base_url && baseUrl.trim() && onPatch(id, { base_url: baseUrl.trim() })}
          />
        </label>
        {TEMPERATURE_KINDS.has(p.kind) && (
          <label className="mfield">
            <span>{t("settings.provider.temperature")}</span>
            <input
              className="field num"
              type="number"
              min={0}
              max={2}
              step={0.1}
              defaultValue={p.temperature ?? ""}
              placeholder={t("settings.provider.temperature.default")}
              title={t("settings.provider.temperature.hint")}
              onBlur={(e) => {
                const raw = e.target.value.trim();
                if (!raw) {
                  if (p.temperature != null) onPatch(id, { temperature: null });
                  return;
                }
                const v = Number(raw);
                if (!Number.isFinite(v) || v < 0 || v > 2) return;
                if (v !== p.temperature) onPatch(id, { temperature: v });
              }}
            />
          </label>
        )}
        <label className="mfield wide">
          <span>{t(p.api_key_set ? "settings.provider.apikey.stored" : viaProxy ? "settings.provider.apikey.proxy" : "settings.provider.apikey")}</span>
          <div className="row" style={{ gap: 8 }}>
            <input
              className="field"
              style={{ flex: 1, minWidth: 0 }}
              type="password"
              autoComplete="new-password"
              placeholder={t(p.api_key_set ? "settings.provider.apikey.placeholder" : "settings.provider.apikey.none")}
              value={keyDraft}
              onChange={(e) => setKeyDraft(e.target.value)}
              onBlur={() => {
                const v = keyDraft.trim();
                if (v) {
                  onPatch(id, { api_key: v });
                  setKeyDraft("");
                }
              }}
            />
            {p.api_key_set && (
              <button className="btn small danger" title={t("settings.provider.forget.title")} onClick={() => onPatch(id, { api_key: "" })}>
                {t("settings.provider.forget")}
              </button>
            )}
          </div>
          {keyLink && <span className="sub">{t("free.key.get")} <a href={keyLink} target="_blank" rel="noopener noreferrer">{p.name || id} ↗</a></span>}
        </label>
      </div>
      <ProviderLimit providerId={id} />
    </div>
  );
}

type ProviderProps = {
  id: string;
  conf?: ProviderConf;
  s: Settings;
  presets: [string, Preset][];
  kinds: string[];
  accounts?: SubscriptionAccounts | null;
  open: boolean;
  onToggle: () => void;
  take: Take;
  toast: (text: string) => void;
  onPatchProvider: Patch;
  onPatchPreset: (id: string, patch: Partial<Preset>) => void;
  onRemove: () => void;
  onRemovePreset: (id: string) => void;
  onRefreshWindow: (id: string) => void;
  onLookup: (provider: string) => Promise<string[] | null>;
  onDefault: (id: string) => void;
};

/** One provider: its mark, name, kind, how many models and whether it can answer, on one 56 px line;
 *  opened, its models, the connection test and its own fields. */
function ProviderRow({ id, conf, s, presets, kinds, accounts, open, onToggle, take, toast, onPatchProvider, onPatchPreset, onRemove, onRemovePreset, onRefreshWindow, onLookup, onDefault }: ProviderProps) {
  const upstream = accounts?.upstreams.find((u) => u.providers.includes(id));
  const name = providerName(id, conf);
  const status = providerStatus(conf, s, id, upstream);
  const kind = conf ? providerKind(conf, s, id) : null;
  // Opened at its key when a link to the key brought the operator here: the field is behind Connection.
  const [editing, setEditing] = useState(() => settingTarget() === `providers.${id}.api_key`);
  const { test, run: runTest, note } = useProviderTest(id);
  const providerIds = Object.keys(s.providers ?? {});
  return (
    <div className={`prov ${open ? "open" : ""}`} data-provider={id} data-setting={`providers.${id}.api_key`}>
      <button type="button" className="prov-row" aria-expanded={open} title={t("prov.open", { name })} onClick={onToggle}>
        <ProviderMark id={id} kind={conf?.kind} name={name} />
        <span className="prov-name">{name}</span>
        <span className="prov-kind">{kind ? t(`prov.kind.${kind}`) : ""}</span>
        <span className="prov-count">{plural("prov.count", presets.length)}</span>
        <span className="prov-status" data-tone={status.tone}><span className="prov-dot" aria-hidden />{status.word}</span>
        <span className="prov-chev" aria-hidden><Icon name="forward" size={14} /></span>
      </button>
      {open && (
        <div className="prov-body">
          {presets.length > 0 ? (
            <>
              <div className="pmodel-head" aria-hidden>
                <span className="pmodel-head-name">{t("prov.head.model")}</span>
                <span className="pmodel-head-context">{t("prov.head.context")}</span>
                <span className="pmodel-head-star">{t("prov.head.star")}</span>
              </div>
              {presets.map(([pid, p]) => (
                <ModelRow
                  key={pid}
                  id={pid}
                  p={p}
                  s={s}
                  accounts={upstream}
                  take={take}
                  toast={toast}
                  providers={providerIds}
                  onPatch={(patch) => onPatchPreset(pid, patch)}
                  onDefault={() => onDefault(pid)}
                  onDelete={() => onRemovePreset(pid)}
                  onLookup={onLookup}
                  onRefreshWindow={() => onRefreshWindow(pid)}
                />
              ))}
            </>
          ) : (
            <p className="sub prov-empty">{t("prov.nomodels")}</p>
          )}
          {conf && (
            <div className="prov-foot">
              <button type="button" className="btn small" disabled={test?.state === "running"} onClick={() => void runTest()}>
                <Icon name="loop" size={14} /> {t("prov.test")}
              </button>
              <span className="prov-note" role="status" data-tone={note?.tone} data-test={test?.state === "done" ? (test.ok ? "ok" : "failed") : test?.state}>{note?.text ?? ""}</span>
              <button type="button" className="btn small" aria-expanded={editing} onClick={() => setEditing((e) => !e)}>
                <Icon name="settings" size={14} /> {t("prov.connection")}
              </button>
              <button type="button" className="btn small danger" onClick={async () => { if (await confirmAsync(t("settings.provider.remove.title", { id }), { body: t("settings.provider.remove.body"), action: t("settings.provider.remove.action") })) onRemove(); }}>
                <Icon name="trash" size={14} /> {t("common.delete")}
              </button>
            </div>
          )}
          {conf && editing && <ProviderConnection id={id} p={conf} kinds={kinds} viaProxy={s.provider_keys?.[id]?.via_proxy ?? false} onPatch={onPatchProvider} />}
        </div>
      )}
    </div>
  );
}

/** Adding an endpoint: an id, its protocol and its address, or one of the OpenCode plans not yet added,
 *  each one click away with its route already wired. */
export function AddProvider({ s, onAdd, onDone, toast }: { s: Settings; onAdd: Patch; onDone: () => void; toast: (text: string) => void }) {
  const kinds = s.provider_kinds ?? DEFAULT_KINDS;
  const [id, setId] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [kind, setKind] = useState("vllm");
  const providers = s.providers ?? {};
  const absent = missingPlans(Object.values(providers)).filter((plan) => !providers[plan.id]);
  async function add() {
    const pid = id.trim();
    const base = baseUrl.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(pid)) {
      toast(t("settings.provider.id.bad"));
      return;
    }
    if (!base.startsWith("http")) {
      toast(t("settings.provider.url.bad"));
      return;
    }
    if (await onAdd(pid, { kind, base_url: base })) onDone();
  }
  return (
    <div className="add-provider">
      <div className="mfields">
        <label className="mfield">
          <span>{t("settings.provider.id")}</span>
          <input className="field" value={id} placeholder="my-provider" onChange={(e) => setId(e.target.value)} />
        </label>
        <div className="mfield">
          <span>{t("settings.provider.kind")}</span>
          <Select id="add-provider-kind" label={t("settings.provider.kind")} value={kind} onChange={setKind} options={kinds.map((k) => ({ id: k, label: k }))} />
        </div>
        <label className="mfield wide">
          <span>{t("settings.provider.baseurl.short")}</span>
          <input className="field" value={baseUrl} placeholder="http://<host>:<port>/v1" onChange={(e) => setBaseUrl(e.target.value)} />
        </label>
      </div>
      <div className="btnrow">
        <button className="btn primary" onClick={() => void add()}>{t("common.add")}</button>
        <button className="btn" onClick={onDone}>{t("common.cancel")}</button>
      </div>
      {absent.length > 0 && (
        <div className="opencode-add">
          <span className="sub">{t("settings.provider.opencode")}</span>
          {absent.map((plan) => (
            // The hint opens with the plan's name, so it is the whole label: the name twice read as a stutter.
            <button key={plan.id} className="btn small" onClick={async () => { if (await onAdd(plan.id, opencodeProvider(plan, s.keyproxy_base ?? ""))) onDone(); }}>
              ＋ {t(plan.hint)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** The three defaults a new session opens with: its model, that model's reasoning, and the model that
 *  compacts its history. Reasoning is a setting of each model, so this row writes the default model's. */
function DefaultsCard({ s, save, onPatchPreset }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void>; onPatchPreset: (id: string, patch: Partial<Preset>) => void }) {
  const presets = Object.entries(s.presets ?? {});
  const options = presets.map(([id, p]) => ({ id, label: `${p.label || p.model} · ${providerName(p.provider, s.providers?.[p.provider])}` }));
  const main = s.presets?.[s.model.preset];
  const effort = main ? (main.thinking ? main.reasoning_effort || "medium" : "off") : "";
  return (
    <>
      <div className="set-label">{t("models.defaults")}</div>
      <div className="set-card models-defaults">
        <div className="set-row" data-setting="model.preset">
          <span className="set-row-title">{t("models.defaults.model")}</span>
          {presets.length ? (
            <Select id="default-model" className="set-value" label={t("models.defaults.model")} value={s.model.preset} onChange={(preset) => void save({ model: { preset } as Settings["model"] })} options={options.some((o) => o.id === s.model.preset) ? options : [{ id: s.model.preset, label: s.model.preset }, ...options]} />
          ) : (
            <span className="sub">{t("settings.models.empty")}</span>
          )}
        </div>
        {main && (
          <div className="set-row">
            <span className="set-row-title">{t("models.defaults.effort")}</span>
            <Select
              id="default-effort"
              className="set-value"
              label={t("models.defaults.effort")}
              value={effort}
              onChange={(next) => onPatchPreset(s.model.preset, next === "off" ? { thinking: false } : { thinking: true, reasoning_effort: next })}
              options={[{ id: "off", label: t("add.effort.off") }, ...REASONING_EFFORTS.map((e) => ({ id: e, label: t(`add.effort.${e}`) }))]}
            />
          </div>
        )}
        <div className="set-row">
          <span className="set-row-title">{t("models.defaults.compaction")}</span>
          <Select id="default-compaction" className="set-value" label={t("models.defaults.compaction")} value={s.compaction?.preset ?? ""} onChange={(preset) => void save({ compaction: { ...s.compaction, preset } })} options={[{ id: "", label: t("settings.compaction.model.own") }, ...options]} />
        </div>
      </div>
    </>
  );
}

/** What happens when a model fails: the switch that allows a stand-in, and the models tried, in their
 *  order, after the default. The order is a list of models, not of providers: a provider holds models
 *  of different strengths, and the chain says which of them stands in. */
function FallbackCard({ s, save }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void> }) {
  const [editing, setEditing] = useState(() => settingTarget() === "model.chain");
  const chain = [s.model.preset, ...(s.model.chain ?? []).filter((id) => id !== s.model.preset)].filter((id) => s.presets?.[id]);
  const stuck = chain.filter((id) => s.model.auto_fallback && !providerReady(s, s.presets[id].provider));
  const many = Object.keys(s.presets ?? {}).length > 1;
  return (
    <div className="set-card fallback-card">
      <div className="fallback-head" data-setting="model.auto_fallback">
        <span className="fallback-title">{t("fallback.title")}</span>
        <Switch checked={s.model.auto_fallback} onChange={(auto_fallback) => void save({ model: { auto_fallback } as Settings["model"] })} label={t("fallback.switch")} />
      </div>
      <p className="fallback-sub">{t("fallback.sub")}</p>
      <div className="fallback-chain" data-setting="model.chain">
        <ol className="fallback-steps" aria-label={t("settings.models.chain.chosen")}>
          {chain.map((id, i) => (
            <li key={id} className="fallback-step" data-tone={stuck.includes(id) ? "attention" : undefined}>
              {i > 0 && <Icon name="forward" size={12} />}
              <span className="fallback-chip"><span className="fallback-n">{i + 1}</span>{s.presets[id].label || s.presets[id].model}</span>
            </li>
          ))}
        </ol>
        <span className="fallback-warn">{stuck.length ? t("fallback.skipped", { names: stuck.map((id) => s.presets[id].label || id).join(", ") }) : ""}</span>
        {many && <button type="button" className="btn small" aria-expanded={editing} onClick={() => setEditing((e) => !e)}>{t(editing ? "fallback.done" : "fallback.edit")}</button>}
      </div>
      {editing && many && (
        <SelectionList
          id="model-chain"
          label={t("settings.models.chain.title")}
          ordered
          chosenLabel={t("settings.models.chain.chosen")}
          emptyChosen={t("settings.models.chain.empty")}
          {...modelItems(s, (id) => id !== s.model.preset)}
          value={(s.model.chain ?? []).filter((id) => id !== s.model.preset)}
          onChange={(next) => void save({ model: { chain: next } as Settings["model"] })}
        />
      )}
    </div>
  );
}

/** The provider rows open at first: the one of the default model, and the one a link names. */
function openedAtFirst(s: Settings): Set<string> {
  const opened = new Set<string>();
  const main = s.presets?.[s.model.preset];
  if (main) opened.add(main.provider);
  const target = settingTarget() ?? "";
  const provider = /^providers\.([^.]+)\./.exec(target)?.[1];
  if (provider) opened.add(provider);
  const preset = /^presets\.([^.]+)\./.exec(target)?.[1];
  if (preset && s.presets?.[preset]) opened.add(s.presets[preset].provider);
  return opened;
}

export function ModelsSection({ s, take, save, toast, accounts }: { s: Settings; take: Take; save: (patch: Partial<Settings>) => Promise<void>; toast: (text: string) => void; accounts?: SubscriptionAccounts | null }) {
  const [opened, setOpened] = useState<Set<string>>(() => openedAtFirst(s));
  const kinds = s.provider_kinds ?? DEFAULT_KINDS;
  // Every configured provider in its configured order, then any a model names that is not configured,
  // so a model is never left out of the list because its provider went.
  const rows = useMemo(() => {
    const ids = Object.keys(s.providers ?? {});
    for (const p of Object.values(s.presets ?? {})) if (!ids.includes(p.provider)) ids.push(p.provider);
    return ids.map((id) => ({ id, conf: s.providers?.[id], presets: Object.entries(s.presets ?? {}).filter(([, p]) => p.provider === id) }));
  }, [s.providers, s.presets]);
  const fail = (e: unknown) => { toast((e as Error).message); return undefined; };
  const patchProvider: Patch = async (id, patch) => {
    try {
      const next = await api.put<Settings>(`/api/providers/${encodeURIComponent(id)}`, patch);
      take(next);
      if ("api_key" in patch) announceProviderKeys();
      return next;
    } catch (e) {
      return fail(e);
    }
  };
  const patchPreset = (id: string, patch: Partial<Preset>) => {
    api.put<Settings>(`/api/presets/${encodeURIComponent(id)}`, patch).then(take, fail);
  };
  const removePreset = (id: string) => {
    api.delete<Settings>(`/api/presets/${encodeURIComponent(id)}`).then(take, fail);
  };
  const removeProvider = (id: string) => {
    api.delete<Settings>(`/api/providers/${encodeURIComponent(id)}`).then(take, fail);
  };
  const refreshWindow = async (id: string) => {
    try {
      await api.post(`/api/presets/${encodeURIComponent(id)}/context-window`, {});
      take(await api.get<Settings>("/api/settings"));
    } catch (e) {
      fail(e);
    }
  };
  const lookup = async (provider: string): Promise<string[] | null> => {
    try {
      return (await api.post<{ models: string[] }>("/api/providers/lookup-models", { provider })).models;
    } catch (e) {
      fail(e);
      return null;
    }
  };
  const toggle = (id: string) => setOpened((now) => {
    const next = new Set(now);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
  return (
    <div className="models-section">
      <DefaultsCard s={s} save={save} onPatchPreset={patchPreset} />
      <div className="set-label">
        <span>{t("models.providers")}</span>
        <span className="set-label-note">{t("models.providers.note")}</span>
      </div>
      <div className="set-card prov-list" data-setting="providers.list">
        {rows.map((row) => (
          <ProviderRow
            key={row.id}
            id={row.id}
            conf={row.conf}
            s={s}
            presets={row.presets}
            kinds={kinds}
            accounts={accounts}
            open={opened.has(row.id)}
            onToggle={() => toggle(row.id)}
            take={take}
            toast={toast}
            onPatchProvider={patchProvider}
            onPatchPreset={patchPreset}
            onRemove={() => removeProvider(row.id)}
            onRemovePreset={removePreset}
            onRefreshWindow={(id) => void refreshWindow(id)}
            onLookup={lookup}
            onDefault={(id) => void save({ model: { preset: id } as Settings["model"] })}
          />
        ))}
        {rows.length === 0 && (
          <div className="prov-none">
            <b>{t("settings.models.empty")}</b>
            <span className="sub">{t("settings.models.empty.sub")}</span>
          </div>
        )}
      </div>
      <FallbackCard s={s} save={save} />
    </div>
  );
}
