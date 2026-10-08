import { ControlTrigger } from "./ui/control-trigger";
// The model, chosen from inside the composer. The button names the model in use with a glyph for
// its speed; the picker groups presets by provider before showing individual choices. Search
// spans the collection from the root and stays scoped inside a provider. While another model stands in for the
// configured one the button is amber and says so, and the list offers the way back first.

import { useEffect, useRef, useState } from "react";
import { api, ModelFallback, Preset, ProviderConf } from "./api";
import { SegmentedControl } from "./ui/phone";
import { REASONING_EFFORTS, effortIndex } from "./models";
import { Popover, Sheet } from "./ui/dialogs";
import { Icon } from "./icons";
import { shortModel, tokens } from "./format";
import { readCustomModel, rememberCustomModel } from "./composer";
import { DICT, num, t } from "./i18n";
import { ProviderMark, providerName } from "./ui/provider-mark";
import { EffortMenu } from "./effortselect";
import { pathFor } from "./router";

export type ModelChoice = { clear: true } | { preset: string } | { provider: string; model: string } | { model: string };

type Catalogue = { presets: Record<string, Preset>; providers: Record<string, ProviderConf>; global: string; globalId: string };

export type ModelSelectProps = {
  /** The model the session is set to, as the host names it. */
  model: string;
  fallback: ModelFallback | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChoose: (choice: ModelChoice) => void;
  /** Phones: a sheet instead of a popover. */
  sheet: boolean;
  effort?: string;
  thinking?: boolean;
  onChooseEffort?: (effort: string) => void;
};

/** Whether a preset is the one the session is set to: by its label, or by its provider/model pair. */
function isCurrent(id: string, p: Preset, model: string): boolean {
  if (!model) return false;
  return model === id || model === p.label || model === p.model || model === `${p.provider}/${p.model}`;
}

export function ModelSelect({ model, fallback, open, onOpenChange, onChoose, sheet, effort, thinking, onChooseEffort }: ModelSelectProps) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [cat, setCat] = useState<Catalogue | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    let gone = false;
    setFailed(null);
    api
      .get<{ presets?: Record<string, Preset>; providers?: Record<string, ProviderConf>; model?: { preset?: string } }>("/api/settings")
      .then((st) => {
        if (gone) return;
        const presets = st.presets ?? {};
        const globalId = String(st.model?.preset ?? "");
        const def = presets[globalId];
        setCat({ presets, providers:st.providers ?? {}, globalId, global: def ? def.label || `${def.provider}/${def.model}` : globalId || t("settings.heartbeat.default") });
      })
      .catch((e) => !gone && setFailed(String(e)));
    return () => {
      gone = true;
    };
  }, [open]);
  const pick = (choice: ModelChoice) => {
    onOpenChange(false);
    onChoose(choice);
  };
  // While a fallback answers, the pill names the stand-in and then the configured model behind an
  // arrow, each in its own span. It used to be one sentence, "via DeepSeek Flash (fallback from
  // Claude Opus 5)", which a phone cut to "via DeepSeek Fla…" and lost the configured model entirely;
  // a narrow composer now stacks the two names and leaves the effort to the sheet (styles.css).
  const label = fallback ? (
    <>
      <span className="model-to truncate">{shortModel(fallback.to, 18)}</span>
      <span className="model-from truncate">{t("session.model.fallback", { from: shortModel(fallback.from, 18) })}</span>
    </>
  ) : shortModel(model, 22);
  const reason = fallback ? (DICT[`session.model.reason.${fallback.reason}`] ? t(`session.model.reason.${fallback.reason}`) : fallback.reason) : "";
  const title = fallback ? `${t("session.model.fallback.turn", { to: fallback.to, from: fallback.from })} — ${reason}` : t("composer.model.title");
  const list = <ModelList cat={cat} failed={failed} model={model} fallback={fallback} onPick={pick} effort={effort} thinking={thinking} onChooseEffort={onChooseEffort ? (value) => { onChooseEffort(value); onOpenChange(false); } : undefined} sheet={sheet} />;
  return (
    <>
      <ControlTrigger ref={trigger} type="button" className={`model-select ${fallback ? "attn" : ""} ${open ? "on" : ""}`} onClick={() => onOpenChange(!open)} title={title} aria-label={t("session.model.for")} aria-haspopup="menu" aria-expanded={open}>
        {fallback ? <span className="model-dot" aria-hidden /> : <Icon name="model" size={14} />}
        {/* The effort is its own span so a long model name is what gives way: in one span the
            ellipsis ate the effort first and the pill read "DeepSeek Flash · l…". */}
        <span className={`model-label truncate ${fallback ? "model-fallback" : ""}`}>{label}</span>
        {onChooseEffort && <span className="model-effort">{t(thinking ? `add.effort.${effort || "medium"}` : "add.effort.off")}</span>}

      </ControlTrigger>
      {open && sheet && (
        <Sheet title={t("session.model")} onClose={() => onOpenChange(false)} className="model-sheet"
          head={cat ? <span className="model-sheet-sub">{t("composer.model.forchat", { n: num(Object.keys(cat.presets).length) })}</span> : undefined}>
          {list}
        </Sheet>
      )}
      {open && !sheet && (
        <Popover anchor={trigger.current} onClose={() => onOpenChange(false)} className="model-menu" align="right" label={t("session.model.for")}>
          {list}
        </Popover>
      )}
    </>
  );
}

