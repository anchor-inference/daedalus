import type React from "react";
import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Icon, IconName } from "../icons";
import { navigate, pathFor } from "../router";
import { go, screenTitle, useMedia } from "../ui/index";
import { api, telegram, HeartbeatStatus, SearchBackendInfo, SearchCheck, Settings } from "../api";
import { errorText } from "../ui";
import { TwoFactorCard } from "../twofactor";
import { timeAgo } from "../ui/components";
import { shortDateTime } from "../format";
import { useMoreBelow } from "../edgefade";
import { SpeechModels } from "./Speech";
import { TtsVoices } from "./Voices";
import { VoiceSettings } from "./Companion";
import { ComponentsTab } from "./Components";
import { DependenciesTab } from "./Dependencies";
import { PromptChange } from "./PromptChange";
import { AddModel } from "./AddModel";
import { QuickModelsPicker, SubagentModelsPicker } from "../subagentmodels";
import { ModelPicker, modelItems } from "../modelitems";
import { FreeModels } from "./FreeModels";
import { orchestratorPreset } from "../models";
import { mainPreset } from "../main/model";
import { Sheet } from "../ui/dialogs";
import { lang, plural, t } from "../i18n";
import { Select, Segmented, SelectionList, Switch } from "../ui/index";
import { Button, confirmDialog } from "../ui/primitives";
import { type Changed, NumInput, NumRow, Row, SetBlock, SetFoot, SetGroup, TextBlock } from "../settingsrow";
import { ModelChoice } from "../modelchoice";
import { DesktopPanel } from "./DesktopSettings";
import { DevicesPanel } from "./DevicesSettings";
import { ADDRESS, PublicAddress, PublicAddressPanel } from "../publicaddress";
import { AppearancePanel } from "./Appearance";
import { NavigationSettings } from "./NavigationSettings";
import { MemorySettings } from "./MemorySettings";
import { HostMemoryRow } from "./HostMemory";
import { modeHome, storedMode } from "../mode";
import { useQuery } from "../store";
import { Capabilities, componentsNeedAttention } from "../capabilities";
import { NotificationSettings } from "./NotificationSettings";
import { ActionRules, DEFAULT_RULES } from "../actionrules";
import { EnvironmentsTab } from "./Environments";
import { BrowserSettingsTab } from "./BrowserSettings";
import { ToolGroupsSettings } from "../toolgroupsview";
import { CompactionModelSelect } from "../compactionmodel";
import { AsrSettingsCard } from "./AsrSettings";
import { McpServers } from "./McpServers";
import { AutopilotSettings, UserStyleCard } from "./AutopilotSettings";
import { TranslationSettings } from "./TranslationSettings";
import { DesktopAppCard } from "../updatedialog";
import { SecretsCard } from "../secrets";
import { KeyproxyCard, SearchKeys } from "../keyproxy";
import { SubscriptionAccountsCard, useSubscriptionAccounts, type SubscriptionAccounts } from "../accounts";
import { revealSetting, settingTarget, clearSettingTarget } from "../settinglink";
import { INDEX_SECTIONS, SECTION_GROUPS, fieldPath, matchFields, sectionOfAnchor, type SectionGroup } from "./settingsindex";
import { HitBody, hitCount, moveInList, usePageKeys } from "./settingssearch";
import { AddProvider, ModelsSection } from "./ModelsSection";
import { ProjectApprovals } from "./ProjectApprovals";

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
    <SetGroup title={t("settings.rules.title")} help={<>{t("settings.rules.sub")} {t(rules ? "settings.rules.custom" : "settings.rules.default")}</>} data-setting="prompt.rules">
      {/* The eleventh line shows through the field's bottom padding sliced in half; the fade over
          it (while more waits below) makes that read as "scroll for more" rather than broken text. */}
      <SetBlock>
        <div className="rules-editor">
          <textarea ref={field} className="field rules-editor-field" rows={10} aria-label={t("settings.rules.title")} value={text} onChange={(e) => (setText(e.target.value), setDirty(true))} />
        </div>
      </SetBlock>
      <SetFoot>
        <Button variant="primary" disabled={!dirty} onClick={() => (onSave(text.trim() === fallback.trim() ? "" : text), setDirty(false))}>
          {t("common.save")}
        </Button>
        <Button onClick={() => (setText(fallback), setDirty(true))}>
          {t("settings.rules.reset")}
        </Button>
      </SetFoot>
    </SetGroup>
  );
}

/** What a row needs to say it was changed. The default is the value the page falls back to when the
 *  host sends none — the one shipped value the app can see — so only rows with such a fallback use it. */
function shipped<T>(value: T, fallback: T, reset: () => void, shown = String(fallback)): Changed {
  return value === fallback ? {} : { changed: true, defaultText: shown, onReset: reset };
}

/** A preset as the model picker names it: its label, or provider/model when it has none. */
function presetName(s: Settings, id: string): string {
  const p = s.presets?.[id];
  return p ? p.label || `${p.provider}/${p.model}` : id;
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
  async function reset() {
    if (!(await confirmDialog({ title: t("settings.limits.reset.title"), body: t("settings.limits.reset.body"), action: t("settings.limits.reset"), danger: true }))) return;
    await api.post("/api/limits/reset-total");
    load();
  }
  return (
    <>
      {spendState === "failed" && <SetBlock><div className="result-warning" role="status">{t("settings.limits.balance.failed")} <Button size="sm" variant="quiet" onClick={load}>{t("common.retry")}</Button></div></SetBlock>}
      {/* Only with a cap: "of $0, no cap" has no bar to draw and no share to read. */}
      {cap > 0 && spend && (
        <SetBlock>
          <div className="spend-meter" data-level={engaged >= cap ? "bad" : engaged >= cap * 0.8 ? "warn" : "ok"}>
            <div className="spend-meter-line">
              <b>{held > 0 ? t("settings.limits.meter.held", { spent: usd(spent), held: usd(held), cap: usd(cap) })
                : t("settings.limits.meter", { spent: usd(spent), cap: usd(cap) })}</b>
              <span className="sub">{t("settings.limits.since", { when: since })}</span>
            </div>
            <span className="spend-meter-track" aria-hidden><span style={{ width: `${Math.min(100, (engaged / cap) * 100)}%` }} /></span>
          </div>
        </SetBlock>
      )}
      {spend && (held > 0 || spend.total.unmetered > 0) && <SetBlock><details className="sheet-section"><summary>{t("settings.limits.balance.details")}</summary>
        {held > 0 && <p className="sub">{t("settings.limits.balance.held", { amount: usd(held), n: spend.total.reserved_count })}</p>}
        {spend.total.uncertain_usd > 0 && <p className="sub">{t("settings.limits.balance.uncertain", { amount: usd(spend.total.uncertain_usd), n: spend.total.uncertain_count })}</p>}
        {spend.total.unmetered > 0 && <p className="sub">{t("settings.limits.balance.unpriced", { n: spend.total.unmetered })}</p>}
        {held > 0 && <p className="sub">{t("settings.limits.balance.reset")}</p>}
      </details></SetBlock>}
      <Row data-setting="limits.usd_total" title={t("settings.limits.total")} htmlFor="limits-total" desc={spend ? t("settings.limits.total.sub", { sum: usd(spent) }) : t(spendState === "loading" ? "settings.limits.balance.loading" : "settings.limits.balance.unknown")} stack>
        <NumInput id="limits-total" label={t("settings.limits.total")} value={cap} min={0} step={1} unit="USD" onSave={(v) => save({ limits: { usd_total: v } })} />
      </Row>
      {providers.map((pid) => (
        <Row key={pid} title={t("settings.limits.provider", { id: pid })} desc={spend ? spend.per_provider[pid]?.reserved_usd > 0
          ? t("settings.limits.provider.held", { spent: usd(spend.per_provider[pid].spent_usd), held: usd(spend.per_provider[pid].reserved_usd) })
          : t("settings.limits.spent", { sum: usd(spend.per_provider[pid]?.spent_usd ?? 0) })
          : t(spendState === "loading" ? "settings.limits.balance.loading" : "settings.limits.balance.unknown")} stack data-provider={pid} data-setting={`limits.usd_total_per_provider.${pid}`}>
          <NumInput label={t("settings.limits.provider", { id: pid })} value={caps[pid] ?? 0} min={0} step={1} unit="USD" onSave={(v) => save({ limits: { usd_total_per_provider: { [pid]: v } } })} />
        </Row>
      ))}
      {/* Said, not left blank: an empty space where the providers go read as a list that failed to load. */}
      {providers.length === 0 && <Row title={t("settings.limits.perprovider")} desc={t("settings.limits.noproviders")} />}
      <Row title={t("settings.limits.counting")} data-setting="limits.total_since" desc={t("settings.limits.since", { when: since })}>
        <Button size="sm" onClick={() => void reset()}>{t("settings.limits.reset")}</Button>
      </Row>
    </>
  );}

