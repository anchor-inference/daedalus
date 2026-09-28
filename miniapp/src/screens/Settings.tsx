import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Icon, IconName } from "../icons";
import { pathFor } from "../router";
import { PageHeader, go, screenTitle, useMedia } from "../shell";
import { api, telegram, HeartbeatStatus, Preset, ProviderConf, SearchBackendInfo, SearchCheck, Settings } from "../api";
import { confirmAsync, errorText, numInput } from "../ui";
import * as passkeys from "../passkeys";
import { timeAgo } from "../components";
import { shortDateTime } from "../format";
import { SpeechModels } from "./Speech";
import { TtsVoices } from "./Voices";
import { VoiceSettings } from "./Voice";
import { ComponentsTab } from "./Components";
import { DependenciesTab } from "./Dependencies";
import { PromptChange } from "./PromptChange";
import { AddModel } from "./AddModel";
import { ON_DEMAND_CHOICES, REASONING_EFFORTS, onDemandGroups, orchestratorPreset } from "../models";
import { mainPreset } from "../main/model";
import { Sheet } from "../dialogs";
import { t } from "../i18n";
import { LangPicker, Segmented } from "../components";
import { AppearancePanel } from "./Appearance";
import { modeHome, storedMode } from "../mode";
import { useQuery } from "../store";
import { Capabilities, componentsNeedAttention } from "../capabilities";
import { NotificationSettings } from "./NotificationSettings";
import { TerminalCap } from "./TerminalCap";
import { BrowserSettingsTab } from "./BrowserSettings";
import { ToolGroupsSettings } from "../toolgroupsview";
import { CompactionModelSelect } from "../compactionmodel";

const DEFAULT_KINDS = ["deepseek", "openrouter", "opencode", "vllm", "llamacpp", "openai_compat"];
/** Self-hosted endpoints: temperature is a sampling pin, not a vendor default. */
const LOCAL_KINDS = new Set(["vllm", "llamacpp", "openai_compat"]);