function ModelList({ cat, failed, model, fallback, onPick, effort, thinking, onChooseEffort, sheet }: { cat: Catalogue | null; failed: string | null; model: string; fallback: ModelFallback | null; onPick: (c: ModelChoice) => void; effort?: string; thinking?: boolean; onChooseEffort?: (value: string) => void; sheet: boolean }) {
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState<string | null>(null);
  const [freeGroup, setFreeGroup] = useState(false);
  const [customOpen, setCustomOpen] = useState(false);
  const list = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (provider !== null) list.current?.querySelector<HTMLButtonElement>(".provider-back")?.focus({ preventScroll:true });
  }, [provider]);
  const [custom, setCustom] = useState(() => readCustomModel());
  if (failed) return <div className="sub model-row-note">{failed}</div>;
  if (!cat) return <div className="sub model-row-note">{t("common.loading")}</div>;
  const entries = Object.entries(cat.presets);
  const search = query.trim().toLocaleLowerCase();
  const matched = entries.filter(([id, p]) => (provider === null || p.provider === provider) && (!freeGroup || p.free_only) && `${id} ${p.label} ${p.provider} ${providerName(p.provider, cat.providers[p.provider])} ${p.model}`.toLocaleLowerCase().includes(search));
  const providers = [...new Set(entries.filter(([, p]) => !!p.free_only === freeGroup).map(([, p]) => p.provider))];
  const browsing = provider === null && !search;
  const back = () => {
    const previous = provider;
    setProvider(null);
    setQuery("");
    requestAnimationFrame(() => list.current?.querySelector<HTMLButtonElement>(`[data-provider="${CSS.escape(previous ?? "")}"]`)?.focus({ preventScroll:true }));
  };
  // The way back from a fallback: the configured model, named first, as the preset it is or as itself.
  const configured = fallback ? (entries.find(([id, p]) => isCurrent(id, p, fallback.from)) ?? null) : null;
  const useCustom = () => {
    const value = custom.trim();
    if (!value) return;
    rememberCustomModel(value);
    const [prov, ...rest] = value.split("/");
    const m = rest.join("/");
    onPick(m ? { provider: prov, model: m } : { model: prov });
  };
  if (sheet) return (
    <PhoneModelList
      listRef={list} cat={cat} model={model} fallback={fallback} configured={configured} onPick={onPick}
      query={query} setQuery={setQuery} provider={provider} setProvider={setProvider} freeGroup={freeGroup} setFreeGroup={setFreeGroup}
      browsing={browsing} matched={matched} providers={providers} entries={entries} back={back}
      effort={effort} thinking={thinking} onChooseEffort={onChooseEffort}
      customOpen={customOpen} setCustomOpen={setCustomOpen} custom={custom} setCustom={setCustom} useCustom={useCustom}
    />
  );
  return (
    <div ref={list} className="model-list" onKeyDown={(e) => { if (e.key === "ArrowLeft" && provider !== null && e.target instanceof HTMLButtonElement) { e.preventDefault(); back(); } }}>
      {/* The heading carries the key to the two marks, so what they mean is on the screen and not
          only in a tooltip a touch screen never shows. */}
      <div className="menu-heading sub model-heading">
        <span className="grow">{t(browsing ? freeGroup ? "composer.model.free" : "composer.model.providers" : "session.model")}</span>
        {!browsing && <span className="model-legend"><span className="model-kind thinking" aria-hidden>✦</span> {t("composer.model.thinking")}</span>}
        {!browsing && <span className="model-legend"><span className="model-kind fast" aria-hidden>⚡</span> {t("composer.model.fast")}</span>}
      </div>
      {freeGroup && provider === null && <button type="button" role="menuitem" className="model-row provider-back" onClick={() => { setFreeGroup(false); setQuery(""); }}><Icon name="back" size={16} /><span className="grow">{t("composer.model.back")}</span></button>}
      {provider !== null && <button type="button" role="menuitem" className="model-row provider-back" onClick={back} aria-label={t("composer.model.back")}>
        <Icon name="back" size={16} /><ProviderMark id={provider} kind={cat.providers[provider]?.kind} />
        <span className="grow">{providerName(provider, cat.providers[provider])}</span>
      </button>}
      <input className="field model-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("composer.model.search")} aria-label={t("composer.model.search")} />
      {fallback && (
        <>
          <div className="model-row-note sub attn">{t("composer.model.configured", { model: fallback.from })}</div>
          <button type="button" role="menuitem" className="model-row restore" onClick={() => onPick(configured ? { preset: configured[0] } : { model: fallback.from })}>
            <Icon name="undo" size={16} />
            <span className="grow truncate">{t("composer.model.restore", { model: shortModel(fallback.from, 24) })}</span>
          </button>
          <div className="menu-sep" />
        </>
      )}
      {(provider !== null && cat.presets[cat.globalId]?.provider === provider || customOpen && !cat.presets[cat.globalId]) && <button type="button" role="menuitem" className="model-row" onClick={() => onPick({ clear: true })}>
        <Icon name="model" size={16} />
        <span className="model-text grow">
          <span>{t("session.model.global")}</span>
          <span className="sub">{cat.global}</span>
        </span>
      </button>}
      {browsing && !freeGroup && <button type="button" role="menuitem" className="model-row provider-row" onClick={() => { setFreeGroup(true); setQuery(""); }}>
        <Icon name="model" size={16} /><span className="grow model-text"><span>{t("composer.model.free")}</span><span className="sub">{t("composer.model.free.sub")}</span></span><Icon name="chevron" size={14} />
      </button>}
      {browsing && providers.map((id) => {
        const choices = entries.filter(([, p]) => p.provider === id && !!p.free_only === freeGroup);
        const current = choices.find(([key, p]) => isCurrent(key, p, model));
        return <button key={id} type="button" role="menuitem" className="model-row provider-row" data-provider={id} onClick={() => { setProvider(id); setQuery(""); }}>
          <ProviderMark id={id} kind={cat.providers[id]?.kind} />
          <span className="grow model-text"><span>{providerName(id, cat.providers[id])}</span><span className="sub">{current ? current[1].label || current[1].model : id}</span></span>
          <span className="sub provider-count" title={t("composer.model.count", { n:num(choices.length) })}>{num(choices.length)}</span>
          {current && <Icon name="check" size={16} />}
          <Icon name="chevron" size={14} />
        </button>;
      })}
      {browsing && freeGroup && providers.length === 0 && <div className="model-row-note sub"><a href={pathFor("settings", "models", { tab: "free" })}>{t("free.chat.add")}</a></div>}
      {!browsing && matched.length === 0 && <div className="model-row-note sub" role="status">{t("shell.search.nomatch")}</div>}
      {!browsing && matched.map(([id, p]) => {
        const current = isCurrent(id, p, model);
        return (
          <button key={id} type="button" role="menuitem" className={`model-row ${current ? "on" : ""}`} onClick={() => onPick({ preset: id })} aria-current={current ? "true" : undefined}>
            <span className={`model-kind ${p.thinking ? "thinking" : "fast"}`} title={t(p.thinking ? "composer.model.thinking" : "composer.model.fast")} aria-label={t(p.thinking ? "composer.model.thinking" : "composer.model.fast")}>
              {p.thinking ? "✦" : "⚡"}
            </span>
            <span className="grow model-text">
              <span className="truncate">{p.label || p.model}</span>
              <span className="sub truncate">{p.provider}/{p.model}</span>
            </span>
            {p.context_window > 0 && <span className="sub" title={t("composer.model.context", { n: num(p.context_window) })}>{tokens(p.context_window)}</span>}
            {current && <Icon name="check" size={16} />}
          </button>
        );
      })}
      <div className="menu-sep" />
      {onChooseEffort && <EffortMenu effort={effort} thinking={thinking} onChoose={onChooseEffort} inline={sheet} />}
      <button type="button" role="menuitem" className="model-row" aria-expanded={customOpen} onClick={() => setCustomOpen(!customOpen)}><Icon name="pen" size={16} /><span className="grow">{t("composer.model.manual")}</span><Icon name="chevron" size={14} /></button>
      {customOpen && <div className="model-custom">
        <div className="sub">{t("session.model.custom")}</div>
        <div className="model-custom-row">
          <input
            className="field"
            placeholder="vllm/Qwen3.6"
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                useCustom();
              }
            }}
            aria-label={t("session.model.custom")}
          />
          <button type="button" className="btn small primary" disabled={!custom.trim()} onClick={useCustom}>{t("session.model.use")}</button>
        </div>
      </div>}
    </div>
  );
}