function SearchBlock({ s, save, toast, reload }: { s: Settings; save: (patch: any) => Promise<void>; toast: (t: string) => void; reload: () => void }) {
  const search = s.tools.web.search;
  const backends = s.search_backends ?? [];
  const [check, setCheck] = useState<SearchCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState("");
  const saveSearch = (patch: any) => save({ tools: { web: { search: patch } } });
  const info = (id: string) => backends.find((b) => b.id === id);
  const usable = (b: SearchBackendInfo) => !b.needs_key || b.available !== false;
  const optionLabel = (b: SearchBackendInfo) => `${b.label}${b.needs_key ? (b.available === false ? t("settings.search.nokey") : b.available == null ? t(b.proxy === "refused" ? "settings.search.proxyrefused" : "settings.search.noproxy") : "") : ""}`;
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
    <>
      {/* One group a concern: the choice of backend, each engine in use, the keys, the check. As one
          card the engines' fields ran on from the rows above them with only a heading between. */}
      <SetGroup title={t("settings.search.title")} help={t("settings.search.sub")}>
        <Row title={t("settings.search.backend")} htmlFor="search-backend" data-setting="tools.web.search.backend">
          <Select
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
        <SetBlock data-setting="tools.web.search.fallback">
          <span className="field-label">{t("settings.search.fallbacks")}</span>
          <span className="set-hint">{t("settings.search.fallbacks.sub")}</span>
          <SelectionList
            id="search-fallbacks"
            label={t("settings.search.fallbacks")}
            emptyChosen={t("settings.search.fallbacks.none")}
            chosenLabel={t("settings.search.fallbacks.chosen")}
            ordered
            value={fallbackIds.filter((id) => id !== search.backend)}
            onChange={(fallback) => saveSearch({ fallback })}
            items={backends.filter((b) => b.id !== search.backend).map((b) => ({ id: b.id, label: b.label, detail: optionLabel(b).slice(b.label.length).replace(/^ — /, "") || undefined, facts: [t(b.needs_key ? "settings.search.keyed" : "settings.search.keyless")], disabled: !usable(b) }))}
          />
        </SetBlock>
        <NumRow setting="tools.web.search.results" title={t("settings.search.results")} value={search.results} min={1} onSave={(v) => saveSearch({ results: v })} />
        <NumRow setting="tools.web.search.timeout_seconds" title={t("settings.search.timeout")} unit={t("settings.unit.seconds")} value={search.timeout_seconds} min={1} onSave={(v) => saveSearch({ timeout_seconds: v })} />
      </SetGroup>
      {search.backend === "searxng" || fallbackIds.includes("searxng") ? (
        <SetGroup title="SearXNG">
          <TextBlock setting="tools.web.search.searxng.url" label={t("settings.search.url")} value={search.searxng.url} placeholder="http://127.0.0.1:8080" onSave={(v) => saveSearch({ searxng: { url: v } })} />
          <TextBlock setting="tools.web.search.searxng.engines" label={t("settings.search.engines")} value={search.searxng.engines} placeholder="google,duckduckgo,bing" onSave={(v) => saveSearch({ searxng: { engines: v } })} />
          <TextBlock setting="tools.web.search.searxng.categories" label={t("settings.search.categories")} value={search.searxng.categories} placeholder="general" onSave={(v) => saveSearch({ searxng: { categories: v } })} />
          <Row title={t("settings.search.safe")} data-setting="tools.web.search.searxng.safesearch">
            <Segmented label={t("settings.search.safe")} value={String(Math.min(2, Math.max(0, search.searxng.safesearch)))} onChange={(v) => saveSearch({ searxng: { safesearch: Number(v) } })} options={["0", "1", "2"].map((id) => ({ id, label: t(`settings.search.safe.${id}`) }))} />
          </Row>
        </SetGroup>
      ) : null}
      {search.backend === "duckduckgo" || fallbackIds.includes("duckduckgo") ? (
        <SetGroup title="DuckDuckGo">
          <TextBlock setting="tools.web.search.duckduckgo.url" label={t("settings.search.html")} value={search.duckduckgo.url} onSave={(v) => saveSearch({ duckduckgo: { url: v } })} />
          <TextBlock setting="tools.web.search.duckduckgo.region" label={t("settings.search.region")} value={search.duckduckgo.region} placeholder="wt-wt, ru-ru, us-en" onSave={(v) => saveSearch({ duckduckgo: { region: v } })} />
        </SetGroup>
      ) : null}
      {search.backend === "serper" || fallbackIds.includes("serper") ? (
        <SetGroup title="Serper (Google)">
          <TextBlock setting="tools.web.search.serper.gl" label={t("settings.search.country")} value={search.serper.gl} placeholder="ru, us" onSave={(v) => saveSearch({ serper: { gl: v } })} />
          <TextBlock setting="tools.web.search.serper.hl" label={t("settings.search.language")} value={search.serper.hl} placeholder="ru, en" onSave={(v) => saveSearch({ serper: { hl: v } })} />
        </SetGroup>
      ) : null}
      {search.backend === "keenable" || fallbackIds.includes("keenable") ? (
        <SetGroup title="Keenable">
          <NumRow setting="tools.web.search.keenable.snippet_max_length" title={t("settings.search.snippet")} unit={t("settings.unit.chars")} value={search.keenable.snippet_max_length} min={180} step={60} onSave={(v) => saveSearch({ keenable: { snippet_max_length: v } })} />
        </SetGroup>
      ) : null}
      {search.backend === "tavily" || fallbackIds.includes("tavily") ? (
        <SetGroup title="Tavily">
          <Row title={t("settings.search.depth")} data-setting="tools.web.search.tavily.depth">
            <Select label={t("settings.search.depth")} value={search.tavily.depth} onChange={(depth) => saveSearch({ tavily: { depth } })} options={["basic", "advanced", "fast", "ultra-fast"].map((d) => ({ id: d, label: d }))} />
          </Row>
        </SetGroup>
      ) : null}
      {search.backend === "exa" || fallbackIds.includes("exa") ? (
        <SetGroup title="Exa">
          <Row title={t("settings.search.type")} data-setting="tools.web.search.exa.type">
            <Select label={t("settings.search.type")} value={search.exa.type} onChange={(type) => saveSearch({ exa: { type } })} options={["auto", "instant", "fast", "deep"].map((d) => ({ id: d, label: d }))} />
          </Row>
        </SetGroup>
      ) : null}
      <SearchKeys toast={toast} onChanged={reload} />
      <SetGroup>
        <SetFoot className="search-check">
          <button className="btn small primary" disabled={checking} onClick={() => runCheck("")}>{t(checking ? "settings.search.checking" : "settings.search.check")}</button>
          <button className="btn small" disabled={checking || !info(search.backend)} onClick={() => runCheck(search.backend)}>{t("settings.search.checkone", { name: info(search.backend)?.label ?? search.backend })}</button>
        </SetFoot>
        {checkError && <SetBlock><span className="set-hint search-bad">{checkError}</span></SetBlock>}
        {check && (
          <SetBlock className="search-result">
            {check.attempts.map((a) => (
              <span key={a.backend} className="set-hint">
                {a.error ? t("settings.search.attempt.error", { name: a.backend, error: a.error, ms: a.ms }) : t("settings.search.attempt.ok", { name: a.backend, n: a.hits, ms: a.ms })}
              </span>
            ))}
            {check.hits.slice(0, 3).map((h) => (
              <span key={h.url} className="search-hit">
                <span>{h.title}{h.source ? ` · ${h.source}` : ""}</span>
                <span className="set-hint search-hit-url">{h.url}</span>
              </span>
            ))}
          </SetBlock>
        )}
      </SetGroup>
    </>
  );
}