function RulesEditor({ rules, fallback, onSave }: { rules: string; fallback: string; onSave: (rules: string) => void }) {
  const [text, setText] = useState(rules || fallback);
  const [dirty, setDirty] = useState(false);
  useEffect(() => {
    setText(rules || fallback);
    setDirty(false);
  }, [rules, fallback]);
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.rules.title")}</div>
      <div className="sub">{t("settings.rules.sub")} {t(rules ? "settings.rules.custom" : "settings.rules.default")}</div>
      <textarea className="field rules-editor-field" rows={10} value={text} onChange={(e) => (setText(e.target.value), setDirty(true))} style={{ fontFamily: "var(--mono)", fontSize: 12.5, marginTop: 8 }} />
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

function Toggle({ on, onClick, children, title, disabled }: { on: boolean; onClick: () => void; children: React.ReactNode; title?: string; disabled?: boolean }) {
  return (
    <button className={`btn small ${on ? "primary" : ""}`} onClick={onClick} title={title} disabled={disabled} aria-pressed={on}>
      {children}
    </button>
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
          <div className="btnrow">
            <Toggle on={p.thinking} onClick={() => onPatch({ thinking: !p.thinking })}>{t("settings.preset.thinking", { state: t(p.thinking ? "common.on" : "common.off") })}</Toggle>
            <div className="segmented inline" role="group" aria-label={t("settings.preset.effort")}>
              {REASONING_EFFORTS.map((e) => (
                <button key={e} className={p.reasoning_effort === e ? "on" : ""} disabled={!p.thinking} onClick={() => onPatch({ reasoning_effort: e })}>{t(`add.effort.${e}`)}</button>
              ))}
            </div>
            <Toggle on={p.images} onClick={() => onPatch({ images: !p.images })} title={t("settings.preset.images.title")}>{t("settings.preset.imagestoggle", { state: t(p.images ? "common.on" : "common.off") })}</Toggle>
            <span className="sub" title={t("settings.preset.ondemand.title")}>{t("settings.preset.ondemand")}</span>
            <div className="segmented inline" role="group" aria-label={t("settings.preset.ondemand")} title={t("settings.preset.ondemand.title")}>
              {ON_DEMAND_CHOICES.map((choice) => (
                <button key={String(choice)} className={(p.on_demand_tool_groups ?? null) === choice ? "on" : ""} onClick={() => onPatch({ on_demand_tool_groups: choice })}>
                  {choice === null ? t("settings.preset.ondemand.bymodel", { state: t(onDemandByModel ?? true ? "common.on" : "common.off") }) : t(choice ? "common.on" : "common.off")}
                </button>
              ))}
            </div>
            {!isDefault && <Toggle on={inChain} onClick={onChain} title={t("settings.preset.fallback.title")}>{t(inChain ? "settings.preset.isfallback" : "settings.preset.usefallback")}</Toggle>}
            <span className="sub mono mid">{id}</span>
          </div>
          <div className="btnrow mrow-foot">
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
            {LOCAL_KINDS.has(p.kind) && (
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
          <input className="field" value={id} placeholder="local-vllm" onChange={(e) => setId(e.target.value)} />
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
          <span>base_url</span>
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

type SpendView = { since: string; total: { spent_usd: number; unmetered: number; cap_usd: number }; per_provider: Record<string, { spent_usd: number; unmetered: number; cap_usd: number }> };

function TotalCaps({ s, save }: { s: Settings; save: (patch: any) => Promise<void> }) {
  const [spend, setSpend] = useState<SpendView | null>(null);
  const load = useCallback(() => {
    api.get<SpendView>("/api/limits/spend").then(setSpend).catch(() => setSpend(null));
  }, []);
  useEffect(load, [load, s.limits.total_since, s.limits.usd_total, s.limits.usd_total_per_provider]);
  const providers = Object.keys(s.providers ?? {});
  const caps = s.limits.usd_total_per_provider ?? {};
  return (
    <>
      <label className="field">{t("settings.limits.total")}</label>
      <div className="composer-row">
        <input className="field" type="number" step="1" min={0} defaultValue={s.limits.usd_total} onBlur={(e) => { const v = numInput(e.target.value, 0); if (v !== null && v !== s.limits.usd_total) save({ limits: { usd_total: v } }); }} />
        <span className="sub" style={{ whiteSpace: "nowrap" }}>{t("settings.limits.spent", { sum: spend ? `$${spend.total.spent_usd.toFixed(2)}` : "…" })}</span>
      </div>
      <label className="field">{t("settings.limits.perprovider")}</label>
      {/* Said, not left blank: an empty space under the heading read as a list that failed to load. */}
      {providers.length === 0 && <div className="sub">{t("settings.limits.noproviders")}</div>}
      {providers.map((pid) => (
        <div key={pid} className="composer-row" style={{ marginBottom: 6 }}>
          <span style={{ minWidth: 90 }}>{pid}</span>
          <input className="field" type="number" step="1" min={0} defaultValue={caps[pid] ?? 0} onBlur={(e) => { const v = numInput(e.target.value, 0); if (v !== null && v !== (caps[pid] ?? 0)) save({ limits: { usd_total_per_provider: { [pid]: v } } }); }} />
          <span className="sub" style={{ whiteSpace: "nowrap" }}>{t("settings.limits.spent", { sum: spend?.per_provider[pid] ? `$${spend.per_provider[pid].spent_usd.toFixed(2)}` : "$0.00" })}</span>
        </div>
      ))}
      <div className="row" style={{ alignItems: "center", gap: 10 }}>
        <span className="sub">{t("settings.limits.since", { when: s.limits.total_since ? shortDateTime(s.limits.total_since) : t("settings.limits.since.start") })}</span>
        <button className="btn small" onClick={async () => { await api.post("/api/limits/reset-total"); load(); }}>{t("settings.limits.reset")}</button>
      </div>
    </>
  );
}

function NumField({ label, value, min, step, onSave, hint }: { label: string; value: number; min?: number; step?: number; onSave: (v: number) => void; hint?: string }) {
  return (
    <div>
      <label className="field">{label}</label>
      <input className="field" type="number" min={min} step={step} defaultValue={value} onBlur={(e) => { const v = numInput(e.target.value, min); if (v !== null && v !== value) onSave(v); }} />
      {hint && <div className="sub">{hint}</div>}
    </div>
  );
}

function TextField({ label, value, placeholder, onSave, hint }: { label: string; value: string; placeholder?: string; onSave: (v: string) => void; hint?: string }) {
  return (
    <div>
      <label className="field">{label}</label>
      <input className="field" defaultValue={value} placeholder={placeholder} onBlur={(e) => e.target.value.trim() !== value && onSave(e.target.value.trim())} />
      {hint && <div className="sub">{hint}</div>}
    </div>
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
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.search.title")}</div>
      <div className="sub">{t("settings.search.sub")}</div>
      <label className="field">{t("settings.search.backend")}</label>
      <select className="field" value={search.backend} onChange={(e) => saveSearch({ backend: e.target.value })}>
        {backends.map((b) => (
          <option key={b.id} value={b.id} disabled={!usable(b)}>{optionLabel(b)}</option>
        ))}
        {!backends.some((b) => b.id === search.backend) && <option value={search.backend}>{search.backend}</option>}
      </select>
      <label className="field">{t("settings.search.fallbacks")}</label>
      <div className="btnrow">
        {backends.filter((b) => b.id !== search.backend).map((b) => {
          const on = fallbackIds.includes(b.id);
          return (
            <button
              key={b.id}
              className={`btn small ${on ? "primary" : ""}`}
              disabled={!on && !usable(b)}
              title={optionLabel(b)}
              onClick={() => saveSearch({ fallback: on ? fallbackIds.filter((x) => x !== b.id) : [...fallbackIds, b.id] })}
            >
              {on ? `${fallbackIds.indexOf(b.id) + 1}. ` : ""}{b.id}
            </button>
          );
        })}
      </div>
      <div className="grid2">
        <NumField label={t("settings.search.results")} value={search.results} min={1} onSave={(v) => saveSearch({ results: v })} />
        <NumField label={t("settings.search.timeout")} value={search.timeout_seconds} min={1} onSave={(v) => saveSearch({ timeout_seconds: v })} />
      </div>
      {search.backend === "searxng" || fallbackIds.includes("searxng") ? (
        <>
          <div className="section-title">SearXNG</div>
          <TextField label={t("settings.search.url")} value={search.searxng.url} onSave={(v) => saveSearch({ searxng: { url: v } })} />
          <TextField label={t("settings.search.engines")} value={search.searxng.engines} placeholder="google,duckduckgo,bing" onSave={(v) => saveSearch({ searxng: { engines: v } })} />
          <div className="grid2">
            <TextField label={t("settings.search.categories")} value={search.searxng.categories} placeholder="general" onSave={(v) => saveSearch({ searxng: { categories: v } })} />
            <NumField label={t("settings.search.safe")} value={search.searxng.safesearch} min={0} onSave={(v) => saveSearch({ searxng: { safesearch: Math.min(2, v) } })} />
          </div>
        </>
      ) : null}
      {search.backend === "duckduckgo" || fallbackIds.includes("duckduckgo") ? (
        <>
          <div className="section-title">DuckDuckGo</div>
          <div className="grid2">
            <TextField label={t("settings.search.html")} value={search.duckduckgo.url} onSave={(v) => saveSearch({ duckduckgo: { url: v } })} />
            <TextField label={t("settings.search.region")} value={search.duckduckgo.region} placeholder="wt-wt, ru-ru, us-en" onSave={(v) => saveSearch({ duckduckgo: { region: v } })} />
          </div>
        </>
      ) : null}
      {search.backend === "serper" || fallbackIds.includes("serper") ? (
        <>
          <div className="section-title">Serper (Google)</div>
          <div className="grid2">
            <TextField label={t("settings.search.country")} value={search.serper.gl} placeholder="ru, us" onSave={(v) => saveSearch({ serper: { gl: v } })} />
            <TextField label={t("settings.search.language")} value={search.serper.hl} placeholder="ru, en" onSave={(v) => saveSearch({ serper: { hl: v } })} />
          </div>
        </>
      ) : null}
      {search.backend === "keenable" || fallbackIds.includes("keenable") ? (
        <>
          <div className="section-title">Keenable</div>
          <NumField label={t("settings.search.snippet")} value={search.keenable.snippet_max_length} min={180} step={60} onSave={(v) => saveSearch({ keenable: { snippet_max_length: v } })} />
        </>
      ) : null}
      {search.backend === "tavily" || fallbackIds.includes("tavily") ? (
        <>
          <div className="section-title">Tavily</div>
          <label className="field">{t("settings.search.depth")}</label>
          <select className="field" value={search.tavily.depth} onChange={(e) => saveSearch({ tavily: { depth: e.target.value } })}>
            {["basic", "advanced", "fast", "ultra-fast"].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </>
      ) : null}
      {search.backend === "exa" || fallbackIds.includes("exa") ? (
        <>
          <div className="section-title">Exa</div>
          <label className="field">{t("settings.search.type")}</label>
          <select className="field" value={search.exa.type} onChange={(e) => saveSearch({ exa: { type: e.target.value } })}>
            {["auto", "instant", "fast", "deep"].map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </>
      ) : null}
      <div className="btnrow" style={{ marginTop: 12 }}>
        <button className="btn small primary" disabled={checking} onClick={() => runCheck("")}>{t(checking ? "settings.search.checking" : "settings.search.check")}</button>
        <button className="btn small" disabled={checking || !info(search.backend)} onClick={() => runCheck(search.backend)}>{t("settings.search.checkone", { name: search.backend })}</button>
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

function ToolsTab({ s, save, toast, onSettings }: { s: Settings; save: (patch: any) => Promise<void>; toast: (t: string) => void; onSettings: (next: Settings) => void }) {
  const web = s.tools.web;
  const asr = s.asr;
  return (
    <>
      <ToolGroupsSettings toast={toast} revision={s.revision} onSettings={onSettings} />
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.asr.title")}</div>
        <div className="sub">{t("settings.asr.sub")}</div>
        <label className="field">{t("settings.asr.provider")}</label>
        <select className="field" value={asr.provider || ""} onChange={(e) => save({ asr: { ...asr, api_key: "", provider: e.target.value } })}>
          <option value="">{t("settings.asr.custom")}</option>
          {Object.keys(s.providers).map((pid) => (
            <option key={pid} value={pid}>{pid}{s.providers[pid].base_url ? ` · ${s.providers[pid].base_url.replace(/^https?:\/\//, "")}` : ""}</option>
          ))}
        </select>
        {!asr.provider && (
          <>
            <label className="field">{t("settings.asr.url")}</label>
            <input className="field" defaultValue={asr.url} placeholder="https://api.openai.com/v1" onBlur={(e) => save({ asr: { ...asr, api_key: "", url: e.target.value.trim() } })} />
            <label className="field">{t(asr.api_key_set ? "settings.asr.key.set" : "settings.asr.key")}</label>
            <input className="field" type="password" defaultValue="" placeholder={asr.api_key_set ? "••••••" : ""} onBlur={(e) => e.target.value && save({ asr: { ...asr, api_key: e.target.value } })} />
          </>
        )}
        <div className="grid2">
          <div>
            <label className="field">{t("settings.asr.model")}</label>
            <input className="field" defaultValue={asr.model} placeholder={asr.provider === "openrouter" ? "openai/whisper-1" : "whisper-1"} onBlur={(e) => save({ asr: { ...asr, api_key: "", model: e.target.value.trim() } })} />
          </div>
          <div>
            <label className="field">{t("settings.asr.language")}</label>
            <input className="field" defaultValue={asr.language} onBlur={(e) => save({ asr: { ...asr, api_key: "", language: e.target.value.trim() } })} />
          </div>
        </div>
        <div className="btnrow">
          <button className={`btn small ${asr.autosend ? "primary" : ""}`} onClick={() => save({ asr: { ...asr, api_key: "", autosend: !asr.autosend } })}>
            {t("settings.asr.autosend", { state: t(asr.autosend ? "common.on" : "common.off") })}
          </button>
        </div>
      </div>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.vision.title")}</div>
        <div className="sub">{t("settings.vision.sub")}</div>
        <div className="grid2">
          <div>
            <label className="field">{t("settings.asr.model")}</label>
            <select className="field" value={s.vision.preset} onChange={(e) => save({ vision: { preset: e.target.value } })}>
              {Object.entries(s.presets ?? {}).filter(([, p]) => p.images).map(([id, p]) => (
                <option key={id} value={id}>{p.label || `${p.provider}/${p.model}`}</option>
              ))}
              {!(s.presets ?? {})[s.vision.preset]?.images && <option value={s.vision.preset}>{s.vision.preset || t("settings.vision.none")}</option>}
            </select>
          </div>
          <NumField label={t("settings.vision.output")} value={s.vision.max_output_tokens} min={100} step={100} onSave={(v) => save({ vision: { max_output_tokens: v } })} />
        </div>
      </div>

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.web.title")}</div>
        <div className="grid2">
          <NumField label={t("settings.web.timeout")} value={web.fetch_timeout_seconds} min={1} onSave={(v) => save({ tools: { web: { fetch_timeout_seconds: v } } })} />
          <NumField label={t("settings.web.maxchars")} value={web.fetch_max_chars} min={1000} step={1000} onSave={(v) => save({ tools: { web: { fetch_max_chars: v } } })} />
        </div>
        <TextField label={t("settings.web.proxy")} value={web.proxy} placeholder="socks5://127.0.0.1:1080" hint={t("settings.web.proxy.hint")} onSave={(v) => save({ tools: { web: { proxy: v } } })} />
        <TextField label={t("settings.web.ua")} value={web.user_agent} onSave={(v) => save({ tools: { web: { user_agent: v } } })} />
      </div>

      <SearchBlock s={s} save={save} />

      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.exec.title")}</div>
        <div className="grid2">
          <NumField label={t("settings.exec.timeout")} value={s.limits.tool_timeout_seconds} min={10} step={30} onSave={(v) => save({ limits: { tool_timeout_seconds: v } })} hint={t("settings.exec.timeout.hint")} />
          <NumField label={t("settings.exec.maxchars")} value={s.tools.exec.max_output_chars} min={2000} step={5000} onSave={(v) => save({ tools: { exec: { max_output_chars: v } } })} hint={t("settings.exec.maxchars.hint")} />
        </div>
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
        <div className="row">
          <div className="grow">
            <b>{t("settings.health.ok", { n: summary.ok ?? 0 })}</b> · {t("settings.health.warn", { n: summary.warn ?? 0 })} · {t("settings.health.fail", { n: summary.fail ?? 0 })}
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
        <div className="btnrow">
          <button className={`btn small ${hb.enabled ? "primary" : ""}`} onClick={() => put({ enabled: !hb.enabled })}>
            {t(hb.enabled ? "common.on" : "common.off")}
          </button>
          <button className="btn small" onClick={runNow} disabled={!text.trim() || hb.running}>
            {t("common.runnow")}
          </button>
          <span className="sub" style={{ alignSelf: "center" }}>
            {hb.armed ? t("settings.heartbeat.armed") : hb.enabled ? t("settings.heartbeat.emptyfile") : t("common.off")} · {t("settings.heartbeat.today", { done: hb.runs_today, max: hb.max_runs_per_day })} ·{" "}
            {t("settings.heartbeat.last", { t: hb.last_run ? timeAgo(hb.last_run) : t("common.never") })}
            {hb.running ? t("settings.heartbeat.running") : ""}
          </span>
        </div>
        <label className="field">{t("settings.heartbeat.interval")}</label>
        <input className="field" type="number" defaultValue={hb.interval_minutes} onBlur={(e) => { const v = numInput(e.target.value, 1); if (v !== null) put({ interval_minutes: v }); }} />
        <label className="field">{t("settings.heartbeat.hours")}</label>
        <input className="field" defaultValue={hb.active_hours} onBlur={(e) => put({ active_hours: e.target.value })} />
        <label className="field">{t("settings.heartbeat.max")}</label>
        <input className="field" type="number" defaultValue={hb.max_runs_per_day} onBlur={(e) => { const v = numInput(e.target.value, 0); if (v !== null) put({ max_runs_per_day: v }); }} />
        <label className="field">{t("settings.heartbeat.preset")}</label>
        <select className="field" value={hb.preset} onChange={(e) => put({ preset: e.target.value })}>
          <option value="">{t("settings.heartbeat.default")}</option>
          {presets.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
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

  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.security.title")}</div>
      <div className="sub">{t("settings.security.sub")}</div>
      <div className="btnrow" style={{ marginTop: 8 }}>
        <button
          className="btn small"
          onClick={async () => {
            if (!(await confirmAsync(t("settings.security.signoutall.title"), { body: t("settings.security.signoutall.body"), action: t("settings.security.signoutall") }))) return;
            try {
              await passkeys.signOutEverywhere();
              toast(t("settings.security.signedout"));
            } catch (e) {
              toast(errorText(e));
            }
          }}
        >
          {t("settings.security.signoutall")}
        </button>
      </div>
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
      <label className="field">{t("settings.security.name")}</label>
      <input className="field" placeholder={t("settings.security.thisdevice")} value={name} onChange={(e) => setName(e.target.value)} />
      <div className="btnrow">
        <button className="btn primary small" disabled={busy || !can} onClick={() => void add()}>
          {t(busy ? "settings.security.waiting" : "settings.security.add")}
        </button>
      </div>
      {!can && (
        <div className="sub">{t("settings.security.cannot")}</div>
      )}
    </div>
  );
}

type Section = "appearance" | "models" | "rules" | "limits" | "terminals" | "browser" | "tools" | "voice" | "components" | "dependencies" | "chat" | "notifications" | "security" | "heartbeat" | "about";
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
    { id: "terminals", icon: "terminal" },
    { id: "browser", icon: "globe" },
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

export function SettingsScreen({ toast, section }: { toast: (t: string) => void; section?: string | null }) {
  const caps = useQuery<Capabilities>("/api/capabilities", { staleMs: 20000 });
  const [s, setS] = useState<Settings | null>(null);
  const [adding, setAdding] = useState(false);
  const [status, setStatus] = useState<any>(null);
  const wide = useMedia("(min-width: 1024px)");
  const [query, setQuery] = useState("");
  const current: Section | null = SECTIONS.some((x) => x.id === section) ? (section as Section) : null;
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
                <label className="field" htmlFor="orchestrator-preset">{t("settings.orchestrator.model")}</label>
                <select
                  id="orchestrator-preset"
                  className="field"
                  value={orchestratorPreset(s.presets, s.orchestrator?.preset, s.orchestrator?.strongest)}
                  onChange={(e) => save({ orchestrator: { preset: e.target.value } })}
                >
                  {Object.entries(s.presets).map(([id, p]) => (
                    <option key={id} value={id}>
                      {(p.label || `${p.provider}/${p.model}`) + (id === s.orchestrator?.strongest ? ` · ${t("settings.orchestrator.strongest")}` : "")}
                    </option>
                  ))}
                </select>
                <div className="sub faint">{t("settings.orchestrator.hint")}</div>
              </div>
            )}
            {Object.keys(s.presets ?? {}).length > 0 && (
              <div className="card" data-card="main-orchestrator">
                <div className="section-title" style={{ marginTop: 0 }}>{t("settings.main.title")}</div>
                <div className="sub">{t("settings.main.sub")}</div>
                <label className="field" htmlFor="main-preset">{t("settings.orchestrator.model")}</label>
                <select
                  id="main-preset"
                  className="field"
                  value={mainPreset(s.presets, s.dispatcher?.preset, s.dispatcher?.middle)}
                  onChange={(e) => save({ dispatcher: { preset: e.target.value } })}
                >
                  {Object.entries(s.presets).map(([id, p]) => (
                    <option key={id} value={id}>
                      {(p.label || `${p.provider}/${p.model}`) + (id === s.dispatcher?.middle ? ` · ${t("settings.main.middle")}` : "")}
                    </option>
                  ))}
                </select>
                <div className="sub faint">{t("settings.main.hint")}</div>
              </div>
            )}
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
              <div className="settings-choice">
                <span>{t("settings.selfchange.approval")}</span>
                <Segmented label={t("settings.selfchange.approval")} value={s.self_change.approval} onChange={(approval) => save({ self_change: { ...s.self_change, approval } })} options={["manual", "auto"].map((id) => ({ id, label: t(`settings.selfchange.${id}`) }))} />
              </div>
              <div className="settings-choice">
                <span>{t("settings.selfchange.rebuild")}</span>
                <Segmented label={t("settings.selfchange.rebuild")} value={s.self_change.auto_rebuild ? "on" : "off"} onChange={(state) => save({ self_change: { ...s.self_change, auto_rebuild: state === "on" } })} options={(["off", "on"] as const).map((id) => ({ id, label: t(`settings.selfchange.rebuild.${id}`) }))} />
              </div>
            </div>
          </>
        );
      case "limits":
        return (
          <div className="card">
            <div className="section-title" style={{ marginTop: 0 }}>{t("settings.limits.title")}</div>
            <div className="sub">{t("settings.limits.daily", { n: (s as any).usd_per_day })}</div>
            <label className="field">{t("settings.limits.iterations")}</label>
            <input className="field" type="number" defaultValue={s.limits.max_iterations} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ limits: { ...s.limits, max_iterations: v } }); }} />
            <label className="field">{t("settings.limits.perrun")}</label>
            <input className="field" type="number" step="0.5" defaultValue={s.limits.usd_per_run} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ limits: { ...s.limits, usd_per_run: v } }); }} />
            <TotalCaps s={s} save={save} />
            <div className="section-title">{t("settings.compaction")}</div>
            <div className="sub">{t("settings.compaction.sub")}</div>
            <div className="grid2">
              <NumField label={t("settings.compaction.ratio")} value={s.compaction?.auto_ratio ?? 0.5} min={0} step={0.05} onSave={(v) => save({ compaction: { ...s.compaction, auto_ratio: v } })} />
              <NumField label={t("settings.compaction.keep")} value={s.compaction?.keep_recent_messages ?? 6} min={0} onSave={(v) => save({ compaction: { ...s.compaction, keep_recent_messages: v } })} />
              <NumField label={t("settings.compaction.words")} value={s.compaction?.max_words ?? 1200} min={200} step={100} onSave={(v) => save({ compaction: { ...s.compaction, max_words: v } })} />
              <NumField label={t("settings.compaction.core")} value={s.compaction?.core_trigger_ratio ?? 0.85} min={0.1} step={0.05} onSave={(v) => save({ compaction: { ...s.compaction, core_trigger_ratio: v } })} hint={t("settings.compaction.core.hint")} />
              <CompactionModelSelect presets={s.presets} value={s.compaction?.preset ?? ""} onSave={(preset) => save({ compaction: { ...s.compaction, preset } })} />
              <NumField label={t("settings.compaction.timeout")} value={s.compaction?.call_timeout_seconds ?? 90} min={10} step={10} onSave={(v) => save({ compaction: { ...s.compaction, call_timeout_seconds: v } })} hint={t("settings.compaction.timeout.hint")} />
            </div>
            <label className="field">{t("settings.balance.thresholds")}</label>
            <input className="field" defaultValue={s.balance.thresholds_usd.join(", ")} onBlur={(e) => save({ balance: { ...s.balance, thresholds_usd: e.target.value.split(",").map(Number).filter((n) => !Number.isNaN(n)) } })} />
            <label className="field">{t("settings.balance.poll")}</label>
            <input className="field" type="number" defaultValue={s.balance.poll_seconds} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ balance: { ...s.balance, poll_seconds: v } }); }} />
          </div>
        );
      case "terminals":
        return <TerminalCap s={s} save={save} />;
      case "browser":
        return <BrowserSettingsTab s={s} save={save} toast={toast} />;
      case "tools":
        return <ToolsTab s={s} save={save} toast={toast} onSettings={(next) => setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) })} />;
      case "voice":
        return (
          <>
            <VoiceSettings toast={toast} />
            <SpeechModels toast={toast} />
            <TtsVoices toast={toast} />
          </>
        );
      case "components":
        return <ComponentsTab toast={toast} />;
      case "dependencies":
        return <DependenciesTab />;
      case "chat":
        return (
          <div className="card">
            <div className="section-title" style={{ marginTop: 0 }}>{t("settings.chat.title")}</div>
            <label className="field">{t("settings.chat.where")}</label>
            <div className="btnrow" style={{ marginTop: 0 }}>
              {(["private", "topics"] as const).map((m) => (
                <button key={m} className={`btn small ${(s.telegram.mode ?? (s.telegram.forum_chat_id ? "topics" : "private")) === m ? "primary" : ""}`} onClick={() => save({ telegram: { ...s.telegram, mode: m } })}>
                  {t(`settings.chat.${m}`)}
                </button>
              ))}
            </div>
            <div className="sub">
              {(s.telegram.mode ?? (s.telegram.forum_chat_id ? "topics" : "private")) === "private"
                ? t("settings.chat.private.sub")
                : t(s.telegram.forum_chat_id ? "settings.chat.topics.sub" : "settings.chat.topics.bind")}
            </div>
            <label className="field">{t("settings.chat.verbosity")}</label>
            <div className="btnrow" style={{ marginTop: 0 }}>
              {[0, 1, 2].map((v) => (
                <button key={v} className={`btn small ${s.telegram.verbosity === v ? "primary" : ""}`} onClick={() => save({ telegram: { ...s.telegram, verbosity: v } })}>
                  {v}
                </button>
              ))}
            </div>
            <div className="btnrow">
              <button className={`btn small ${s.telegram.reactions ? "primary" : ""}`} onClick={() => save({ telegram: { ...s.telegram, reactions: !s.telegram.reactions } })}>
                {t("settings.chat.reactions", { state: t(s.telegram.reactions ? "common.on" : "common.off") })}
              </button>
              <button className={`btn small ${s.telegram.topic_status_emoji ? "primary" : ""}`} onClick={() => save({ telegram: { ...s.telegram, topic_status_emoji: !s.telegram.topic_status_emoji } })}>
                {t("settings.chat.topicemoji", { state: t(s.telegram.topic_status_emoji ? "common.on" : "common.off") })}
              </button>
              <button className={`btn small ${s.telegram.forward_unknown_commands ? "primary" : ""}`} onClick={() => save({ telegram: { ...s.telegram, forward_unknown_commands: !s.telegram.forward_unknown_commands } })}>
                {t("settings.chat.forward", { state: t(s.telegram.forward_unknown_commands ? "common.on" : "common.off") })}
              </button>
            </div>
            <label className="field">{t("settings.chat.stale")}</label>
            <input className="field" type="number" defaultValue={s.telegram.stale_after_seconds} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ telegram: { ...s.telegram, stale_after_seconds: v } }); }} />
            <label className="field">{t("settings.chat.maxfile")}</label>
            <input className="field" type="number" defaultValue={s.telegram.max_inbound_file_mb} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ telegram: { ...s.telegram, max_inbound_file_mb: v } }); }} />
            <label className="field">{t("settings.chat.caption")}</label>
            <input className="field" type="number" defaultValue={s.telegram.photo_caption_wait_seconds} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ telegram: { ...s.telegram, photo_caption_wait_seconds: v } }); }} />
            <label className="field">{t("settings.chat.slowtool")}</label>
            <input className="field" type="number" defaultValue={s.telegram.slow_tool_seconds} onBlur={(e) => { const v = numInput(e.target.value); if (v !== null) save({ telegram: { ...s.telegram, slow_tool_seconds: v } }); }} />
            <label className="field">{t("settings.chat.scheduled")}</label>
            <div className="btnrow" style={{ marginTop: 0 }}>
              {["per_task", "per_run"].map((m) => (
                <button key={m} className={`btn small ${s.scheduler.topic_mode === m ? "primary" : ""}`} onClick={() => save({ scheduler: { ...s.scheduler, topic_mode: m } })}>
                  {t(m === "per_task" ? "settings.chat.pertask" : "settings.chat.perrun")}
                </button>
              ))}
            </div>
          </div>
        );
      case "notifications":
        return <NotificationSettings toast={toast} />;
      case "security":
        return <SecurityTab toast={toast} />;
      case "heartbeat":
        return <HeartbeatTab s={s} toast={toast} />;
      case "about":
        return (
          <div className="card">
            <div className="section-title" style={{ marginTop: 0 }}>{t("settings.about.runtime")}</div>
            {status ? (
              <>
                <div className="kv"><span>{t("settings.about.providers")}</span><b>{status.providers?.join(", ")}</b></div>
                {status.supervisor ? (
                  <>
                    <div className="kv"><span>{t("settings.about.bot")}</span><b className="mono">{String(status.supervisor.bot).slice(0, 10)}</b></div>
                    <div className="kv"><span>{t("settings.about.core")}</span><b className="mono">{String(status.supervisor.core).slice(0, 10)}</b></div>
                    <div className="kv"><span>{t("settings.about.process")}</span><b>{t(status.supervisor.child_running ? "settings.about.running" : "settings.about.stopped")}</b></div>
                  </>
                ) : (
                  <div className="sub">{t("settings.about.nosupervisor")}</div>
                )}
                {status.budget_exceeded && <div className="sub" style={{ color: "var(--bad)" }}>{t("settings.about.budget", { what: String(status.budget_exceeded) })}</div>}
              </>
            ) : (
              <div className="sub">{t("settings.about.nostatus")}</div>
            )}
            {!telegram()?.initData && (
              <>
                <div className="section-title">{t("settings.about.browser")}</div>
                <div className="sub"><a href={pathFor("settings", "appearance")} onClick={(e) => go(e, pathFor("settings", "appearance"))}>{t("settings.about.appearance")}</a></div>
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
              </>
            )}
          </div>
        );
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