type PhoneListProps = {
  listRef: React.RefObject<HTMLDivElement | null>; cat: Catalogue; model: string; fallback: ModelFallback | null; configured: [string, Preset] | null; onPick: (c: ModelChoice) => void;
  query: string; setQuery: (q: string) => void; provider: string | null; setProvider: (p: string | null) => void; freeGroup: boolean; setFreeGroup: (on: boolean) => void;
  browsing: boolean; matched: [string, Preset][]; providers: string[]; entries: [string, Preset][]; back: () => void;
  effort?: string; thinking?: boolean; onChooseEffort?: (value: string) => void;
  customOpen: boolean; setCustomOpen: (open: boolean) => void; custom: string; setCustom: (value: string) => void; useCustom: () => void;
};

/** What a provider needs before it answers, in the words of its row: a key that is there or not. */
function keyState(conf: ProviderConf | undefined): string {
  if (!conf) return "";
  if (conf.kind === "local" || conf.kind === "llamacpp" || conf.kind === "vllm" || conf.kind === "ollama") return t("composer.model.nokey");
  if (conf.billing === "subscription") return t("composer.model.subscription");
  return t(conf.api_key_set ? "composer.model.keyready" : "composer.model.keymissing");
}

/**
 * The phone's model sheet, in the order the design gives it: search, the way back from a stand-in,
 * effort as one row of choices, the models of the provider in use, then the other providers and
 * manual entry. The same choices, class names and drill-down as the desktop's list, so a provider
 * opens the same way and the keyboard behaves the same; only the layout is the phone's.
 */
