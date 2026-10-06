import { useEffect, useState } from "react";
import { api, Settings } from "../api";
import { t, useLang } from "../i18n";
import { errorText } from "../ui";
import { presetIdFor } from "../models";

export type FreeModel = {
  id: string;
  name?: string;
  provider: string;
  mechanism: "zero_price" | "unverified_promotion";
  context_length?: number;
  max_output_tokens?: number;
  images?: boolean;
  reasoning?: boolean;
  tools_reported: boolean;
  agent_ready: boolean;
  may_train: boolean;
};
export type FreeProvider = {
  id: string;
  name: string;
  base_url: string;
  key_url: string;
  key_required: boolean;
  fresh: boolean;
  models: FreeModel[];
};
export type FreeCatalog = { providers: FreeProvider[]; updated_at: number | null; stale: boolean };

function freePresetId(provider: string, model: string): string {
  const id = presetIdFor(provider, model);
  if (id.length <= 64) return id;
  let hash = 2166136261;
  for (const byte of new TextEncoder().encode(`${provider}/${model}`)) {
    hash = Math.imul(hash ^ byte, 16777619) >>> 0;
  }
  return `${id.slice(0, 55)}-${hash.toString(16).padStart(8, "0")}`;
}

/** One shared catalog in onboarding and Settings; selecting a model never hides its actual endpoint. */
export function FreeModels({ onSaved, toast }: { onSaved: (id: string, settings: Settings) => void; toast: (message: string) => void }) {
  useLang();
  const [catalog, setCatalog] = useState<FreeCatalog | null>(null);
  const [ready, setReady] = useState<string[]>([]);
  const [selected, setSelected] = useState("");
  const [query, setQuery] = useState("");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  async function load(refresh = false) {
    setError("");
    try {
      const [found, onboarding] = await Promise.all([
        api.get<FreeCatalog>(`/api/providers/free-catalog${refresh ? "?refresh=true" : ""}`),
        api.get<{ providers: { id: string; ready: boolean }[] }>("/api/onboarding"),
      ]);
      setCatalog(found);
      setReady(onboarding.providers.filter((p) => p.ready).map((p) => p.id));
      setSelected((old) => found.providers.some((p) => p.id === old) ? old : found.providers[0]?.id ?? "");
    } catch (cause) {
      setError(errorText(cause));
    }
  }

  useEffect(() => { void load(); }, []);
  const provider = catalog?.providers.find((p) => p.id === selected);
  const connected = !!provider && (!provider.key_required || ready.includes(provider.id));
  const shown = provider?.models.filter((m) => `${m.id} ${m.name ?? ""}`.toLowerCase().includes(query.toLowerCase())) ?? [];

  async function connect() {
    if (!provider || !key.trim()) return;
    setBusy("connect");
    try {
      await api.put<Settings>(`/api/providers/${encodeURIComponent(provider.id)}`, {
        kind: provider.id === "openrouter" ? "openrouter" : "openai_compat",
        name: provider.name, base_url: provider.base_url, api_key: key.trim(),
      });
      setKey("");
      await load();
      toast(t("free.connected"));
    } catch (cause) {
      toast(errorText(cause));
    } finally {
      setBusy("");
    }
  }

  async function install(model: FreeModel) {
    if (!provider || !connected || !provider.fresh || model.mechanism !== "zero_price") return;
    setBusy(model.id);
    try {
      const probe = await api.post<{ agent_ready: boolean }>("/api/providers/free-catalog/probe", { provider: provider.id, model: model.id });
      if (!probe.agent_ready) {
        toast(t("free.probe.failed"));
        return;
      }
      const current = await api.get<Settings>("/api/settings");
      if (!current.providers[provider.id]) {
        await api.put<Settings>(`/api/providers/${encodeURIComponent(provider.id)}`, {
          kind: "openai_compat", name: provider.name, base_url: provider.base_url,
        });
      }
      const id = freePresetId(provider.id, model.id);
      const next = await api.put<Settings>(`/api/presets/${encodeURIComponent(id)}`, {
        provider: provider.id, model: model.id, label: model.name ?? model.id,
        free_only: true, thinking: model.reasoning ?? false, images: model.images ?? false,
        context_window: Math.max(8000, Math.min(model.context_length ?? 128000, 400000)),
        max_output_tokens: Math.max(1024, Math.min(model.max_output_tokens ?? 8192, 64000)),
      });
      onSaved(id, next);
      toast(t("free.added"));
      await load();
    } catch (cause) {
      toast(errorText(cause));
    } finally {
      setBusy("");
    }
  }

  return <div className="free-catalog">
    <div className="sub">{t("free.intro")}</div>
    <div className="btnrow" style={{ marginTop: 12 }}>
      <button className="btn small" disabled={!!busy} onClick={() => void load(true)}>{t("free.refresh")}</button>
      {catalog?.updated_at && <span className="sub">{t("free.updated")} {new Date(catalog.updated_at * 1000).toLocaleString()}</span>}
      {catalog?.stale && <span className="pill waiting">{t("free.stale")}</span>}
    </div>
    {error && <div className="empty calm" role="status">{error}</div>}
    {!catalog && !error && <div className="empty calm">{t("common.loading")}</div>}
    {catalog && <>
      <div className="section-title" style={{ marginTop: 20 }}>{t("free.providers")}</div>
      <div className="pickgrid">
        {catalog.providers.map((p) => <button key={p.id} className={`pick ${selected === p.id ? "on" : ""}`} aria-pressed={selected === p.id} onClick={() => { setSelected(p.id); setQuery(""); setKey(""); }}>
          <span className="pick-top"><b>{p.name}</b><span className="pill">{p.models.length}</span></span>
          <span className="sub">{p.key_required ? t("free.key.needed") : t("free.key.none")}</span>
        </button>)}
      </div>
      {provider && <>
        <div className="section-title" style={{ marginTop: 22 }}>{provider.name} → {t("free.models")}</div>
        {!connected && <div className="card" style={{ marginTop: 12 }}>
          <label className="mfield"><span>{t("free.key.label")}</span><input className="field" type="password" autoComplete="new-password" value={key} onChange={(e) => setKey(e.target.value)} /></label>
          <div className="sub">{t("free.key.get")} <a href={provider.key_url} target="_blank" rel="noopener noreferrer">{provider.name} ↗</a></div>
          <div className="btnrow" style={{ marginTop: 12 }}><button className="btn primary" disabled={!key.trim() || !!busy} onClick={() => void connect()}>{t("free.connect")}</button></div>
        </div>}
        <input className="field" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("free.search")} aria-label={t("free.search")} style={{ marginTop: 12 }} />
        <div className="mlist" style={{ marginTop: 12 }}>
          {shown.map((m) => <div className="mrow" key={m.id}>
            <div className="mline noradio"><div className="mmain"><span className="mtitle">{m.name ?? m.id}</span><span className="mmeta">{m.id}</span></div>
              <div className="mtags"><span className={`pill ${m.mechanism === "zero_price" ? "idle" : "waiting"}`}>{t(m.mechanism === "zero_price" ? "free.zero" : "free.promotion")}</span>{m.may_train && <span className="pill waiting">{t("free.training")}</span>}</div>
              <button className="btn small" disabled={!connected || !provider.fresh || m.mechanism !== "zero_price" || !!busy} onClick={() => void install(m)}>{busy === m.id ? t("free.testing") : t("free.add")}</button>
            </div>
            <div className="sub" style={{ padding: "0 14px 12px" }}>{t(m.tools_reported ? "free.tools.reported" : "free.tools.unknown")}</div>
          </div>)}
          {shown.length === 0 && <div className="empty calm">{t("free.empty")}</div>}
        </div>
      </>}
    </>}
  </div>;
}
