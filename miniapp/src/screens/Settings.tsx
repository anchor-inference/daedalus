import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Icon, IconName } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader, go, screenTitle, useMedia } from "../ui/index";
import { api, telegram, HeartbeatStatus, Preset, ProviderConf, SearchBackendInfo, SearchCheck, Settings } from "../api";
import { confirmAsync, errorText, numInput } from "../ui";
import * as passkeys from "../passkeys";
import { timeAgo } from "../ui/components";
import { shortDateTime } from "../format";
import { useMoreBelow } from "../edgefade";
import { SpeechModels } from "./Speech";
import { TtsVoices } from "./Voices";
import { VoiceSettings } from "./Voice";
import { ComponentsTab } from "./Components";
import { DependenciesTab } from "./Dependencies";
import { PromptChange } from "./PromptChange";
import { AddModel } from "./AddModel";
import { ON_DEMAND_CHOICES, REASONING_EFFORTS, onDemandGroups, orchestratorPreset } from "../models";
import { mainPreset } from "../main/model";
import { Sheet } from "../ui/dialogs";
import { plural, t } from "../i18n";
import { Dropdown, LangPicker, MultiDropdown, Segmented, Switch } from "../ui/index";
import { NumInput, NumRow, Row, TextBlock } from "../settingsrow";
import { AppearancePanel } from "./Appearance";
import { modeHome, storedMode } from "../mode";
import { useQuery } from "../store";
import { Capabilities, componentsNeedAttention } from "../capabilities";
import { NotificationSettings } from "./NotificationSettings";
import { EnvironmentsTab } from "./Environments";
import { ToolGroupsSettings } from "../toolgroupsview";
import { CompactionModelSelect } from "../compactionmodel";
import { AsrSettingsCard } from "./AsrSettings";
import { DesktopAppCard } from "../updatedialog";
import { ProviderLimit } from "./ProviderLimit";

const DEFAULT_KINDS = ["deepseek", "openrouter", "opencode", "vllm", "llamacpp", "openai_compat"];
/** The generic protocol also serves remote vendors, so temperature is available there too. */
const TEMPERATURE_KINDS = new Set(["vllm", "llamacpp", "openai_compat"]);

function RulesEditor({ rules, fallback, onSave }: { rules: string; fallback: string; onSave: (rules: string) => void }) {
  const [text, setText] = useState(rules || fallback);
  const [dirty, setDirty] = useState(false);
  const field = useRef<HTMLTextAreaElement>(null);
  useMoreBelow(field, text);
  useEffect(() => {
    setText(rules || fallback);
    setDirty(false);
  }, [rules, fallback]);
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.rules.title")}</div>
      <div className="sub">{t("settings.rules.sub")} {t(rules ? "settings.rules.custom" : "settings.rules.default")}</div>
      {/* The eleventh line shows through the field's bottom padding sliced in half; the fade over
          it (while more waits below) makes that read as "scroll for more" rather than broken text. */}
      <div className="rules-editor">
        <textarea ref={field} className="field rules-editor-field" rows={10} value={text} onChange={(e) => (setText(e.target.value), setDirty(true))} style={{ fontFamily: "var(--mono)", fontSize: 12.5 }} />
      </div>
      <div className="btnrow">
        <button className="btn primary" disabled={!dirty} onClick={() => (onSave(text.trim() === fallback.trim() ? "" : text), setDirty(false))}>
          {t("common.save")}
        </button>
        <button className="btn" onClick={() => (setText(fallback), setDirty(true))}>
          {t("settings.rules.reset")}
        </button>
      </div>
    </div>
  );
}

/** A preset as the model picker names it: its label, or provider/model when it has none. */
function presetName(s: Settings, id: string): string {
  const p = s.presets?.[id];
  return p ? p.label || `${p.provider}/${p.model}` : id;
}

type Patch = (id: string, patch: Record<string, unknown>) => Promise<Settings | undefined>;

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