/** Tools: the tool groups first — the one card here that changes what every request costs — then what
 *  the tools that read a page or run a command may do. Web search and the MCP servers are sections of
 *  their own, each a whole card of choices. */
function ToolsTab({ s, save, toast, onSettings }: { s: Settings; save: (patch: any) => Promise<void>; toast: (t: string) => void; onSettings: (next: Settings) => void }) {
  const web = s.tools.web;
  return (
    <>
      <ToolGroupsSettings toast={toast} revision={s.revision} onSettings={onSettings} />

      <SetGroup title={t("settings.web.title")}>
        <NumRow setting="tools.web.fetch_timeout_seconds" title={t("settings.web.timeout")} unit={t("settings.unit.seconds")} value={web.fetch_timeout_seconds} min={1} onSave={(v) => save({ tools: { web: { fetch_timeout_seconds: v } } })} />
        <NumRow setting="tools.web.fetch_max_chars" title={t("settings.web.maxchars")} unit={t("settings.unit.chars")} value={web.fetch_max_chars} min={1000} step={1000} onSave={(v) => save({ tools: { web: { fetch_max_chars: v } } })} />
        <TextBlock setting="tools.web.proxy" label={t("settings.web.proxy")} value={web.proxy} placeholder="socks5://127.0.0.1:1080" hint={t("settings.web.proxy.hint")} onSave={(v) => save({ tools: { web: { proxy: v } } })} />
        <TextBlock setting="tools.web.user_agent" label={t("settings.web.ua")} value={web.user_agent} onSave={(v) => save({ tools: { web: { user_agent: v } } })} />
      </SetGroup>

      <SetGroup title={t("settings.exec.title")}>
        <NumRow setting="limits.tool_timeout_seconds" title={t("settings.exec.timeout")} desc={t("settings.exec.timeout.hint")} unit={t("settings.unit.seconds")} value={s.limits.tool_timeout_seconds} min={10} step={30} onSave={(v) => save({ limits: { tool_timeout_seconds: v } })} />
        <NumRow setting="tools.exec.background_after_seconds" title={t("settings.exec.background")} desc={t("settings.exec.background.hint")} unit={t("settings.unit.seconds")} value={s.tools.exec.background_after_seconds} min={1} step={30} onSave={(v) => save({ tools: { exec: { background_after_seconds: v } } })} />
        <NumRow setting="tools.exec.max_output_chars" title={t("settings.exec.maxchars")} desc={t("settings.exec.maxchars.hint")} unit={t("settings.unit.chars")} value={s.tools.exec.max_output_chars} min={2000} step={5000} onSave={(v) => save({ tools: { exec: { max_output_chars: v } } })} />
      </SetGroup>
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
    } catch (e) {
      toast((e as Error).message);
    }
  }

  async function runNow() {
    try {
      await api.post("/api/heartbeat/run");
      load();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  return (
    <>
      <SetGroup title={t("settings.heartbeat.title")} help={t("settings.heartbeat.sub")}>
        {/* The state is the row's description: apart, an "on" chip, a Run now and a status line made
            three rows that read as three actions. Run now is under the card, with the other buttons. */}
        <Row
          data-setting="heartbeat.enabled"
          title={t("settings.heartbeat.enabled")}
          desc={
            <>
              {hb.armed ? t("settings.heartbeat.armed") : hb.enabled ? t("settings.heartbeat.emptyfile") : t("common.off")} · {t("settings.heartbeat.today", { done: hb.runs_today, max: hb.max_runs_per_day })} ·{" "}
              {t("settings.heartbeat.last", { t: hb.last_run ? timeAgo(hb.last_run) : t("common.never") })}
              {hb.running ? t("settings.heartbeat.running") : ""}
            </>
          }
        >
          <Switch checked={hb.enabled} onChange={(enabled) => put({ enabled })} label={t("settings.heartbeat.enabled")} />
        </Row>
        <NumRow id="heartbeat-interval" setting="heartbeat.interval_minutes" title={t("settings.heartbeat.interval")} unit={t("settings.unit.minutes")} value={hb.interval_minutes} min={1} onSave={(v) => put({ interval_minutes: v })} />
        <Row title={t("settings.heartbeat.hours")} desc={t("settings.heartbeat.hours.sub")} htmlFor="heartbeat-hours" stack data-setting="heartbeat.active_hours">
          <input id="heartbeat-hours" className="field settings-inline-field" defaultValue={hb.active_hours} placeholder="09:00-21:00" onBlur={(e) => e.target.value !== hb.active_hours && put({ active_hours: e.target.value })} />
        </Row>
        <NumRow id="heartbeat-max" setting="heartbeat.max_runs_per_day" title={t("settings.heartbeat.max")} value={hb.max_runs_per_day} min={0} onSave={(v) => put({ max_runs_per_day: v })} />
        <Row title={t("settings.heartbeat.preset")} stack data-setting="heartbeat.preset">
          <ModelChoice id="heartbeat-preset" label={t("settings.heartbeat.preset")} catalogue={s} value={hb.preset} onChange={(preset) => put({ preset })} defaultOption={{ label: t("settings.heartbeat.default") }} />
        </Row>
        <SetFoot>
          <Button onClick={runNow} disabled={!text.trim() || hb.running}>{t("common.runnow")}</Button>
        </SetFoot>
      </SetGroup>
      <SetGroup title="HEARTBEAT.md" data-setting="heartbeat.text">
        <SetBlock>
          <textarea
            className="field"
            rows={12}
            aria-label="HEARTBEAT.md"
            value={text}
            placeholder={hb.template}
            onChange={(e) => {
              setText(e.target.value);
              setDirty(true);
              dirtyRef.current = true;
            }}
          />
        </SetBlock>
        <SetFoot>
          <Button variant="primary" disabled={!dirty} onClick={() => put({ text })}>
            {t("common.save")}
          </Button>
          <Button size="sm" onClick={() => { setText(hb.template ?? ""); setDirty(true); }}>
            {t("settings.heartbeat.template")}
          </Button>
          <Button size="sm" onClick={() => { setText(""); setDirty(true); }}>
            {t("settings.heartbeat.clear")}
          </Button>
        </SetFoot>
      </SetGroup>
    </>
  );
}

/** Security: each concern its own group — two-factor sign-in, the operator's secrets, signing out everywhere. */
function SecurityTab({ toast }: { toast: (t: string) => void }) {
  // Nothing on this screen changes when the other sessions end, so the button keeps the answer.
  const [signedOut, setSignedOut] = useState(false);
  const signOutEverywhere = async () => {
    if (!(await confirmDialog({ title: t("settings.security.signoutall.title"), body: t("settings.security.signoutall.body"), action: t("settings.security.signoutall"), danger: true }))) return;
    try {
      await api.post("/api/auth/sessions/revoke");
      setSignedOut(true);
    } catch (e) {
      toast(errorText(e));
    }
  };

  return (
    <>
      <TwoFactorCard toast={toast} />
      <SecretsCard toast={toast} />
      <SetGroup title={t("settings.security.sessions")}>
        <Row title={t("settings.security.signoutall")} desc={t("settings.security.signoutall.sub")} data-setting="security.signout">
          <Button size="sm" onClick={() => void signOutEverywhere()}>{signedOut && <Icon name="check" size={13} />}{t("settings.security.signoutall.action")}</Button>
        </Row>
      </SetGroup>
    </>
  );
}

type Section =
  | "appearance" | "text" | "notifications" | "navigation" | "desktop"
  | "voice" | "dictation" | "speech-models"
  | "models" | "tasks" | "accounts" | "keyproxy" | "free-models" | "budget"
  | "chat" | "rules" | "approvals" | "limits" | "context" | "memory" | "memory-upkeep" | "translation" | "schedules"
  | "tools" | "search" | "browser" | "browser-safety" | "environments"
  | "telegram" | "mcp" | "devices" | "address"
  | "security" | "components" | "dependencies" | "about";
/** The sections, grouped the way the page lists them. Their ids, groups and icons are read from the
 *  settings index beside this file — the same table the agent's documentation search answers from —
 *  so a section renamed there is renamed in the links the agent gives out as well. The words on the
 *  page come from the translation table, not from here. */
const GROUPS = SECTION_GROUPS as { id: SectionGroup; sections: { id: Section; icon: IconName }[] }[];
const SECTIONS = GROUPS.flatMap((group) => group.sections);

const sectionLabel = (id: Section) => t(`settings.sec.${id}`);

/** Sections that were pages of their own and are now part of another: an address written down, sent
 *  in a notification or bookmarked still lands on the setting, scrolled to the part it named. The
 *  models page's tabs are sections of their own now, so its old `?tab=` addresses are here too; the
 *  providers became part of the models' section, which draws each model inside its provider. A row
 *  that moved needs no entry: an address naming it goes to the section the index says holds it. */
const MOVED: Record<string, { to: Section; anchor?: string }> = {
  terminals: { to: "environments", anchor: "terminal-sessions" },
  heartbeat: { to: "schedules" },
  providers: { to: "models" },
  "models?tab=providers": { to: "models" },
  "models?tab=free": { to: "free-models" },
};

/** Where an address naming `section`, perhaps with a tab and a row, belongs now; nothing when it is right. */
function movedTo(section: string | null | undefined, search: string, row: string | null): { to: Section; anchor?: string } | undefined {
  if (!section) return undefined;
  const home = row ? sectionOfAnchor(row) : null;
  if (home && home !== section && SECTIONS.some((x) => x.id === home)) return { to: home as Section };
  const tab = new URLSearchParams(search).get("tab");
  return (tab ? MOVED[`${section}?tab=${tab}`] : undefined) ?? MOVED[section];
}

/** How many subscriptions hold an account added but not signed in yet: the one number worth a badge in
 *  the list of sections, since that account is skipped until it is. */
function signInsWaiting(accounts: SubscriptionAccounts | null | undefined): number {
  return (accounts?.upstreams ?? []).filter((u) => u.sign_in === "terminal" && u.accounts.some((a) => !a.configured)).length;
}

/** Settings: a page of its own on a desktop — the list of sections where the sidebar was, the open
 *  section in a centred column — and a full-screen stack of two levels on a phone. `onClose` is the
 *  shell's way back to where the operator was. */
export function SettingsScreen({ toast, section, onClose }: { toast: (t: string) => void; section?: string | null; onClose?: () => void }) {
  const caps = useQuery<Capabilities>("/api/capabilities", { staleMs: 20000 });
  const [s, setS] = useState<Settings | null>(null);
  // `?add=1` on the models section opens the add sheet straight away: the composer's model picker
  // sends the operator here to add a model, and landing on the list would leave one more click to find.
  const [adding, setAdding] = useState(() => (section === "models" || section === "providers") && new URLSearchParams(window.location.search).get("add") === "1");
  const [addingProvider, setAddingProvider] = useState(false);
  const [status, setStatus] = useState<any>(null);
  // Names and sign-in state: a model's account choice, a provider's status, the badge on Accounts.
  const { data: subscriptionAccounts } = useSubscriptionAccounts(false);
  const wide = useMedia("(min-width: 1024px)");
  const [query, setQuery] = useState("");
  const page = useRef<HTMLDivElement>(null);
  const searchField = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLElement>(null);
  const close = onClose ?? (() => navigate(modeHome(storedMode(), wide)));
  usePageKeys({ page, search: searchField, query, clear: () => setQuery(""), onClose: close });
  const target = settingTarget();
  const moved = movedTo(section, window.location.search, target);
  const movedTarget = moved?.to;
  const movedAnchor = moved?.anchor;
  // The public address is the native installation's alone, and whether this is one only the host knows.
  const native = useQuery<PublicAddress>(ADDRESS, { staleMs: 20000 }).data?.native;
  const named: Section | null = moved ? moved.to : SECTIONS.some((x) => x.id === section) ? (section as Section) : null;
  const current = named === "address" && native === false ? null : named;
  useEffect(() => {
    if (!movedTarget) return;
    // The tab is spent once it has chosen the section; the rest of the query (`add=1`) stays.
    const params = new URLSearchParams(window.location.search);
    params.delete("tab");
    const rest = params.toString();
    navigate(pathFor("settings", movedTarget) + (rest ? `?${rest}` : "") + window.location.hash, { replace: true });
  }, [movedTarget]);
  // The part an old address named is scrolled to once it has drawn, which for a card that reads its
  // own listing is after that answers; a few tries rather than a guess at how long that takes.
  const [anchor, setAnchor] = useState<string | null>(null);
  useEffect(() => {
    if (movedAnchor) setAnchor(movedAnchor);
  }, [movedAnchor]);
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
  // The list of sections is taller than the window: the open one is brought into it, or a link to
  // About opened a page whose own entry sat below the fold of the list beside it.
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(".settings-link.active")?.scrollIntoView({ block: "nearest" });
  }, [shown]);
  // A section's column starts at its top: the page keeps one scroller, and the next section opened
  // in it began where the last one had been read to.
  const main = useRef<HTMLDivElement>(null);
  useEffect(() => {
    main.current?.scrollTo({ top: 0 });
  }, [shown]);
  // A link to one setting (`#<key>`, from an error's button or a link in the agent's answer): the tab
  // that holds it is opened, the row is scrolled to once it has drawn and blinks, and the name leaves
  // the address so a reload does not blink it again and the next press of the same link is a change
  // the page sees.
  useEffect(() => {
    // Not while the address is on its way to the section that holds the row: it is not on this one.
    if (!target || !shown || movedTarget) return;
    let cancelled = false;
    void revealSetting(target, { root: page.current ?? document }).then(() => {
      if (!cancelled) clearSettingTarget();
    });
    return () => {
      cancelled = true;
    };
  }, [target, shown, movedTarget]);
  useEffect(() => {
    api.get<Settings>("/api/settings").then(setS).catch((e) => toast((e as Error).message));
    api.get("/api/status").then(setStatus).catch(() => setStatus(null));
  }, [toast]);

  /** A settings body the host answered with, keeping the list of reachable providers when it left it out. */
  const take = (next: Settings) => setS((now) => ({ ...next, providers_available: next.providers_available ?? (now?.providers_available ?? []) }));

  // After a key proxy value changes, provider readiness and the search backends are read again.
  function rereadSettings() {
    api.get<Settings>("/api/settings").then(take).catch(() => undefined);
  }

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
      take(await api.put<Settings>("/api/settings", { ...patch, base_revision: s.revision }));
    } catch (e) {
      toast((e as Error).message);
    }
  }
  async function addProvider(id: string, patch: Record<string, unknown>): Promise<Settings | undefined> {
    try {
      const next = await api.put<Settings>(`/api/providers/${encodeURIComponent(id)}`, patch);
      take(next);
      return next;
    } catch (e) {
      toast((e as Error).message);
      return undefined;
    }
  }

  const needle = query.trim();
  // The desktop application's own settings are listed only in its window, and the public address
  // only by an installation that says it is native, which a browser on a phone can be looking at too.
  const visible = (id: Section) => (id !== "desktop" || !!window.daedalus?.desk) && (id !== "address" || !!native);
  const fields = needle ? matchFields(needle).filter((field) => visible(field.section as Section)) : [];
  const fieldsOf = (id: Section) => fields.filter((field) => field.section === id);
  const titled = (id: Section) => {
    const entry = INDEX_SECTIONS.find((item) => item.id === id);
    const words = [sectionLabel(id), t(`settings.sec.${id}.hint`), ...(entry ? [entry.title.en, entry.title.ru, ...entry.synonyms] : [])];
    return words.join("\n").toLowerCase().includes(needle.toLowerCase());
  };
  const hits = needle ? SECTIONS.filter((sec) => visible(sec.id) && (titled(sec.id) || fieldsOf(sec.id).length > 0)) : [];
  const about = (id: Section) => INDEX_SECTIONS.find((item) => item.id === id)?.about[lang()] ?? t(`settings.sec.${id}.hint`);
  const leave = (e: React.MouseEvent, href: string) => {
    setQuery("");
    go(e, href);
  };
  // Enter in the search goes where the first result does: the first row found, else the first section.
  const first = fields[0] ? fieldPath(fields[0]) : hits[0] ? pathFor("settings", hits[0].id) : null;
  const waiting = signInsWaiting(subscriptionAccounts);

  const link = (sec: { id: Section; icon: IconName }) => {
    const href = pathFor("settings", sec.id);
    const here = !needle && shown === sec.id;
    return (
      <a key={sec.id} href={href} className={`settings-link ${here ? "active" : ""}`} aria-current={here ? "page" : undefined} data-section={sec.id} onClick={(e) => leave(e, href)}>
        <Icon name={sec.icon} size={wide ? 16 : 18} />
        <span className="settings-link-text">
          <b>{sectionLabel(sec.id)}</b>
          <span className="sub">{t(`settings.sec.${sec.id}.hint`)}</span>
        </span>
        {/* A mark, not a count: the one thing worth interrupting a reader for is a feature they have
            already configured whose runtime is not installed. */}
        {sec.id === "components" && componentsNeedAttention(caps.data) && <span className="tab-badge dot settings-mark" role="img" aria-label={t("comp.state.missing")} />}
        {/* Subscriptions that wait for a sign-in: every model behind them is skipped until then. */}
        {sec.id === "accounts" && waiting > 0 && <span className="settings-count-badge" role="img" aria-label={plural("settings.accounts.waiting", waiting)}>{waiting}</span>}
        <span className="chev" aria-hidden><Icon name="chevron" size={14} /></span>
      </a>
    );
  };
  const search = (
    <div className="settings-search-box">
      <Icon name="search" size={16} />
      <input
        ref={searchField}
        className="field settings-search"
        value={query}
        placeholder={t("settings.find.placeholder")}
        aria-label={t("settings.find.placeholder")}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && first) {
            e.preventDefault();
            setQuery("");
            navigate(first);
          } else moveInList(e, list.current);
        }}
      />
      {query ? (
        <button type="button" className="iconbtn small settings-search-clear" aria-label={t("settings.find.clear")} title={t("settings.find.clear")} onClick={() => (setQuery(""), searchField.current?.focus())}>
          <Icon name="close" size={13} />
        </button>
      ) : wide && <kbd className="settings-search-key" aria-hidden>/</kbd>}
    </div>
  );
  const index = (
    <nav ref={list} className="settings-index" aria-label={t("settings.index")} onKeyDown={(e) => moveInList(e, list.current)}>
      {needle ? (
        hits.length ? (
          <div className="settings-group-block">
            <div className="settings-group settings-count" role="status">{hitCount(fields.length, hits.length)}</div>
            {hits.map((sec) => (
              <Fragment key={sec.id}>
                {link(sec)}
                {/* The rows a search found by one of their names, each a way straight to the row. */}
                {fieldsOf(sec.id).map((field) => (
                  <a key={field.anchor} href={fieldPath(field)} className="settings-field-hit" data-field-hit={field.anchor} onClick={(e) => leave(e, fieldPath(field))}>
                    {field.title[lang()]}
                  </a>
                ))}
              </Fragment>
            ))}
          </div>
        ) : (
          <div className="sub settings-empty" role="status">{t("settings.search.empty")}</div>
        )
      ) : (
        GROUPS.map((group) => {
          const items = group.sections.filter((sec) => visible(sec.id));
          if (!items.length) return null;
          return (
            <div key={group.id} className="settings-group-block" data-group={group.id}>
              <div className="settings-group">{t(`settings.group.${group.id}`)}</div>
              {items.map(link)}
            </div>
          );
        })
      )}
    </nav>
  );

  const body = (sec: Section) => {
    if (sec === "appearance") return <AppearancePanel part="theme" />;
    if (sec === "text") return <AppearancePanel part="text" />;
    if (sec === "desktop") return <DesktopPanel />;
    if (sec === "devices") return <DevicesPanel toast={toast} />;
    if (sec === "address") return <PublicAddressPanel />;
    if (!s) return <div className="empty">{t("common.loading")}</div>;
    switch (sec) {
      case "models":
        return <ModelsSection s={s} take={take} save={save} toast={toast} accounts={subscriptionAccounts} />;
      case "tasks":
        return (
          <>
            {/* Which models subagents may run on: it sat under Limits, but it is a choice of model and belongs with the others. */}
            <SetGroup title={t("settings.tasks.helpers")}>
              <Row title={t("subm.title")} desc={t("subm.settings.sub")} stack data-setting="subagents.models">
                <SubagentModelsPicker
                  value={s.subagents?.models ?? { mode: "all", presets: [] }}
                  {...modelItems(s)}
                  onChange={(models) => save({ subagents: { ...s.subagents!, models } })}
                />
              </Row>
              <Row title={t("qagent.title")} desc={t("qagent.settings.sub")} stack data-setting="subagents.quick_models">
                <QuickModelsPicker
                  value={s.subagents?.quick_models ?? []}
                  {...modelItems(s)}
                  onChange={(quick_models) => save({ subagents: { ...s.subagents!, quick_models } })}
                />
              </Row>
              {/* Beside the models it limits: how long a quick agent holds its leader waiting for the operator in the browser. */}
              <NumRow id="quick-operator-wait" setting="subagents.operator_wait_minutes" title={t("qagent.wait.setting")} desc={t("qagent.wait.setting.sub")} unit={t("settings.unit.minutes")} value={s.subagents?.operator_wait_minutes ?? 10} min={1} max={60} step={1} onSave={(v) => save({ subagents: { ...s.subagents!, operator_wait_minutes: v } })} {...shipped(s.subagents?.operator_wait_minutes ?? 10, 10, () => save({ subagents: { ...s.subagents!, operator_wait_minutes: 10 } }))} />
            </SetGroup>
            {Object.keys(s.presets ?? {}).length > 0 && (
              <SetGroup title={t("settings.orchestrator.title")} help={t("settings.orchestrator.sub")}>
                <Row title={t("settings.orchestrator.model")} desc={t("settings.orchestrator.hint")} stack data-setting="orchestrator.preset">
                  <ModelPicker
                    id="orchestrator-preset"
                    label={t("settings.orchestrator.title")}
                    catalogue={s}
                    value={orchestratorPreset(s.presets, s.orchestrator?.preset, s.orchestrator?.strongest)}
                    onChange={(preset) => save({ orchestrator: { preset } })}
                    tag={(id) => (id === s.orchestrator?.strongest ? t("settings.orchestrator.strongest") : undefined)}
                  />
                </Row>
              </SetGroup>
            )}
            {Object.keys(s.presets ?? {}).length > 0 && (
              <SetGroup title={t("settings.main.title")} help={t("settings.main.sub")} data-card="main-orchestrator">
                <Row title={t("settings.orchestrator.model")} desc={t("settings.main.hint")} stack data-setting="dispatcher.preset">
                  <ModelPicker
                    id="main-preset"
                    label={t("settings.main.title")}
                    catalogue={s}
                    value={mainPreset(s.presets, s.dispatcher?.preset, s.dispatcher?.middle)}
                    onChange={(preset) => save({ dispatcher: { preset } })}
                    tag={(id) => (id === s.dispatcher?.middle ? t("settings.main.middle") : undefined)}
                  />
                </Row>
              </SetGroup>
            )}
            {/* Here rather than under Tools: it is a choice of model, and this is where the operator
                comes to choose one. */}
            <SetGroup title={t("settings.vision.title")} help={t("settings.vision.sub")}>
              <Row title={t("settings.vision.model")} stack data-setting="vision.preset">
                {/* Only the models that see images are offered; the one in use stays listed even if
                    it does not, so the row names it rather than calling it missing. */}
                <ModelPicker
                  id="vision-preset"
                  label={t("settings.vision.title")}
                  catalogue={s}
                  value={s.vision.preset}
                  onChange={(preset) => save({ vision: { ...s.vision, preset } })}
                  keep={(id, p) => p.images || id === s.vision.preset}
                />
              </Row>
              <NumRow id="vision-output" title={t("settings.vision.output")} desc={t("settings.vision.output.sub")} setting="vision.max_output_tokens" value={s.vision.max_output_tokens} min={100} step={100} onSave={(v) => save({ vision: { ...s.vision, max_output_tokens: v } })} />
            </SetGroup>
            {/* The same setting as under Approvals, here too: the operator came to the models looking
                for which model reviews a push, and it was only on a page about permissions. */}
            <SetGroup
              title={t("settings.review.title")}
              help={<>{t("settings.review.sub")}{" "}<a href={pathFor("settings", "approvals")} onClick={(e) => go(e, pathFor("settings", "approvals"))}>{t("settings.review.rules")}</a></>}
            >
              <Row title={t("rules.review.model")} desc={t("rules.review.model.hint")} stack data-setting="action_rules.review_preset">
                <ModelPicker
                  id="models-review-preset"
                  label={t("rules.review.model")}
                  catalogue={s}
                  value={s.action_rules?.review_preset ?? ""}
                  onChange={(review_preset) => save({ action_rules: { ...DEFAULT_RULES, ...(s.action_rules ?? {}), review_preset } })}
                  defaultOption={{ label: t("rules.model.auto") }}
                />
              </Row>
            </SetGroup>
            {/* Beside the auxiliary models it governs: the summary model (Context) and the vision model above. */}
            <SetGroup title={t("settings.auxiliary.title")}>
              <Row title={t("settings.auxiliary.fallback")} desc={t("settings.auxiliary.fallback.sub")} data-setting="model.fallback_to_session">
                <Switch checked={s.model.fallback_to_session ?? true} onChange={(fallback_to_session) => save({ model: { fallback_to_session } as any })} label={t("settings.auxiliary.fallback")} />
              </Row>
            </SetGroup>
          </>
        );
      case "accounts":
        return <SubscriptionAccountsCard toast={toast} />;
      case "keyproxy":
        return <KeyproxyCard toast={toast} onChanged={rereadSettings} />;
      case "free-models":
        return <FreeModels onSaved={(_, next) => setS(next)} toast={toast} />;
      case "translation":
        return <TranslationSettings s={s} save={save} presetLabel={(id) => presetName(s, id)} />;
      case "rules":
        return (
          <>
            <PromptChange onApplied={(rules) => setS({ ...s, prompt: { ...s.prompt, rules } })} />
            <RulesEditor rules={s.prompt.rules} fallback={s.prompt.default_rules ?? ""} onSave={(rules) => save({ prompt: { rules } })} />
          </>
        );
      case "approvals":
        return (
          <>
            {/* The two rules about the agent's own code first: the action grid's review log grows as it
                loads, and a link to a row below it landed on a row the log had just pushed away. */}
            {/* Two separate rules, each a segmented pick like every other choice in Settings. They
                were three loose buttons once, the third a toggle that looked like a third mode. */}
            <SetGroup title={t("settings.selfchange")} help={t("settings.selfchange.sub")}>
              <Row title={t("settings.selfchange.approval")} data-setting="self_change.approval">
                <Segmented label={t("settings.selfchange.approval")} value={s.self_change.approval} onChange={(approval) => save({ self_change: { ...s.self_change, approval } })} options={["manual", "auto"].map((id) => ({ id, label: t(`settings.selfchange.${id}`) }))} />
              </Row>
              <Row title={t("settings.selfchange.rebuild")} data-setting="self_change.auto_rebuild">
                <Segmented label={t("settings.selfchange.rebuild")} value={s.self_change.auto_rebuild ? "on" : "off"} onChange={(state) => save({ self_change: { ...s.self_change, auto_rebuild: state === "on" } })} options={(["off", "on"] as const).map((id) => ({ id, label: t(`settings.selfchange.rebuild.${id}`) }))} />
              </Row>
            </SetGroup>
            <ActionRules s={s} save={save} />
            <ProjectApprovals toast={toast} />
          </>
        );
      case "budget":
        return (
          <>
            <SetGroup title={t("settings.budget.caps")}>
              <NumRow id="limits-daily" setting="limits.usd_per_day" title={t("settings.limits.daily")} desc={t("settings.limits.daily.sub")} unit="USD" step="1" value={s.limits.usd_per_day ?? 0} min={0} onSave={(v) => save({ limits: { ...s.limits, usd_per_day: v } })} />
              <NumRow id="limits-perrun" setting="limits.usd_per_run" title={t("settings.limits.perrun")} desc={t("settings.limits.perrun.sub")} unit="USD" step="0.5" value={s.limits.usd_per_run} min={0} onSave={(v) => save({ limits: { ...s.limits, usd_per_run: v } })} />
              <TotalCaps s={s} save={save} />
            </SetGroup>
            <SetGroup title={t("settings.balance.title")}>
              <Row title={t("settings.balance.thresholds")} desc={t("settings.balance.thresholds.sub")} htmlFor="balance-thresholds" data-setting="balance.thresholds_usd">
                <input id="balance-thresholds" className="field" defaultValue={s.balance.thresholds_usd.join(", ")} onBlur={(e) => save({ balance: { ...s.balance, thresholds_usd: e.target.value.split(",").map(Number).filter((n) => !Number.isNaN(n)) } })} />
              </Row>
              <NumRow id="balance-poll" setting="balance.poll_seconds" title={t("settings.balance.poll")} unit={t("settings.unit.seconds")} value={s.balance.poll_seconds} min={1} onSave={(v) => save({ balance: { ...s.balance, poll_seconds: v } })} />
            </SetGroup>
          </>
        );
      case "limits":
        return (
          <SetGroup title={t("settings.limits.title")}>
            <NumRow id="limits-iterations" setting="limits.max_iterations" title={t("settings.limits.iterations")} desc={t("settings.limits.iterations.sub")} value={s.limits.max_iterations} min={0} onSave={(v) => save({ limits: { ...s.limits, max_iterations: v } })} />
            <NumRow id="limits-subagents" setting="subagents.max_active" title={t("settings.limits.subagents")} desc={t("settings.limits.subagents.sub")} value={s.subagents?.max_active ?? 10} min={1} max={32} step={1} onSave={(v) => save({ subagents: { ...s.subagents!, max_active: v } })} {...shipped(s.subagents?.max_active ?? 10, 10, () => save({ subagents: { ...s.subagents!, max_active: 10 } }))} />
            <NumRow id="limits-disk" setting="disk.workspace_soft_limit_gb" title={t("settings.limits.disk")} desc={t("settings.limits.disk.sub")} unit={t("disk.limit.unit")} step="1" value={s.disk?.workspace_soft_limit_gb ?? 20} min={0} onSave={(v) => save({ disk: { workspace_soft_limit_gb: v } })} {...shipped(s.disk?.workspace_soft_limit_gb ?? 20, 20, () => save({ disk: { workspace_soft_limit_gb: 20 } }), `20 ${t("disk.limit.unit")}`)} />
            <HostMemoryRow s={s} save={save} />
          </SetGroup>
        );
      case "context":
        return (
          <SetGroup title={t("settings.compaction")} help={t("settings.compaction.sub")}>
            <NumRow id="compaction-ratio" setting="compaction.auto_ratio" title={t("settings.compaction.ratio")} desc={t("settings.compaction.ratio.sub")} value={s.compaction?.auto_ratio ?? 0.8} min={0} max={1} step={0.05} onSave={(v) => save({ compaction: { ...s.compaction, auto_ratio: v } })} {...shipped(s.compaction?.auto_ratio ?? 0.8, 0.8, () => save({ compaction: { ...s.compaction, auto_ratio: 0.8 } }))} />
            <NumRow id="compaction-keep" setting="compaction.keep_recent_messages" title={t("settings.compaction.keep")} value={s.compaction?.keep_recent_messages ?? 6} min={0} onSave={(v) => save({ compaction: { ...s.compaction, keep_recent_messages: v } })} {...shipped(s.compaction?.keep_recent_messages ?? 6, 6, () => save({ compaction: { ...s.compaction, keep_recent_messages: 6 } }))} />
            <NumRow id="compaction-words" setting="compaction.max_words" title={t("settings.compaction.words")} unit={t("settings.unit.words")} value={s.compaction?.max_words ?? 1200} min={200} step={100} onSave={(v) => save({ compaction: { ...s.compaction, max_words: v } })} {...shipped(s.compaction?.max_words ?? 1200, 1200, () => save({ compaction: { ...s.compaction, max_words: 1200 } }), `1200 ${t("settings.unit.words")}`)} />
            <NumRow id="compaction-core" setting="compaction.core_trigger_ratio" title={t("settings.compaction.core")} desc={t("settings.compaction.core.hint")} value={s.compaction?.core_trigger_ratio ?? 0.85} min={0.1} max={1} step={0.05} onSave={(v) => save({ compaction: { ...s.compaction, core_trigger_ratio: v } })} {...shipped(s.compaction?.core_trigger_ratio ?? 0.85, 0.85, () => save({ compaction: { ...s.compaction, core_trigger_ratio: 0.85 } }))} />
            <CompactionModelSelect catalogue={s} value={s.compaction?.preset ?? ""} onSave={(preset) => save({ compaction: { ...s.compaction, preset } })} />
            <NumRow id="compaction-timeout" setting="compaction.call_timeout_seconds" title={t("settings.compaction.timeout")} desc={t("settings.compaction.timeout.hint")} unit={t("settings.unit.seconds")} value={s.compaction?.call_timeout_seconds ?? 90} min={10} step={10} onSave={(v) => save({ compaction: { ...s.compaction, call_timeout_seconds: v } })} {...shipped(s.compaction?.call_timeout_seconds ?? 90, 90, () => save({ compaction: { ...s.compaction, call_timeout_seconds: 90 } }), `90 ${t("settings.unit.seconds")}`)} />
          </SetGroup>
        );
      case "memory":
        return <MemorySettings s={s} save={save} part="memory" />;
      case "memory-upkeep":
        return <MemorySettings s={s} save={save} part="upkeep" />;
      case "navigation":
        return <NavigationSettings s={s} save={save} selfdev={caps.data?.selfdev?.mode ?? "server"} />;
      case "environments":
        return <EnvironmentsTab s={s} save={save} toast={toast} />;
      case "browser":
        return <BrowserSettingsTab s={s} save={save} toast={toast} part="browser" />;
      case "browser-safety":
        return <BrowserSettingsTab s={s} save={save} toast={toast} part="safety" />;
      case "tools":
        return <ToolsTab s={s} save={save} toast={toast} onSettings={(next) => setS({ ...next, providers_available: next.providers_available ?? (s?.providers_available ?? []) })} />;
      case "search":
        // Read again after a search key is saved: which backends can be picked depends on it.
        return <SearchBlock s={s} save={save} toast={toast} reload={rereadSettings} />;
      case "mcp":
        return <McpServers toast={toast} />;
      case "voice":
        return (
          <>
            <VoiceSettings />
            <TtsVoices />
          </>
        );
      case "dictation":
        return <AsrSettingsCard s={s} save={save} />;
      case "speech-models":
        return <SpeechModels />;
      case "components":
        return <ComponentsTab toast={toast} />;
      case "dependencies":
        return <DependenciesTab />;
      case "chat":
        return (
          <>
            <SetGroup title={t("settings.chat.app")}>
              <Row title={t("settings.chat.predict")} desc={t("settings.chat.predict.sub")} data-setting="assist.predict" {...shipped(s.assist?.predict ?? true, true, () => save({ assist: { ...s.assist, predict: true } }), t("common.on"))}>
                <Switch checked={s.assist?.predict ?? true} onChange={(predict) => save({ assist: { ...s.assist, predict } })} label={t("settings.chat.predict")} />
              </Row>
            </SetGroup>
            <AutopilotSettings s={s} save={save} />
            <UserStyleCard toast={toast} />
          </>
        );
      case "telegram": {
        const mode = s.telegram.mode ?? (s.telegram.forum_chat_id ? "topics" : "private");
        const tg = (patch: Partial<Settings["telegram"]>) => save({ telegram: { ...s.telegram, ...patch } });
        return (
          <SetGroup title={t("settings.chat.telegram")}>
            <Row title={t("settings.chat.where")} data-setting="telegram.mode" desc={mode === "private" ? t("settings.chat.private.sub") : t(s.telegram.forum_chat_id ? "settings.chat.topics.sub" : "settings.chat.topics.bind")}>
              <Segmented label={t("settings.chat.where")} value={mode} onChange={(m) => tg({ mode: m })} options={(["private", "topics"] as const).map((m) => ({ id: m, label: t(`settings.chat.${m}`) }))} />
            </Row>
            {/* The digits are the ones /verbosity takes in the chat, so they stay; the description is
                what they mean, which nothing on the page said. */}
            <Row title={t("settings.chat.verbosity")} desc={t("settings.chat.verbosity.sub")} data-setting="telegram.verbosity">
              <Segmented label={t("settings.chat.verbosity")} value={String(s.telegram.verbosity)} onChange={(v) => tg({ verbosity: Number(v) })} options={["0", "1", "2"].map((v) => ({ id: v, label: v }))} />
            </Row>
            <Row title={t("settings.chat.reactions")} desc={t("settings.chat.reactions.sub")} data-setting="telegram.reactions">
              <Switch checked={!!s.telegram.reactions} onChange={(reactions) => tg({ reactions })} label={t("settings.chat.reactions")} />
            </Row>
            <Row title={t("settings.chat.topicemoji")} desc={t("settings.chat.topicemoji.sub")} data-setting="telegram.topic_status_emoji">
              <Switch checked={!!s.telegram.topic_status_emoji} onChange={(topic_status_emoji) => tg({ topic_status_emoji })} label={t("settings.chat.topicemoji")} />
            </Row>
            <Row title={t("settings.chat.forward")} desc={t("settings.chat.forward.sub")} data-setting="telegram.forward_unknown_commands">
              <Switch checked={!!s.telegram.forward_unknown_commands} onChange={(forward_unknown_commands) => tg({ forward_unknown_commands })} label={t("settings.chat.forward")} />
            </Row>
            <NumRow id="chat-stale" setting="telegram.stale_after_seconds" title={t("settings.chat.stale")} desc={t("settings.chat.stale.sub")} unit={t("settings.unit.seconds")} value={s.telegram.stale_after_seconds} min={0} onSave={(v) => tg({ stale_after_seconds: v })} />
            <NumRow id="chat-maxfile" setting="telegram.max_inbound_file_mb" title={t("settings.chat.maxfile")} unit={t("settings.unit.mb")} value={s.telegram.max_inbound_file_mb} min={1} onSave={(v) => tg({ max_inbound_file_mb: v })} />
            <NumRow id="chat-caption" setting="telegram.photo_caption_wait_seconds" title={t("settings.chat.caption")} unit={t("settings.unit.seconds")} value={s.telegram.photo_caption_wait_seconds} min={0} onSave={(v) => tg({ photo_caption_wait_seconds: v })} />
            <NumRow id="chat-slowtool" setting="telegram.slow_tool_seconds" title={t("settings.chat.slowtool")} unit={t("settings.unit.seconds")} value={s.telegram.slow_tool_seconds} min={1} onSave={(v) => tg({ slow_tool_seconds: v })} />
          </SetGroup>
        );
      }
      case "schedules":
        return (
          <>
            <SetGroup title={t("settings.chat.scheduler")}>
              <Row title={t("settings.chat.scheduled")} desc={t("settings.chat.scheduled.sub")} stack data-setting="scheduler.topic_mode">
                <Segmented label={t("settings.chat.scheduled")} value={s.scheduler.topic_mode} onChange={(topic_mode) => save({ scheduler: { ...s.scheduler, topic_mode } })} options={["per_task", "per_run"].map((m) => ({ id: m, label: t(m === "per_task" ? "settings.chat.pertask" : "settings.chat.perrun") }))} />
              </Row>
              <Row title={t("settings.chat.schedapproval")} desc={t("settings.chat.schedapproval.sub")} data-setting="scheduler.require_approval">
                <Switch checked={!!s.scheduler.require_approval} onChange={(require_approval) => save({ scheduler: { ...s.scheduler, require_approval } })} label={t("settings.chat.schedapproval")} />
              </Row>
            </SetGroup>
            <HeartbeatTab s={s} toast={toast} />
          </>
        );
      case "notifications":
        return <NotificationSettings toast={toast} />;
      case "security":
        return <SecurityTab toast={toast} />;
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
            <SetGroup title={t("settings.about.runtime")}>
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
                  {status.budget_exceeded && <SetBlock><span className="set-warn">{t("settings.about.budget", { what: String(status.budget_exceeded) })}</span></SetBlock>}
                </>
              ) : (
                <SetBlock><span className="set-hint">{t("settings.about.nostatus")}</span></SetBlock>
              )}
            </SetGroup>
            {!telegram()?.initData && (
              <SetGroup title={t("settings.about.browser")}>
                <Row title={t("settings.sec.appearance")} desc={t("settings.about.appearance")}>
                  <a className="btn small" href={pathFor("settings", "appearance")} onClick={(e) => go(e, pathFor("settings", "appearance"))}>{t("settings.about.open")}</a>
                </Row>
                <Row title={t("settings.logout")} desc={t("settings.about.logout.sub")} data-setting="logout">
                  <Button size="sm" onClick={() => void logout()}>{t("settings.about.logout.action")}</Button>
                </Row>
              </SetGroup>
            )}
          </>
        );
      }
    }
  };
  // Closing the sheet takes `add` out of the address, so a reload or the back button does not open it again.
  const stopAdding = () => {
    setAdding(false);
    const params = new URLSearchParams(window.location.search);
    if (params.has("add")) {
      params.delete("add");
      const rest = params.toString();
      navigate(window.location.pathname + (rest ? `?${rest}` : ""), { replace: true });
    }
  };
  const sheets = (
    <>
      {adding && (
        <Sheet title={t("settings.models.add")} size="xl" onClose={stopAdding}>
          <AddModel
            toast={toast}
            onCancel={stopAdding}
            onSaved={(_, next) => {
              take(next);
              stopAdding();
            }}
          />
        </Sheet>
      )}
      {addingProvider && s && (
        <Sheet title={t("settings.provider.add")} onClose={() => setAddingProvider(false)}>
          <AddProvider s={s} toast={toast} onAdd={addProvider} onDone={() => setAddingProvider(false)} />
        </Sheet>
      )}
    </>
  );
  /** What a section puts in the page's header beside its title: the models' two ways to add. */
  const actions = (sec: Section) =>
    sec === "models" && s ? (
      <>
        <button type="button" className="btn" data-setting="models.add" onClick={() => setAdding(true)}>
          <Icon name="plus" size={16} /> {t("settings.models.add")}
        </button>
        <button type="button" className="btn primary" onClick={() => setAddingProvider(true)}>
          <Icon name="plus" size={16} /> {t("settings.provider.add")}
        </button>
      </>
    ) : null;
  // Found rows as the controls they are, each section's body cut down to them (settingssearch.tsx);
  // a section found by its own name, not by a row, is a way into it.
  const results = hits.length === 0 ? (
    <div className="empty settings-none" role="status">
      <b>{t("settings.search.empty")}</b>
      <span className="sub">{t("settings.search.empty.sub")}</span>
    </div>
  ) : (
    hits.map((sec) => {
      const own = fieldsOf(sec.id);
      const href = pathFor("settings", sec.id);
      return (
        <section key={sec.id} className="settings-hit-section" data-hit-section={sec.id}>
          <h2 className="settings-hit-head">
            <a href={href} onClick={(e) => leave(e, href)}>{sectionLabel(sec.id)}</a>
          </h2>
          {own.length ? (
            <HitBody fields={own}>{body(sec.id)}</HitBody>
          ) : (
            <div className="card settings-hit-links">
              <a className="settings-hit-link" href={href} onClick={(e) => leave(e, href)}>
                <b>{sectionLabel(sec.id)}</b>
                <span className="sub">{about(sec.id)}</span>
              </a>
            </div>
          )}
        </section>
      );
    })
  );

  if (wide) {
    return (
      <div ref={page} className="settings-page" tabIndex={-1} aria-labelledby="settings-page-title">
        <aside className="settings-nav" aria-label={screenTitle("settings")}>
          <div className="settings-nav-head">
            <button type="button" className="settings-close" aria-label={t("settings.back.label")} onClick={close}>
              <Icon name="back" size={16} />
              <span className="settings-close-text">{t("settings.back")}</span>
              <kbd className="settings-close-key" aria-hidden>Esc</kbd>
            </button>
          </div>
          {search}
          {index}
        </aside>
        <main className="settings-pane">
          <header className="settings-head">
            <h1 id="settings-page-title" className="settings-title">{needle ? t("settings.find.title", { q: needle }) : sectionLabel(shown!)}</h1>
            {!needle && actions(shown!)}
          </header>
          <div ref={main} className="settings-main">
            <div className="settings-col" data-section={needle ? undefined : shown!}>
              {needle && hits.length > 0 && <p className="settings-about">{t("settings.find.lead")}</p>}
              {needle ? results : body(shown!)}
            </div>
          </div>
        </main>
        {sheets}
      </div>
    );
  }
  // A phone: the list of sections (the first level) or one section (the second). A link to a row
  // opens the second level straight away; its back goes to the first, not out of Settings.
  const level = current && !needle ? current : null;
  return (
    <div ref={page} className="settings-phone" tabIndex={-1} aria-labelledby="settings-page-title">
      <header className="pagehead settings-phone-head">
        <div className="pagehead-row">
          {level ? (
            <button type="button" className="settings-phone-back" onClick={() => navigate(pathFor("settings"), { replace: true })}>
              <Icon name="back" /> <span id="settings-page-title">{screenTitle("settings")}</span>
            </button>
          ) : (
            <>
              <button type="button" className="iconbtn settings-close" aria-label={t("common.close")} title={t("common.close")} onClick={close}>
                <Icon name="close" />
              </button>
              <div className="pagehead-title">
                <h1 id="settings-page-title">{screenTitle("settings")}</h1>
              </div>
            </>
          )}
        </div>
      </header>
      {level ? (
        <div className="screen narrow settings-col" data-section={level}>
          <h1 className="settings-title">{sectionLabel(level)}</h1>
          <p className="settings-about">{about(level)}</p>
          {level === "models" && s && <div className="btnrow settings-phone-actions">{actions(level)}</div>}
          {body(level)}
        </div>
      ) : (
        <div className="screen narrow">
          {search}
          {index}
        </div>
      )}
      {sheets}
    </div>
  );
}