function PhoneModelList(p: PhoneListProps) {
  const { cat, model, fallback, configured, onPick } = p;
  const currentEntry = p.entries.find(([id, preset]) => isCurrent(id, preset, model)) ?? null;
  const home = currentEntry && !currentEntry[1].free_only ? currentEntry[1].provider : null;
  const homeModels = home ? p.entries.filter(([, preset]) => preset.provider === home && !preset.free_only) : [];
  const row = ([id, preset]: [string, Preset]) => {
    const current = isCurrent(id, preset, model);
    const billing = cat.providers[preset.provider]?.billing === "subscription" ? t("composer.model.subscription") : "";
    return (
      <button key={id} type="button" role="menuitem" className={`model-row ph-model-row ${current ? "on" : ""}`} onClick={() => onPick({ preset: id })} aria-current={current ? "true" : undefined}>
        <span className="grow model-text">
          <span className="ph-model-name"><span className="truncate">{preset.label || preset.model}</span>
            <span className="ph-model-tag">{t(preset.thinking ? "composer.model.thinking" : "composer.model.fast")}</span>
            {current && <span className="ph-model-tag on">{t("composer.model.current")}</span>}
          </span>
          <span className="sub truncate">{[preset.context_window > 0 ? t("composer.model.context", { n: tokens(preset.context_window) }) : `${preset.provider}/${preset.model}`, billing].filter(Boolean).join(" · ")}</span>
        </span>
        {current && <Icon name="check" size={18} />}
      </button>
    );
  };
  return (
    <div ref={p.listRef} className="model-list ph-model-list" onKeyDown={(e) => { if (e.key === "ArrowLeft" && p.provider !== null && e.target instanceof HTMLButtonElement) { e.preventDefault(); p.back(); } }}>
      <label className="ph-search ph-model-search">
        <Icon name="search" size={18} />
        <input className="model-search" type="search" value={p.query} onChange={(e) => p.setQuery(e.target.value)} placeholder={t("composer.model.search")} aria-label={t("composer.model.search")} />
      </label>
      {fallback && (
        <div className="ph-model-fallback" role="status">
          <Icon name="reload" size={18} />
          <div className="grow">
            <div>{t("composer.model.standing", { model: shortModel(fallback.to, 28) })}</div>
            <div className="sub">{t("composer.model.configured", { model: fallback.from })}{DICT[`session.model.reason.${fallback.reason}`] ? ` · ${t(`session.model.reason.${fallback.reason}`)}` : ""}</div>
            <button type="button" role="menuitem" className="model-row restore ph-btn sm" onClick={() => onPick(configured ? { preset: configured[0] } : { model: fallback.from })}>
              <span className="truncate">{t("composer.model.restore", { model: shortModel(fallback.from, 24) })}</span>
            </button>
          </div>
        </div>
      )}
      {p.onChooseEffort && p.browsing && !p.freeGroup && (
        <>
          <div className="ph-model-sec">{t("composer.effort.short")}</div>
          <SegmentedControl className="ph-model-effort" label={t("composer.effort.short")}
            value={p.thinking ? REASONING_EFFORTS[effortIndex(p.effort)] : "off"}
            options={["off", ...REASONING_EFFORTS].map((value) => ({ id: value, label: t(value === "off" ? "composer.effort.offShort" : `add.effort.${value}`) }))}
            onChange={(value) => p.onChooseEffort!(value)} />
        </>
      )}
      {p.freeGroup && p.provider === null && <button type="button" role="menuitem" className="model-row provider-back" onClick={() => { p.setFreeGroup(false); p.setQuery(""); }}><Icon name="back" size={18} /><span className="grow">{t("composer.model.back")}</span></button>}
      {p.provider !== null && <button type="button" role="menuitem" className="model-row provider-back" onClick={p.back} aria-label={t("composer.model.back")}>
        <Icon name="back" size={18} /><ProviderMark id={p.provider} kind={cat.providers[p.provider]?.kind} />
        <span className="grow">{providerName(p.provider, cat.providers[p.provider])}</span>
      </button>}
      {p.browsing && !p.freeGroup && home && (
        <>
          <div className="ph-model-sec">{[providerName(home, cat.providers[home]), keyState(cat.providers[home])].filter(Boolean).join(" · ")}</div>
          {homeModels.map(row)}
        </>
      )}
      {(p.provider !== null && cat.presets[cat.globalId]?.provider === p.provider || p.customOpen && !cat.presets[cat.globalId]) && <button type="button" role="menuitem" className="model-row" onClick={() => onPick({ clear: true })}>
        <span className="model-text grow">
          <span>{t("session.model.global")}</span>
          <span className="sub">{cat.global}</span>
        </span>
      </button>}
      {p.browsing && <div className="ph-model-sec">{t(p.freeGroup ? "composer.model.free" : "composer.model.providers")}</div>}
      {p.browsing && p.providers.map((id) => {
        const choices = p.entries.filter(([, preset]) => preset.provider === id && !!preset.free_only === p.freeGroup);
        return <button key={id} type="button" role="menuitem" className="model-row provider-row" data-provider={id} onClick={() => { p.setProvider(id); p.setQuery(""); }}>
          <span className="grow model-text"><span>{providerName(id, cat.providers[id])}</span><span className="sub truncate">{[keyState(cat.providers[id]), t("composer.model.count", { n: num(choices.length) })].filter(Boolean).join(" · ")}</span></span>
          <span className="provider-count" hidden>{num(choices.length)}</span>
          <Icon name="chevron" size={16} />
        </button>;
      })}
      {p.browsing && !p.freeGroup && <button type="button" role="menuitem" className="model-row provider-row" onClick={() => { p.setFreeGroup(true); p.setQuery(""); }}>
        <span className="grow model-text"><span>{t("composer.model.free")}</span><span className="sub">{t("composer.model.free.sub")}</span></span><Icon name="chevron" size={16} />
      </button>}
      {p.browsing && p.freeGroup && p.providers.length === 0 && <div className="model-row-note sub"><a href={pathFor("settings", "models", { tab: "free" })}>{t("free.chat.add")}</a></div>}
      {!p.browsing && p.matched.length === 0 && <div className="model-row-note sub" role="status">{t("shell.search.nomatch")}</div>}
      {!p.browsing && p.matched.map(row)}
      <button type="button" role="menuitem" className="model-row" aria-expanded={p.customOpen} onClick={() => p.setCustomOpen(!p.customOpen)}><Icon name="pen" size={18} /><span className="grow">{t("composer.model.manual")}</span><Icon name="chevron" size={16} /></button>
      {p.customOpen && <div className="model-custom">
        <div className="sub">{t("session.model.custom")}</div>
        <div className="ph-tray-answer">
          <input className="ph-field" placeholder="vllm/Qwen3.6" value={p.custom} onChange={(e) => p.setCustom(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); p.useCustom(); } }} aria-label={t("session.model.custom")} />
          <button type="button" className="ph-btn primary" disabled={!p.custom.trim()} onClick={p.useCustom}>{t("session.model.use")}</button>
        </div>
      </div>}
    </div>
  );
}