/** One model: a compact line to scan, an expanded panel to edit. */
function PresetRow({ id, p, onDemandByModel, isDefault, inChain, providers, onDefault, onChain, onPatch, onDelete, onLookup }: {
  id: string;
  p: Preset;
  onDemandByModel?: boolean;
  isDefault: boolean;
  inChain: boolean;
  providers: string[];
  onDefault: () => void;
  onChain: () => void;
  onPatch: (patch: Partial<Preset>) => void;
  onDelete: () => void;
  onLookup: (provider: string) => Promise<string[] | null>;
}) {
  const [label, setLabel] = useState(p.label);
  const [model, setModel] = useState(p.model);
  const [open, setOpen] = useState(false);
  useEffect(() => setLabel(p.label), [p.label]);
  useEffect(() => setModel(p.model), [p.model]);
  const title = p.label || `${p.provider}/${p.model}`;
  return (
    <div className={`mrow ${isDefault ? "default" : ""} ${open ? "open" : ""}`}>
      <div className="mline noradio">
        <button className="mmain" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span className="mtitle">{title}</span>
          <span className="mmeta">{p.provider} · {p.model}</span>
        </button>
        <div className="mtags">
          {isDefault && <span className="chip accent">{t("settings.preset.default")}</span>}
          {!isDefault && inChain && <span className="pill">{t("settings.preset.fallback")}</span>}
          <span className="pill">{p.thinking ? t("settings.preset.think", { effort: t(`add.effort.${p.reasoning_effort}`) }) : t("settings.preset.nothink")}</span>
          {p.images && <span className="pill">{t("settings.preset.images")}</span>}
          {!onDemandGroups(p, onDemandByModel) && <span className="pill" title={t("settings.preset.ondemand.title")}>{t("settings.preset.ondemand.offpill")}</span>}
          <span className="pill">{Math.round(p.context_window / 1000)}k</span>
        </div>
        <div className="mactions">
          <button className={`iconbtn small ${open ? "on" : ""}`} onClick={() => setOpen((o) => !o)} title={t(open ? "common.close" : "common.edit")} aria-label={t(open ? "common.close" : "common.edit")}><span className={`chev ${open ? "down" : ""}`}>›</span></button>
        </div>
      </div>
      {open && (
        <div className="mpanel">
          <div className="mfields">
            <label className="mfield">
              <span>{t("settings.preset.label")}</span>
              <input className="field" value={label} placeholder={`${p.provider}/${p.model}`} onChange={(e) => setLabel(e.target.value)} onBlur={() => label.trim() !== p.label && onPatch({ label: label.trim() })} />
            </label>
            <label className="mfield">
              <span>{t("settings.preset.client")}</span>
              <select className="field" value={p.provider} onChange={(e) => onPatch({ provider: e.target.value })}>
                {providers.map((x) => (
                  <option key={x}>{x}</option>
                ))}
                {!providers.includes(p.provider) && <option>{p.provider}</option>}
              </select>
            </label>
            <label className="mfield wide">
              <span>{t("settings.preset.model")}</span>
              <div className="row" style={{ gap: 8 }}>
                <input className="field" style={{ flex: 1, minWidth: 0 }} value={model} placeholder={t("settings.preset.model.placeholder")} onChange={(e) => setModel(e.target.value)} onBlur={() => model.trim() && model.trim() !== p.model && onPatch({ model: model.trim() })} />
                <ModelsMenu load={() => onLookup(p.provider)} current={p.model} onPick={(m) => onPatch({ model: m })} />
              </div>
            </label>
            <label className="mfield">
              <span>{t("settings.preset.window")}</span>
              <input className="field" type="number" min={8000} step={1000} defaultValue={p.context_window} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null && v !== p.context_window) onPatch({ context_window: v }); }} />
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
            <Row title={t("settings.preset.imagestoggle")} desc={t("settings.preset.images.title")}>
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
            {!isDefault && (
              <Row title={t("settings.preset.usefallback")} desc={t("settings.preset.fallback.title")}>
                <Switch checked={inChain} onChange={() => onChain()} label={t("settings.preset.usefallback")} />
              </Row>
            )}
          </div>
          <div className="btnrow mrow-foot">
            <span className="sub mono">{id}</span>
            {isDefault ? <span className="sub">{t("settings.preset.opens")}</span> : <button className="btn small primary" onClick={onDefault}>{t("settings.preset.makedefault")}</button>}
            <span className="grow" />
            <button className="btn small danger" disabled={isDefault} title={t(isDefault ? "settings.preset.cannotdelete" : "settings.preset.remove.title")} onClick={async () => { if (await confirmAsync(t("settings.preset.delete.title", { title }), { body: t("settings.preset.delete.body"), action: t("settings.preset.delete.action") })) onDelete(); }}>
              <Icon name="trash" size={14} /> {t("common.delete")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** One client: id, kind and readiness on a line; the address and the key behind it. */
function ProviderBlock({ id, p, kinds, available, onPatch, onRemove }: {
  id: string;
  p: ProviderConf;
  kinds: string[];
  available: boolean;
  onPatch: Patch;
  onRemove: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(p.name ?? "");
  const [baseUrl, setBaseUrl] = useState(p.base_url);
  const [keyDraft, setKeyDraft] = useState("");
  useEffect(() => setBaseUrl(p.base_url), [p.base_url]);
  useEffect(() => setName(p.name ?? ""), [p.name]);
  useEffect(() => setKeyDraft(""), [p.api_key_set]);
  return (
    <div className={`mrow ${open ? "open" : ""}`}>
      <div className="mline noradio">
        <button className="mmain" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          <span className="mtitle">{p.name || id}</span>
          <span className="mmeta">{p.kind} · {p.base_url || t("settings.provider.noaddress")}</span>
        </button>
        <div className="mtags">
          <span className={`pill ${available ? "idle" : "waiting"}`}>{t(available ? "settings.provider.ready" : "settings.provider.needs")}</span>
          {p.api_key_set && <span className="pill">{t("settings.provider.keystored")}</span>}
        </div>
        <div className="mactions">
          <button className={`iconbtn small ${open ? "on" : ""}`} onClick={() => setOpen((o) => !o)} title={t(open ? "common.close" : "common.edit")} aria-label={t(open ? "common.close" : "common.edit")}><span className={`chev ${open ? "down" : ""}`}>›</span></button>
        </div>
      </div>
      {open && (
        <div className="mpanel">
          <div className="mfields">
            <label className="mfield">
              <span>{t("settings.provider.name")}</span>
              <input className="field" value={name} placeholder={id} onChange={(e) => setName(e.target.value)} onBlur={() => name.trim() !== (p.name ?? "") && onPatch(id, { name: name.trim() })} />
            </label>
            <label className="mfield">
              <span>{t("settings.provider.kind")}</span>
              <select className="field" value={p.kind} onChange={(e) => onPatch(id, { kind: e.target.value })}>
                {kinds.map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </select>
            </label>
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
              <span>{t(p.api_key_set ? "settings.provider.apikey.stored" : "settings.provider.apikey")}</span>
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
            </label>
          </div>
          <ProviderLimit providerId={id} />
          <div className="btnrow mrow-foot">
            <span className="grow" />
            <button className="btn small danger" onClick={async () => { if (await confirmAsync(t("settings.provider.remove.title", { id }), { body: t("settings.provider.remove.body"), action: t("settings.provider.remove.action") })) onRemove(id); }}>
              <Icon name="trash" size={14} /> {t("common.remove")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function AddProviderRow({ kinds, onAdd, toast }: { kinds: string[]; onAdd: (id: string, baseUrl: string, kind: string) => void; toast: (t: string) => void }) {
  const [open, setOpen] = useState(false);
  const [id, setId] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [kind, setKind] = useState("vllm");
  function add() {
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
    onAdd(pid, base, kind);
    setId("");
    setBaseUrl("");
    setOpen(false);
  }
  if (!open)
    return (
      <button className="btn small" style={{ marginTop: 10 }} onClick={() => setOpen(true)}>
        ＋ {t("settings.provider.add")}
      </button>
    );
  return (
    <div className="mpanel add">
      <div className="mfields">
        <label className="mfield">
          <span>{t("settings.provider.id")}</span>
          <input className="field" value={id} placeholder="my-provider" onChange={(e) => setId(e.target.value)} />
        </label>
        <label className="mfield">
          <span>{t("settings.provider.kind")}</span>
          <select className="field" value={kind} onChange={(e) => setKind(e.target.value)}>
            {kinds.map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
        </label>
        <label className="mfield wide">
          <span>{t("settings.provider.baseurl.short")}</span>
          <input className="field" value={baseUrl} placeholder="http://<host>:<port>/v1" onChange={(e) => setBaseUrl(e.target.value)} />
        </label>
      </div>
      <div className="btnrow">
        <button className="btn small primary" onClick={add}>{t("common.add")}</button>
        <button className="btn small" onClick={() => setOpen(false)}>{t("common.cancel")}</button>
      </div>
    </div>
  );
}

type SpendBalance = { spent_usd: number; unmetered: number; cap_usd: number; reserved_usd: number; uncertain_usd: number; reserved_count: number; uncertain_count: number };
type SpendView = { since: string; total: SpendBalance; per_provider: Record<string, SpendBalance> };

function spendBalance(value: unknown): value is SpendBalance {
  if (!value || typeof value !== "object") return false;
  const entry = value as Record<string, unknown>;
  return ["spent_usd", "unmetered", "cap_usd", "reserved_usd", "uncertain_usd", "reserved_count", "uncertain_count"]
    .every((field) => typeof entry[field] === "number" && Number.isFinite(entry[field]) && (entry[field] as number) >= 0);
}

function usd(value: number): string {
  return `$${value.toFixed(value > 0 && value < 0.01 ? 6 : 2)}`;
}

function TotalCaps({ s, save }: { s: Settings; save: (patch: any) => Promise<void> }) {
  const [spend, setSpend] = useState<SpendView | null>(null);
  const [spendState, setSpendState] = useState<"loading" | "ready" | "failed">("loading");
  const requestSeq = useRef(0);
  const load = useCallback(() => {
    const sequence = ++requestSeq.current;
    setSpend(null);
    setSpendState("loading");
    api.get<SpendView>("/api/limits/spend").then((result) => {
      if (sequence !== requestSeq.current) return;
      if (!spendBalance(result.total) || !result.per_provider || typeof result.per_provider !== "object"
          || !Object.values(result.per_provider).every(spendBalance)) throw new Error("incomplete spend balance");
      setSpend(result);
      setSpendState("ready");
    }).catch(() => { if (sequence === requestSeq.current) { setSpend(null); setSpendState("failed"); } });
  }, []);
  useEffect(load, [load, s.limits.total_since, s.limits.usd_total, s.limits.usd_total_per_provider]);
  const providers = Object.keys(s.providers ?? {});
  const caps = s.limits.usd_total_per_provider ?? {};
  const since = s.limits.total_since ? shortDateTime(s.limits.total_since) : t("settings.limits.since.start");
  const spent = spend?.total.spent_usd ?? 0;
  const held = spend?.total.reserved_usd ?? 0;
  const engaged = spent + held;
  const cap = s.limits.usd_total;
  return (
    <>
      {spendState === "failed" && <div className="result-warning" role="status">{t("settings.limits.balance.failed")} <button type="button" className="linkbtn" onClick={load}>{t("common.retry")}</button></div>}
      {/* Only with a cap: "of $0, no cap" has no bar to draw and no share to read. */}
      {cap > 0 && spend && (
        <div className="spend-meter" data-level={engaged >= cap ? "bad" : engaged >= cap * 0.8 ? "warn" : "ok"}>
          <div className="spend-meter-line">
            <b>{held > 0 ? t("settings.limits.meter.held", { spent: usd(spent), held: usd(held), cap: usd(cap) })
              : t("settings.limits.meter", { spent: usd(spent), cap: usd(cap) })}</b>
            <span className="sub">{t("settings.limits.since", { when: since })}</span>
          </div>
          <span className="spend-meter-track" aria-hidden><span style={{ width: `${Math.min(100, (engaged / cap) * 100)}%` }} /></span>
        </div>
      )}
      {spend && (held > 0 || spend.total.unmetered > 0) && <details className="sheet-section"><summary>{t("settings.limits.balance.details")}</summary>
        {held > 0 && <p className="sub">{t("settings.limits.balance.held", { amount: usd(held), n: spend.total.reserved_count })}</p>}
        {spend.total.uncertain_usd > 0 && <p className="sub">{t("settings.limits.balance.uncertain", { amount: usd(spend.total.uncertain_usd), n: spend.total.uncertain_count })}</p>}
        {spend.total.unmetered > 0 && <p className="sub">{t("settings.limits.balance.unpriced", { n: spend.total.unmetered })}</p>}
        {held > 0 && <p className="sub">{t("settings.limits.balance.reset")}</p>}
      </details>}
      <Row title={t("settings.limits.total")} htmlFor="limits-total" desc={spend ? t("settings.limits.total.sub", { sum: usd(spent) }) : t(spendState === "loading" ? "settings.limits.balance.loading" : "settings.limits.balance.unknown")} stack>
        <NumInput id="limits-total" label={t("settings.limits.total")} value={cap} min={0} step={1} unit="USD" onSave={(v) => save({ limits: { usd_total: v } })} />
      </Row>
      {providers.map((pid) => (
        <Row key={pid} title={t("settings.limits.provider", { id: pid })} desc={spend ? spend.per_provider[pid]?.reserved_usd > 0
          ? t("settings.limits.provider.held", { spent: usd(spend.per_provider[pid].spent_usd), held: usd(spend.per_provider[pid].reserved_usd) })
          : t("settings.limits.spent", { sum: usd(spend.per_provider[pid]?.spent_usd ?? 0) })
          : t(spendState === "loading" ? "settings.limits.balance.loading" : "settings.limits.balance.unknown")} stack data-provider={pid}>
          <NumInput label={t("settings.limits.provider", { id: pid })} value={caps[pid] ?? 0} min={0} step={1} unit="USD" onSave={(v) => save({ limits: { usd_total_per_provider: { [pid]: v } } })} />
        </Row>
      ))}
      {/* Said, not left blank: an empty space where the providers go read as a list that failed to load. */}
      {providers.length === 0 && <Row title={t("settings.limits.perprovider")} desc={t("settings.limits.noproviders")} />}
      <Row title={t("settings.limits.counting")} desc={t("settings.limits.since", { when: since })}>
        <button className="btn small" onClick={async () => { await api.post("/api/limits/reset-total"); load(); }}>{t("settings.limits.reset")}</button>
      </Row>
    </>
  );
}

function SearchBlock({ s, save }: { s: Settings; save: (patch: any) => Promise<void> }) {
  const search = s.tools.web.search;
  const backends = s.search_backends ?? [];
  const [check, setCheck] = useState<SearchCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const saveSearch = (patch: any) => save({ tools: { web: { search: patch } } });
  const info = (id: string) => backends.find((b) => b.id === id);
  const usable = (b: SearchBackendInfo) => !b.needs_key || b.available !== false;
  const optionLabel = (b: SearchBackendInfo) => `${b.label}${b.needs_key ? (b.available === false ? t("settings.search.nokey") : b.available == null ? t("settings.search.noproxy") : "") : ""}`;
  const runCheck = async (backend: string) => {
    setChecking(true);
    setCheckError("");
    try {
      setCheck(await api.post<SearchCheck>("/api/settings/search-check", { backend }));
    } catch (e) {
      setCheck(null);
      setCheckError((e as Error).message);
    } finally {
      setChecking(false);
    }
  };
  const fallbackIds = search.fallback ?? [];
  return (
    <div className="card search-card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.search.title")}</div>
      <div className="sub">{t("settings.search.sub")}</div>
      <Row title={t("settings.search.backend")} stack>
        <Dropdown
          id="search-backend"
          label={t("settings.search.backend")}
          value={search.backend}
          onChange={(backend) => saveSearch({ backend })}
          options={[
            ...backends.map((b) => ({ id: b.id, label: b.label, hint: optionLabel(b).slice(b.label.length).replace(/^ — /, "") || undefined, disabled: !usable(b) })),
            ...(backends.some((b) => b.id === search.backend) ? [] : [{ id: search.backend, label: search.backend }]),
          ]}
        />
      </Row>
      <Row title={t("settings.search.fallbacks")} desc={t("settings.search.fallbacks.sub")} stack>
        <MultiDropdown
          id="search-fallbacks"
          label={t("settings.search.fallbacks")}
          none={t("settings.search.fallbacks.none")}
          values={fallbackIds.filter((id) => id !== search.backend)}
          onChange={(fallback) => saveSearch({ fallback })}
          options={backends.filter((b) => b.id !== search.backend).map((b) => ({ id: b.id, label: b.label, hint: optionLabel(b).slice(b.label.length).replace(/^ — /, "") || undefined, disabled: !fallbackIds.includes(b.id) && !usable(b) }))}
        />
      </Row>
      <NumRow title={t("settings.search.results")} value={search.results} min={1} onSave={(v) => saveSearch({ results: v })} />
      <NumRow title={t("settings.search.timeout")} unit={t("settings.unit.seconds")} value={search.timeout_seconds} min={1} onSave={(v) => saveSearch({ timeout_seconds: v })} />
      {search.backend === "searxng" || fallbackIds.includes("searxng") ? (
        <>
          <div className="section-title">SearXNG</div>
          <TextBlock label={t("settings.search.url")} value={search.searxng.url} placeholder="http://127.0.0.1:8080" onSave={(v) => saveSearch({ searxng: { url: v } })} />
          <TextBlock label={t("settings.search.engines")} value={search.searxng.engines} placeholder="google,duckduckgo,bing" onSave={(v) => saveSearch({ searxng: { engines: v } })} />
          <TextBlock label={t("settings.search.categories")} value={search.searxng.categories} placeholder="general" onSave={(v) => saveSearch({ searxng: { categories: v } })} />
          <Row title={t("settings.search.safe")}>
            <Segmented label={t("settings.search.safe")} value={String(Math.min(2, Math.max(0, search.searxng.safesearch)))} onChange={(v) => saveSearch({ searxng: { safesearch: Number(v) } })} options={["0", "1", "2"].map((id) => ({ id, label: t(`settings.search.safe.${id}`) }))} />
          </Row>
        </>
      ) : null}
      {search.backend === "duckduckgo" || fallbackIds.includes("duckduckgo") ? (
        <>
          <div className="section-title">DuckDuckGo</div>
          <TextBlock label={t("settings.search.html")} value={search.duckduckgo.url} onSave={(v) => saveSearch({ duckduckgo: { url: v } })} />
          <TextBlock label={t("settings.search.region")} value={search.duckduckgo.region} placeholder="wt-wt, ru-ru, us-en" onSave={(v) => saveSearch({ duckduckgo: { region: v } })} />
        </>
      ) : null}
      {search.backend === "serper" || fallbackIds.includes("serper") ? (
        <>
          <div className="section-title">Serper (Google)</div>
          <div className="grid2 settings-block">
            <TextBlock label={t("settings.search.country")} value={search.serper.gl} placeholder="ru, us" onSave={(v) => saveSearch({ serper: { gl: v } })} />
            <TextBlock label={t("settings.search.language")} value={search.serper.hl} placeholder="ru, en" onSave={(v) => saveSearch({ serper: { hl: v } })} />
          </div>
        </>
      ) : null}
      {search.backend === "keenable" || fallbackIds.includes("keenable") ? (
        <>
          <div className="section-title">Keenable</div>
          <NumRow title={t("settings.search.snippet")} unit={t("settings.unit.chars")} value={search.keenable.snippet_max_length} min={180} step={60} onSave={(v) => saveSearch({ keenable: { snippet_max_length: v } })} />
        </>
      ) : null}
      {search.backend === "tavily" || fallbackIds.includes("tavily") ? (
        <>
          <div className="section-title">Tavily</div>
          <Row title={t("settings.search.depth")}>
            <Dropdown label={t("settings.search.depth")} value={search.tavily.depth} onChange={(depth) => saveSearch({ tavily: { depth } })} options={["basic", "advanced", "fast", "ultra-fast"].map((d) => ({ id: d, label: d }))} />
          </Row>
        </>
      ) : null}
      {search.backend === "exa" || fallbackIds.includes("exa") ? (
        <>
          <div className="section-title">Exa</div>
          <Row title={t("settings.search.type")}>
            <Dropdown label={t("settings.search.type")} value={search.exa.type} onChange={(type) => saveSearch({ exa: { type } })} options={["auto", "instant", "fast", "deep"].map((d) => ({ id: d, label: d }))} />
          </Row>
        </>
      ) : null}
      <div className="btnrow search-check">
        <button className="btn small primary" disabled={checking} onClick={() => runCheck("")}>{t(checking ? "settings.search.checking" : "settings.search.check")}</button>
        <button className="btn small" disabled={checking || !info(search.backend)} onClick={() => runCheck(search.backend)}>{t("settings.search.checkone", { name: info(search.backend)?.label ?? search.backend })}</button>
      </div>
      {checkError && <div className="sub" style={{ color: "var(--bad)" }}>{checkError}</div>}
      {check && (
        <div className="sub" style={{ marginTop: 8 }}>
          {check.attempts.map((a) => (
            <div key={a.backend}>
              {a.error ? t("settings.search.attempt.error", { name: a.backend, error: a.error, ms: a.ms }) : t("settings.search.attempt.ok", { name: a.backend, n: a.hits, ms: a.ms })}
            </div>
          ))}
          {check.hits.slice(0, 3).map((h) => (
            <div key={h.url} style={{ marginTop: 4 }}>
              <div>{h.title}{h.source ? ` · ${h.source}` : ""}</div>
              <div style={{ wordBreak: "break-all" }}>{h.url}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Tools & search: the tool groups first — the one card here that changes what every request costs —
 *  then what each tool that reaches out may do. Speech-to-text moved to Voice & speech, and the
 *  image model to Models & providers, where the operator goes looking for a model. */
function ToolsTab({ s, save, toast, onSettings }: { s: Settings; save: (patch: any) => Promise<void>; toast: (t: string) => void; onSettings: (next: Settings) => void }) {
  const web = s.tools.web;
  return (
    <>
      <ToolGroupsSettings toast={toast} revision={s.revision} onSettings={onSettings} />

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.web.title")}</div>
        <NumRow title={t("settings.web.timeout")} unit={t("settings.unit.seconds")} value={web.fetch_timeout_seconds} min={1} onSave={(v) => save({ tools: { web: { fetch_timeout_seconds: v } } })} />
        <NumRow title={t("settings.web.maxchars")} unit={t("settings.unit.chars")} value={web.fetch_max_chars} min={1000} step={1000} onSave={(v) => save({ tools: { web: { fetch_max_chars: v } } })} />
        <TextBlock label={t("settings.web.proxy")} value={web.proxy} placeholder="socks5://127.0.0.1:1080" hint={t("settings.web.proxy.hint")} onSave={(v) => save({ tools: { web: { proxy: v } } })} />
        <TextBlock label={t("settings.web.ua")} value={web.user_agent} onSave={(v) => save({ tools: { web: { user_agent: v } } })} />
      </div>

      <SearchBlock s={s} save={save} />

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.exec.title")}</div>
        <NumRow title={t("settings.exec.timeout")} desc={t("settings.exec.timeout.hint")} unit={t("settings.unit.seconds")} value={s.limits.tool_timeout_seconds} min={10} step={30} onSave={(v) => save({ limits: { tool_timeout_seconds: v } })} />
        <NumRow title={t("settings.exec.maxchars")} desc={t("settings.exec.maxchars.hint")} unit={t("settings.unit.chars")} value={s.tools.exec.max_output_chars} min={2000} step={5000} onSave={(v) => save({ tools: { exec: { max_output_chars: v } } })} />
      </div>

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.mcp.title")}</div>
        <div className="sub">{t("settings.mcp.sub")}</div>
        <div className="sub" style={{ marginTop: 6 }}>{Object.keys((s as any).mcp?.servers ?? {}).join(", ") || t("settings.mcp.none")}</div>
        <div className="sub" style={{ marginTop: 6 }}>{t("settings.mcp.apply")}</div>
      </div>
    </>
  );
}

type Check = { name: string; ok: boolean; message: string; severity: string; fix_hint: string; fixable: boolean; fixed: boolean };

function HealthTab({ toast }: { toast: (t: string) => void }) {
  const [data, setData] = useState<{ checks: Check[]; summary: Record<string, number> } | null>(null);
  const [busy, setBusy] = useState(false);
  const load = async (fix = false) => {
    setBusy(true);
    try {
      setData(fix ? await api.post("/api/doctor/fix") : await api.get("/api/doctor"));
      if (fix) toast(t("settings.health.fixed"));
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (!data) return <div className="empty">{t(busy ? "settings.health.checking" : "common.loading")}</div>;
  // The app's own glyphs, coloured by the state, as the dependency cards draw a failure; emoji drew
  // the same states in each platform's own colours and shapes.
  const mark = (c: Check): [IconName, string] => (c.fixed ? ["wrench", "ok"] : c.ok ? ["check", "ok"] : c.severity === "fail" ? ["close", "bad"] : ["alert", "warn"]);
  // An answer without `checks` is not a screen that should go down with "Cannot read properties of
  // undefined": the doctor is one endpoint away and an installation that has not got it yet reads
  // as no checks rather than as a broken page.
  const checks = data.checks ?? [];
  const summary = data.summary ?? {};
  const fixable = checks.some((c) => !c.ok && c.fixable);
  return (
    <>
      <div className="card">
        <div className="row health-summary">
          <div className="grow">
            <b>{t("settings.health.ok", { n: summary.ok ?? 0 })}</b> · {plural("settings.health.warn", summary.warn ?? 0)} · {plural("settings.health.fail", summary.fail ?? 0)}
          </div>
          <button className="btn small" disabled={busy} onClick={() => load(false)}>
            {t("settings.health.recheck")}
          </button>
          {fixable && (
            <button className="btn small primary" disabled={busy} onClick={() => load(true)}>
              {t("settings.health.fix")}
            </button>
          )}
        </div>
      </div>
      <div className="card">
        {checks.map((c, i) => (
          <div key={i} className="row" style={{ alignItems: "flex-start", padding: "6px 0", borderTop: i ? "1px solid var(--line)" : undefined }}>
            <span className={`health-mark ${mark(c)[1]}`}><Icon name={mark(c)[0]} size={16} /></span>
            <div className="grow" style={{ minWidth: 0 }}>
              <div>
                <b>{c.name}</b> <span className="sub">{c.message}</span>
              </div>
              {!c.ok && c.fix_hint && <div className="sub">→ {c.fix_hint}</div>}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

function HeartbeatTab({ s, toast }: { s: Settings; toast: (t: string) => void }) {
  const [hb, setHb] = useState<HeartbeatStatus | null>(null);
  const [text, setText] = useState("");
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const load = () =>
    api
      .get<HeartbeatStatus>("/api/heartbeat")
      .then((r) => {
        setHb(r);
        if (!dirtyRef.current) setText(r.text);
      })
      .catch((e) => toast((e as Error).message));
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (!hb) return <div className="empty">{t("common.loading")}</div>;

  async function put(patch: Record<string, unknown>) {
    try {
      const r = await api.put<HeartbeatStatus>("/api/heartbeat", patch);
      setHb(r);
      if ("text" in patch) {
        setDirty(false);
        dirtyRef.current = false;
        setText(r.text);
      }
      toast(t("common.saved"));
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function runNow() {
    try {
      const r = await api.post<{ session_id: string }>("/api/heartbeat/run");
      toast(t("settings.heartbeat.started", { id: r.session_id }));
      load();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  const presets = Object.keys(s.presets ?? {});
  return (
    <>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.heartbeat.title")}</div>
        <div className="sub">{t("settings.heartbeat.sub")}</div>
        {/* The switch and Run now share the row, and the state is the row's description: apart,
            an "on" chip, a Run now and a status line made three rows that read as three actions. */}
        <Row
          title={t("settings.heartbeat.enabled")}
          desc={
            <>
              {hb.armed ? t("settings.heartbeat.armed") : hb.enabled ? t("settings.heartbeat.emptyfile") : t("common.off")} · {t("settings.heartbeat.today", { done: hb.runs_today, max: hb.max_runs_per_day })} ·{" "}
              {t("settings.heartbeat.last", { t: hb.last_run ? timeAgo(hb.last_run) : t("common.never") })}
              {hb.running ? t("settings.heartbeat.running") : ""}
            </>
          }
        >
          <button className="btn small" onClick={runNow} disabled={!text.trim() || hb.running}>
            {t("common.runnow")}
          </button>
          <Switch checked={hb.enabled} onChange={(enabled) => put({ enabled })} label={t("settings.heartbeat.enabled")} />
        </Row>
        <NumRow id="heartbeat-interval" title={t("settings.heartbeat.interval")} unit={t("settings.unit.minutes")} value={hb.interval_minutes} min={1} onSave={(v) => put({ interval_minutes: v })} />
        <Row title={t("settings.heartbeat.hours")} desc={t("settings.heartbeat.hours.sub")} htmlFor="heartbeat-hours" stack>
          <input id="heartbeat-hours" className="field settings-inline-field" defaultValue={hb.active_hours} placeholder="09:00-21:00" onBlur={(e) => e.target.value !== hb.active_hours && put({ active_hours: e.target.value })} />
        </Row>
        <NumRow id="heartbeat-max" title={t("settings.heartbeat.max")} value={hb.max_runs_per_day} min={0} onSave={(v) => put({ max_runs_per_day: v })} />
        <Row title={t("settings.heartbeat.preset")} stack>
          <Dropdown id="heartbeat-preset" label={t("settings.heartbeat.preset")} value={hb.preset} onChange={(preset) => put({ preset })} options={[{ id: "", label: t("settings.heartbeat.default") }, ...presets.map((p) => ({ id: p, label: presetName(s, p) }))]} />
        </Row>
      </div>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>
          HEARTBEAT.md
        </div>
        <textarea
          className="field"
          rows={12}
          value={text}
          placeholder={hb.template}
          onChange={(e) => {
            setText(e.target.value);
            setDirty(true);
            dirtyRef.current = true;
          }}
        />
        <div className="btnrow">
          <button className="btn primary" disabled={!dirty} onClick={() => put({ text })}>
            {t("common.save")}
          </button>
          <button className="btn small" onClick={() => { setText(hb.template ?? ""); setDirty(true); }}>
            {t("settings.heartbeat.template")}
          </button>
          <button className="btn small" onClick={() => { setText(""); setDirty(true); }}>
            {t("settings.heartbeat.clear")}
          </button>
        </div>
      </div>
    </>
  );
}

/** The doctor's checks as a screen of their own. */
export function HealthScreen({ toast }: { toast: (t: string) => void }) {
  return (
    <>
      <PageHeader title={screenTitle("health")} />
      <div className="screen narrow">
        <HealthTab toast={toast} />
        {!telegram()?.initData && (
          <div className="btnrow">
            <button
              className="btn small"
              onClick={async () => {
                try {
                  await api.post("/api/auth/logout");
                  sessionStorage.removeItem("daedalus_token");
                } finally {
                  window.location.reload();
                }
              }}
            >
              {t("settings.logout")}
            </button>
          </div>
        )}
      </div>
    </>
  );
}

/** Security: the passkeys that sign the owner in with no Telegram, no password and no pairing link. */
function SecurityTab({ toast }: { toast: (t: string) => void }) {
  const [keys, setKeys] = useState<passkeys.PasskeyView[] | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const can = passkeys.supported();
  useEffect(() => {
    passkeys
      .list()
      .then(setKeys)
      .catch((e) => toast(errorText(e)));
  }, [toast]);

  async function add() {
    setBusy(true);
    try {
      setKeys(await passkeys.enrol(name.trim() || t("settings.security.thisdevice")));
      setName("");
      toast(t("settings.security.addedtoast"));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(key: passkeys.PasskeyView) {
    if (!(await confirmAsync(t("settings.security.remove.title", { name: key.name }), { body: t("settings.security.remove.body"), action: t("common.remove") }))) return;
    try {
      setKeys((await passkeys.forget(key.id)).passkeys);
    } catch (e) {
      toast(errorText(e));
    }
  }

  const signOutEverywhere = async () => {
    if (!(await confirmAsync(t("settings.security.signoutall.title"), { body: t("settings.security.signoutall.body"), action: t("settings.security.signoutall") }))) return;
    try {
      await passkeys.signOutEverywhere();
      toast(t("settings.security.signedout"));
    } catch (e) {
      toast(errorText(e));
    }
  };

  return (
    <>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.security.title")}</div>
        <div className="sub">{t("settings.security.sub")}</div>
        {keys === null && <div className="empty">{t("common.loading")}</div>}
        {keys !== null && keys.length === 0 && <div className="sub" style={{ marginTop: 8 }}>{t("settings.security.none")}</div>}
        {keys !== null && keys.length > 0 && (
          <div className="mlist">
            {keys.map((k) => (
              <div key={k.id} className="mrow">
                <div className="mline noradio">
                  <div className="mmain">
                    <span className="mtitle">{k.name}</span>
                    <span className="mmeta">
                      {t("settings.security.added", { t: timeAgo(k.created_at) })} · {k.last_used_at ? t("settings.security.lastused", { t: timeAgo(k.last_used_at) }) : t("settings.security.neverused")}
                    </span>
                  </div>
                  <div className="mactions">
                    <button className="btn small" onClick={() => void remove(k)}>{t("common.remove")}</button>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
        {/* The name and the button that uses it are one row: stacked, the button read as a separate
            action under a field it had nothing to do with. */}
        <Row title={t("settings.security.new")} desc={can ? t("settings.security.new.sub") : t("settings.security.cannot")} stack>
          <input className="field settings-name-field" placeholder={t("settings.security.thisdevice")} aria-label={t("settings.security.name")} value={name} onChange={(e) => setName(e.target.value)} />
          <button className="btn primary small" disabled={busy || !can} onClick={() => void add()}>
            {t(busy ? "settings.security.waiting" : "settings.security.add")}
          </button>
        </Row>
      </div>
      <div className="card">
        <Row title={t("settings.security.signoutall")} desc={t("settings.security.signoutall.sub")}>
          <button className="btn small" onClick={() => void signOutEverywhere()}>{t("settings.security.signoutall.action")}</button>
        </Row>
      </div>
    </>
  );
}

type Section = "appearance" | "models" | "rules" | "limits" | "environments" | "tools" | "voice" | "components" | "dependencies" | "chat" | "notifications" | "security" | "heartbeat" | "about";
/** The sections, grouped the way the page lists them. The words come from the table, not from here. */
const GROUPS: { id: "you" | "work" | "system"; sections: { id: Section; icon: IconName }[] }[] = [
  { id: "you", sections: [
    { id: "appearance", icon: "eye" },
    { id: "notifications", icon: "bell" },
    { id: "voice", icon: "mic" },
    { id: "security", icon: "key" },
  ] },
  { id: "work", sections: [
    { id: "models", icon: "model" },
    { id: "rules", icon: "pen" },
    { id: "limits", icon: "chart" },
    { id: "tools", icon: "wrench" },
    { id: "environments", icon: "terminal" },
    { id: "chat", icon: "inbox" },
  ] },
  { id: "system", sections: [
    { id: "components", icon: "plug" },
    { id: "dependencies", icon: "wrench" },
    { id: "heartbeat", icon: "loop" },
    { id: "about", icon: "settings" },
  ] },
];
const SECTIONS = GROUPS.flatMap((group) => group.sections);

const sectionLabel = (id: Section) => t(`settings.sec.${id}`);

/** Sections that were pages of their own and are now part of another: an address written down, sent
 *  in a notification or bookmarked still lands on the setting, scrolled to the part it named. */
const MOVED: Record<string, { to: Section; anchor?: string }> = {
  terminals: { to: "environments", anchor: "terminal-sessions" },
  browser: { to: "environments", anchor: "browser-sessions" },
};

export function SettingsScreen({ toast, section }: { toast: (t: string) => void; section?: string | null }) {
  const caps = useQuery<Capabilities>("/api/capabilities", { staleMs: 20000 });
  const [s, setS] = useState<Settings | null>(null);
  const [adding, setAdding] = useState(false);
  const [status, setStatus] = useState<any>(null);
  const wide = useMedia("(min-width: 1024px)");
  const [query, setQuery] = useState("");
  const moved = section ? MOVED[section] : undefined;
  const current: Section | null = moved ? moved.to : SECTIONS.some((x) => x.id === section) ? (section as Section) : null;
  useEffect(() => {
    if (!moved) return;
    navigate(pathFor("settings", moved.to) + window.location.search, { replace: true });
  }, [moved]);
  // The part an old address named is scrolled to once it has drawn, which for the browser half is
  // after its own listing answers; a few tries rather than a guess at how long that takes.
  const [anchor, setAnchor] = useState<string | null>(null);
  useEffect(() => {
    if (moved?.anchor) setAnchor(moved.anchor);
  }, [moved]);
  useEffect(() => {
    if (!anchor) return;
    let tries = 0;
    const timer = window.setInterval(() => {
      const target = document.getElementById(anchor);
      if (target || ++tries > 20) {
        window.clearInterval(timer);
        target?.scrollIntoView({ block: "start" });
        setAnchor(null);
      }
    }, 100);
    return () => window.clearInterval(timer);
  }, [anchor]);
  const shown: Section | null = current ?? (wide ? "models" : null);
  useEffect(() => {
    api.get<Settings>("/api/settings").then(setS).catch((e) => toast((e as Error).message));
    api.get("/api/status").then(setStatus).catch(() => setStatus(null));
  }, [toast]);

  async function save(patch: Partial<Settings>) {
    if (!s?.revision) return;
    try {
      const validation = await api.post<{ valid: boolean; stale: boolean; problems: { message: string }[] }>("/api/settings/validate", { base_revision: s.revision, candidate: patch });
      if (validation.stale) {
        const current = await api.get<Settings>("/api/settings");
        setS(current);
        throw new Error(t("settings.validation.stale"));
      }
      // An answer without its list of problems still ends in a sentence the operator can read; indexing
      // the missing list put "Cannot read properties of undefined (reading '0')" in a toast.
      if (!validation.valid) throw new Error(validation.problems?.[0]?.message || t("settings.validation.invalid"));
      const next = await api.put<Settings>("/api/settings", { ...patch, base_revision: s.revision });
      setS({ ...next, providers_available: next.providers_available ?? s?.providers_available ?? [] });
      toast(t("common.saved"));
    } catch (e) {
      toast((e as Error).message);
    }
  }
  async function patchProvider(id: string, patch: Record<string, unknown>): Promise<Settings | undefined> {
    try {
      const next = await api.put<Settings>(`/api/providers/${encodeURIComponent(id)}`, patch);
      setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) });
      return next;
    } catch (e) {
      toast((e as Error).message);
      return undefined;
    }
  }
  async function patchPreset(id: string, patch: Partial<Preset>) {
    try {
      const next = await api.put<Settings>(`/api/presets/${encodeURIComponent(id)}`, patch);
      setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) });
    } catch (e) {
      toast((e as Error).message);
    }
  }
  async function removePreset(id: string) {
    try {
      const next = await api.delete<Settings>(`/api/presets/${encodeURIComponent(id)}`);
      setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) });
    } catch (e) {
      toast((e as Error).message);
    }
  }
  async function lookupProviderModels(provider: string): Promise<string[] | null> {
    try {
      return (await api.post<{ models: string[] }>("/api/providers/lookup-models", { provider })).models;
    } catch (e) {
      toast((e as Error).message);
      return null;
    }
  }
  async function removeProvider(id: string) {
    try {
      const next = await api.delete<Settings>(`/api/providers/${encodeURIComponent(id)}`);
      setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) });
      toast(t("settings.provider.removed"));
    } catch (e) {
      toast((e as Error).message);
    }
  }

  const needle = query.trim().toLowerCase();
  const matches = (id: Section | "language") => {
    const label = id === "language" ? t("settings.sec.language") : sectionLabel(id);
    const hint = id === "language" ? t("lang.hint") : t(`settings.sec.${id}.hint`);
    return !needle || `${label} ${hint}`.toLowerCase().includes(needle);
  };
  const home = modeHome(storedMode(), wide);
  const index = (
    <div className="settings-index">
      <a className="settings-back" href={home} onClick={(e) => go(e, home)}>
        <Icon name="back" size={16} /> {t("settings.back")}
      </a>
      <input className="field settings-search" value={query} placeholder={t("common.search")} aria-label={t("common.search")} onChange={(e) => setQuery(e.target.value)} />
      {/* The language is answered here: a reader who cannot read the rest of the page should not
          have to open a section to change the language of the page. */}
      {matches("language") && (
        <div className="settings-link settings-lang">
          <Icon name="globe" size={18} />
          <span className="settings-link-text">
            <b>{t("settings.sec.language")}</b>
            <span className="sub">{t("lang.hint")}</span>
          </span>
          <LangPicker />
        </div>
      )}
      {GROUPS.map((group) => {
        const items = group.sections.filter((sec) => matches(sec.id));
        if (!items.length) return null;
        return (
          <div key={group.id}>
            <div className="settings-group">{t(`settings.group.${group.id}`)}</div>
            {items.map((sec) => (
              <a key={sec.id} href={pathFor("settings", sec.id)} className={`settings-link ${shown === sec.id ? "active" : ""}`} aria-current={shown === sec.id ? "page" : undefined} onClick={(e) => go(e, pathFor("settings", sec.id))}>
                <Icon name={sec.icon} size={18} />
                <span className="settings-link-text">
                  <b>
                    {sectionLabel(sec.id)}
                    {/* A mark, not a count: the one thing worth interrupting a reader for is a feature they
                        have already configured whose runtime is not installed. */}
                    {sec.id === "components" && componentsNeedAttention(caps.data) && <span className="tab-badge dot settings-mark" aria-label={t("comp.state.missing")} />}
                  </b>
                  <span className="sub">{t(`settings.sec.${sec.id}.hint`)}</span>
                </span>
                <span className="chev">›</span>
              </a>
            ))}
          </div>
        );
      })}
      {needle && !matches("language") && GROUPS.every((group) => group.sections.every((sec) => !matches(sec.id))) && <div className="sub">{t("settings.search.empty")}</div>}
    </div>
  );

  const body = (sec: Section) => {
    if (sec === "appearance") return <AppearancePanel />;
    if (!s) return <div className="empty">{t("common.loading")}</div>;
    const kinds = s.provider_kinds ?? DEFAULT_KINDS;
    const providerIds = Object.keys(s.providers ?? {});
    switch (sec) {
      case "models":
        return (
          <>
            <div className="card">
              <div className="section-title" style={{ marginTop: 0 }}>{t("settings.models.title")}</div>
              <div className="sub">{t("settings.models.sub")}</div>
              <div className="mlist">
                {Object.entries(s.presets ?? {}).map(([id, p]) => (
                  <PresetRow
                    key={id}
                    id={id}
                    p={p}
                    onDemandByModel={s.on_demand_defaults?.[id]}
                    isDefault={s.model.preset === id}
                    inChain={(s.model.chain ?? []).includes(id)}
                    providers={providerIds}
                    onDefault={() => save({ model: { preset: id } as any })}
                    onChain={() => save({ model: { chain: (s.model.chain ?? []).includes(id) ? s.model.chain.filter((c) => c !== id) : [...(s.model.chain ?? []), id] } as any })}
                    onPatch={(patch) => void patchPreset(id, patch)}
                    onDelete={() => void removePreset(id)}
                    onLookup={lookupProviderModels}
                  />
                ))}
              </div>
              {Object.keys(s.presets ?? {}).length === 0 && (
                <div className="empty">
                  <b>{t("settings.models.empty")}</b>
                  <div className="sub">{t("settings.models.empty.sub")}</div>
                </div>
              )}
              <div className="btnrow">
                <button className="btn primary" onClick={() => setAdding(true)}>
                  <Icon name="plus" size={14} /> {t("settings.models.add")}
                </button>
                <span className="sub faint" style={{ alignSelf: "center" }}>{t("settings.models.add.hint")}</span>
              </div>
              <div className="sub" style={{ marginTop: 10 }}>{t("settings.models.chain", { chain: (s.model.chain ?? []).length ? s.model.chain.join(" → ") : t("settings.models.chain.none") })}</div>
            </div>
            {Object.keys(s.presets ?? {}).length > 0 && (
              <div className="card">
                <div className="section-title" style={{ marginTop: 0 }}>{t("settings.orchestrator.title")}</div>
                <div className="sub">{t("settings.orchestrator.sub")}</div>
                <Row title={t("settings.orchestrator.model")} desc={t("settings.orchestrator.hint")} stack>
                  <Dropdown
                    id="orchestrator-preset"
                    label={t("settings.orchestrator.title")}
                    value={orchestratorPreset(s.presets, s.orchestrator?.preset, s.orchestrator?.strongest)}
                    onChange={(preset) => save({ orchestrator: { preset } })}
                    options={Object.keys(s.presets).map((id) => ({ id, label: presetName(s, id) + (id === s.orchestrator?.strongest ? ` · ${t("settings.orchestrator.strongest")}` : "") }))}
                  />
                </Row>
              </div>
            )}
            {Object.keys(s.presets ?? {}).length > 0 && (
              <div className="card" data-card="main-orchestrator">
                <div className="section-title" style={{ marginTop: 0 }}>{t("settings.main.title")}</div>
                <div className="sub">{t("settings.main.sub")}</div>
                <Row title={t("settings.orchestrator.model")} desc={t("settings.main.hint")} stack>
                  <Dropdown
                    id="main-preset"
                    label={t("settings.main.title")}
                    value={mainPreset(s.presets, s.dispatcher?.preset, s.dispatcher?.middle)}
                    onChange={(preset) => save({ dispatcher: { preset } })}
                    options={Object.keys(s.presets).map((id) => ({ id, label: presetName(s, id) + (id === s.dispatcher?.middle ? ` · ${t("settings.main.middle")}` : "") }))}
                  />
                </Row>
              </div>
            )}
            {/* Here rather than under Tools: it is a choice of model, and this is where the operator
                comes to choose one. */}
            <div className="card vision-card">
              <div className="section-title" style={{ marginTop: 0 }}>{t("settings.vision.title")}</div>
              <div className="sub">{t("settings.vision.sub")}</div>
              <Row title={t("settings.vision.model")} stack>
                <Dropdown
                  id="vision-preset"
                  label={t("settings.vision.title")}
                  value={s.vision.preset}
                  onChange={(preset) => save({ vision: { ...s.vision, preset } })}
                  options={[
                    ...Object.keys(s.presets ?? {}).filter((id) => s.presets[id].images).map((id) => ({ id, label: presetName(s, id) })),
                    ...(s.presets?.[s.vision.preset]?.images ? [] : [{ id: s.vision.preset, label: s.vision.preset || t("settings.vision.none") }]),
                  ]}
                />
              </Row>
              <NumRow id="vision-output" title={t("settings.vision.output")} value={s.vision.max_output_tokens} min={100} step={100} onSave={(v) => save({ vision: { ...s.vision, max_output_tokens: v } })} />
            </div>
            <div className="card">
              <div className="section-title" style={{ marginTop: 0 }}>{t("settings.providers.title")}</div>
              <div className="sub">{t("settings.providers.sub")}</div>
              <div className="mlist">
                {providerIds.map((id) => (
                  <ProviderBlock key={id} id={id} p={s.providers[id]} kinds={kinds} available={(s.providers_available ?? []).includes(id)} onPatch={patchProvider} onRemove={(pid) => void removeProvider(pid)} />
                ))}
              </div>
              <AddProviderRow kinds={kinds} toast={toast} onAdd={(pid, base, kind) => void patchProvider(pid, { kind, base_url: base })} />
            </div>
          </>
        );
      case "rules":
        return (
          <>
            <PromptChange onApplied={(rules) => setS({ ...s, prompt: { ...s.prompt, rules } })} />
            <RulesEditor rules={s.prompt.rules} fallback={s.prompt.default_rules ?? ""} onSave={(rules) => save({ prompt: { rules } })} />
            <div className="card">
              <div className="section-title" style={{ marginTop: 0 }}>{t("settings.selfchange")}</div>
              <div className="sub">{t("settings.selfchange.sub")}</div>
              {/* Two separate rules, each a segmented pick like every other choice in Settings. They
                  were three loose buttons once, the third a toggle that looked like a third mode. */}
              <Row title={t("settings.selfchange.approval")}>
                <Segmented label={t("settings.selfchange.approval")} value={s.self_change.approval} onChange={(approval) => save({ self_change: { ...s.self_change, approval } })} options={["manual", "auto"].map((id) => ({ id, label: t(`settings.selfchange.${id}`) }))} />
              </Row>
              <Row title={t("settings.selfchange.rebuild")}>
                <Segmented label={t("settings.selfchange.rebuild")} value={s.self_change.auto_rebuild ? "on" : "off"} onChange={(state) => save({ self_change: { ...s.self_change, auto_rebuild: state === "on" } })} options={(["off", "on"] as const).map((id) => ({ id, label: t(`settings.selfchange.rebuild.${id}`) }))} />
              </Row>
            </div>
          </>
        );
      case "limits":
        return (
          <div className="card">
            <div className="section-title" style={{ marginTop: 0 }}>{t("settings.limits.title")}</div>
            {/* Only where the environment sets one: without it the line read "daily cap: $undefined". */}
            {typeof (s as any).usd_per_day === "number" && <div className="sub">{t("settings.limits.daily", { n: (s as any).usd_per_day })}</div>}
            <NumRow id="limits-iterations" title={t("settings.limits.iterations")} value={s.limits.max_iterations} min={1} onSave={(v) => save({ limits: { ...s.limits, max_iterations: v } })} />
            <NumRow id="limits-perrun" title={t("settings.limits.perrun")} desc={t("settings.limits.perrun.sub")} unit="USD" step="0.5" value={s.limits.usd_per_run} min={0} onSave={(v) => save({ limits: { ...s.limits, usd_per_run: v } })} />
            <TotalCaps s={s} save={save} />
            <div className="section-title">{t("settings.compaction")}</div>
            <div className="sub">{t("settings.compaction.sub")}</div>
            <NumRow id="compaction-ratio" title={t("settings.compaction.ratio")} desc={t("settings.compaction.ratio.sub")} value={s.compaction?.auto_ratio ?? 0.5} min={0} max={1} step={0.05} onSave={(v) => save({ compaction: { ...s.compaction, auto_ratio: v } })} />
            <NumRow id="compaction-keep" title={t("settings.compaction.keep")} value={s.compaction?.keep_recent_messages ?? 6} min={0} onSave={(v) => save({ compaction: { ...s.compaction, keep_recent_messages: v } })} />
            <NumRow id="compaction-words" title={t("settings.compaction.words")} unit={t("settings.unit.words")} value={s.compaction?.max_words ?? 1200} min={200} step={100} onSave={(v) => save({ compaction: { ...s.compaction, max_words: v } })} />
            <NumRow id="compaction-core" title={t("settings.compaction.core")} desc={t("settings.compaction.core.hint")} value={s.compaction?.core_trigger_ratio ?? 0.85} min={0.1} max={1} step={0.05} onSave={(v) => save({ compaction: { ...s.compaction, core_trigger_ratio: v } })} />
            <CompactionModelSelect presets={s.presets} value={s.compaction?.preset ?? ""} onSave={(preset) => save({ compaction: { ...s.compaction, preset } })} />
            <NumRow id="compaction-timeout" title={t("settings.compaction.timeout")} desc={t("settings.compaction.timeout.hint")} unit={t("settings.unit.seconds")} value={s.compaction?.call_timeout_seconds ?? 90} min={10} step={10} onSave={(v) => save({ compaction: { ...s.compaction, call_timeout_seconds: v } })} />
            {/* A heading of their own: they trailed the compaction fields with nothing to say they
                were a different subject. */}
            <div className="section-title">{t("settings.balance.title")}</div>
            <Row title={t("settings.balance.thresholds")} desc={t("settings.balance.thresholds.sub")} htmlFor="balance-thresholds" stack>
              <input id="balance-thresholds" className="field settings-inline-field" defaultValue={s.balance.thresholds_usd.join(", ")} onBlur={(e) => save({ balance: { ...s.balance, thresholds_usd: e.target.value.split(",").map(Number).filter((n) => !Number.isNaN(n)) } })} />
            </Row>
            <NumRow id="balance-poll" title={t("settings.balance.poll")} unit={t("settings.unit.seconds")} value={s.balance.poll_seconds} min={1} onSave={(v) => save({ balance: { ...s.balance, poll_seconds: v } })} />
          </div>
        );
      case "environments":
        return <EnvironmentsTab s={s} save={save} toast={toast} />;
      case "tools":
        return <ToolsTab s={s} save={save} toast={toast} onSettings={(next) => setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) })} />;
      case "voice":
        return (
          <>
            <VoiceSettings toast={toast} />
            <AsrSettingsCard s={s} save={save} />
            <SpeechModels toast={toast} />
            <TtsVoices toast={toast} />
          </>
        );
      case "components":
        return <ComponentsTab toast={toast} />;
      case "dependencies":
        return <DependenciesTab />;
      case "chat": {
        const mode = s.telegram.mode ?? (s.telegram.forum_chat_id ? "topics" : "private");
        const tg = (patch: Partial<Settings["telegram"]>) => save({ telegram: { ...s.telegram, ...patch } });
        return (
          <>
            <div className="card">
              <div className="section-title" style={{ marginTop: 0 }}>{t("settings.chat.telegram")}</div>
              <Row title={t("settings.chat.where")} desc={mode === "private" ? t("settings.chat.private.sub") : t(s.telegram.forum_chat_id ? "settings.chat.topics.sub" : "settings.chat.topics.bind")} stack>
                <Segmented label={t("settings.chat.where")} value={mode} onChange={(m) => tg({ mode: m })} options={(["private", "topics"] as const).map((m) => ({ id: m, label: t(`settings.chat.${m}`) }))} />
              </Row>
              {/* The digits are the ones /verbosity takes in the chat, so they stay; the description is
                  what they mean, which nothing on the page said. */}
              <Row title={t("settings.chat.verbosity")} desc={t("settings.chat.verbosity.sub")}>
                <Segmented label={t("settings.chat.verbosity")} value={String(s.telegram.verbosity)} onChange={(v) => tg({ verbosity: Number(v) })} options={["0", "1", "2"].map((v) => ({ id: v, label: v }))} />
              </Row>
              <Row title={t("settings.chat.reactions")} desc={t("settings.chat.reactions.sub")}>
                <Switch checked={!!s.telegram.reactions} onChange={(reactions) => tg({ reactions })} label={t("settings.chat.reactions")} />
              </Row>
              <Row title={t("settings.chat.topicemoji")} desc={t("settings.chat.topicemoji.sub")}>
                <Switch checked={!!s.telegram.topic_status_emoji} onChange={(topic_status_emoji) => tg({ topic_status_emoji })} label={t("settings.chat.topicemoji")} />
              </Row>
              <Row title={t("settings.chat.forward")} desc={t("settings.chat.forward.sub")}>
                <Switch checked={!!s.telegram.forward_unknown_commands} onChange={(forward_unknown_commands) => tg({ forward_unknown_commands })} label={t("settings.chat.forward")} />
              </Row>
              <NumRow id="chat-stale" title={t("settings.chat.stale")} desc={t("settings.chat.stale.sub")} unit={t("settings.unit.seconds")} value={s.telegram.stale_after_seconds} min={0} onSave={(v) => tg({ stale_after_seconds: v })} />
              <NumRow id="chat-maxfile" title={t("settings.chat.maxfile")} unit={t("settings.unit.mb")} value={s.telegram.max_inbound_file_mb} min={1} onSave={(v) => tg({ max_inbound_file_mb: v })} />
              <NumRow id="chat-caption" title={t("settings.chat.caption")} unit={t("settings.unit.seconds")} value={s.telegram.photo_caption_wait_seconds} min={0} onSave={(v) => tg({ photo_caption_wait_seconds: v })} />
              <NumRow id="chat-slowtool" title={t("settings.chat.slowtool")} unit={t("settings.unit.seconds")} value={s.telegram.slow_tool_seconds} min={1} onSave={(v) => tg({ slow_tool_seconds: v })} />
            </div>
            <div className="card">
              <div className="section-title" style={{ marginTop: 0 }}>{t("settings.chat.scheduler")}</div>
              <Row title={t("settings.chat.scheduled")} desc={t("settings.chat.scheduled.sub")} stack>
                <Segmented label={t("settings.chat.scheduled")} value={s.scheduler.topic_mode} onChange={(topic_mode) => save({ scheduler: { ...s.scheduler, topic_mode } })} options={["per_task", "per_run"].map((m) => ({ id: m, label: t(m === "per_task" ? "settings.chat.pertask" : "settings.chat.perrun") }))} />
              </Row>
            </div>
          </>
        );
      }
      case "notifications":
        return <NotificationSettings toast={toast} />;
      case "security":
        return <SecurityTab toast={toast} />;
      case "heartbeat":
        return <HeartbeatTab s={s} toast={toast} />;
      case "about": {
        const logout = async () => {
          try {
            await api.post("/api/auth/logout");
            sessionStorage.removeItem("daedalus_token");
          } finally {
            window.location.reload();
          }
        };
        const value = (text: React.ReactNode, mono = false) => <span className={`settings-value ${mono ? "mono" : ""}`}>{text}</span>;
        return (
          <>
            <DesktopAppCard toast={toast} />
            <div className="card">
              <div className="section-title" style={{ marginTop: 0 }}>{t("settings.about.runtime")}</div>
              {status ? (
                <>
                  <Row title={t("settings.about.providers")}>{value(status.providers?.length ? status.providers.join(", ") : "—")}</Row>
                  {status.supervisor ? (
                    <>
                      <Row title={t("settings.about.bot")}>{value(String(status.supervisor.bot).slice(0, 10), true)}</Row>
                      <Row title={t("settings.about.core")}>{value(String(status.supervisor.core).slice(0, 10), true)}</Row>
                      <Row title={t("settings.about.process")}>{value(t(status.supervisor.child_running ? "settings.about.running" : "settings.about.stopped"))}</Row>
                    </>
                  ) : (
                    <Row title={t("settings.about.supervisor")}>{value(t("settings.about.nosupervisor.short"))}</Row>
                  )}
                  {status.budget_exceeded && <div className="sub" style={{ color: "var(--bad)" }}>{t("settings.about.budget", { what: String(status.budget_exceeded) })}</div>}
                </>
              ) : (
                <div className="sub">{t("settings.about.nostatus")}</div>
              )}
            </div>
            {!telegram()?.initData && (
              <div className="card">
                <div className="section-title" style={{ marginTop: 0 }}>{t("settings.about.browser")}</div>
                <Row title={t("settings.sec.appearance")} desc={t("settings.about.appearance")}>
                  <a className="btn small" href={pathFor("settings", "appearance")} onClick={(e) => go(e, pathFor("settings", "appearance"))}>{t("settings.about.open")}</a>
                </Row>
                <Row title={t("settings.logout")} desc={t("settings.about.logout.sub")}>
                  <button className="btn small" onClick={() => void logout()}>{t("settings.about.logout.action")}</button>
                </Row>
              </div>
            )}
          </>
        );
      }
    }
  };

  const addSheet = adding && (
    <Sheet title={t("settings.models.add")} size="wide" onClose={() => setAdding(false)}>
      <AddModel
        toast={toast}
        onCancel={() => setAdding(false)}
        onSaved={(id, next) => {
          setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) });
          setAdding(false);
          toast(t("settings.added", { id }));
        }}
      />
    </Sheet>
  );
  if (wide) {
    return (
      <>
        <div className="settings-stage">
          <aside className="settings-nav">{index}</aside>
          <div className="settings-main">
            <div className="settings-col">
              {shown && <h1 className="settings-title">{sectionLabel(shown)}</h1>}
              {shown && body(shown)}
            </div>
          </div>
        </div>
        {addSheet}
      </>
    );
  }
  if (!current) {
    return (
      <>
        <PageHeader title={screenTitle("settings")} />
        <div className="screen narrow">{index}</div>
        {addSheet}
      </>
    );
  }
  return (
    <>
      <PageHeader title={sectionLabel(current)} back={pathFor("settings")} />
      <div className="screen narrow settings-col">{body(current)}</div>
      {addSheet}
    </>
  );
}
