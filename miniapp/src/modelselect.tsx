import { ControlTrigger } from "./ui/control-trigger";
// The model, chosen from inside the composer. The button names the model in use with a glyph for
// its speed; the picker groups presets by provider before showing individual choices. Search
// spans the collection from the root and stays scoped inside a provider. While another model stands in for the
// configured one the button is amber and says so, and the list offers the way back first.

import { useEffect, useRef, useState } from "react";
import { api, ModelFallback, Preset, ProviderConf } from "./api";
import { Popover, Sheet } from "./ui/dialogs";
import { Icon } from "./icons";
import { shortModel, tokens } from "./format";
import { readCustomModel, rememberCustomModel } from "./composer";
import { DICT, num, t } from "./i18n";
import { ProviderMark, providerName } from "./ui/provider-mark";
import { EffortOptions } from "./effortselect";

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
  const list = <ModelList cat={cat} failed={failed} model={model} fallback={fallback} onPick={pick} />;
  return (
    <>
      <ControlTrigger ref={trigger} type="button" className={`model-select ${fallback ? "attn" : ""} ${open ? "on" : ""}`} onClick={() => onOpenChange(!open)} title={title} aria-label={t("session.model.for")} aria-haspopup="menu" aria-expanded={open}>
        {fallback ? <span className="model-dot" aria-hidden /> : <Icon name="model" size={14} />}
        {/* The effort is its own span so a long model name is what gives way: in one span the
            ellipsis ate the effort first and the pill read "DeepSeek Flash · l…". */}
        <span className={`model-label truncate ${fallback ? "model-fallback" : ""}`}>{label}</span>
        {onChooseEffort && thinking && <span className="model-effort">· {t(`add.effort.${effort || "medium"}`)}</span>}

      </ControlTrigger>
      {open && sheet && (
        <Sheet title={t("composer.settings")} onClose={() => onOpenChange(false)} className="model-sheet">
          {onChooseEffort && <EffortOptions effort={effort} thinking={thinking} onChoose={onChooseEffort} />}
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

function ModelList({ cat, failed, model, fallback, onPick }: { cat: Catalogue | null; failed: string | null; model: string; fallback: ModelFallback | null; onPick: (c: ModelChoice) => void }) {
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState<string | null>(null);
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
  const matched = entries.filter(([id, p]) => (provider === null || p.provider === provider) && `${id} ${p.label} ${p.provider} ${providerName(p.provider, cat.providers[p.provider])} ${p.model}`.toLocaleLowerCase().includes(search));
  const providers = [...new Set(entries.map(([, p]) => p.provider))];
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
  return (
    <div ref={list} className="model-list" onKeyDown={(e) => { if (e.key === "ArrowLeft" && provider !== null && e.target instanceof HTMLButtonElement) { e.preventDefault(); back(); } }}>
      {/* The heading carries the key to the two marks, so what they mean is on the screen and not
          only in a tooltip a touch screen never shows. */}
      <div className="menu-heading sub model-heading">
        <span className="grow">{t(browsing ? "composer.model.providers" : "session.model")}</span>
        {!browsing && <span className="model-legend"><span className="model-kind thinking" aria-hidden>✦</span> {t("composer.model.thinking")}</span>}
        {!browsing && <span className="model-legend"><span className="model-kind fast" aria-hidden>⚡</span> {t("composer.model.fast")}</span>}
      </div>
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
      {browsing && providers.map((id) => {
        const choices = entries.filter(([, p]) => p.provider === id);
        const current = choices.find(([key, p]) => isCurrent(key, p, model));
        return <button key={id} type="button" role="menuitem" className="model-row provider-row" data-provider={id} onClick={() => { setProvider(id); setQuery(""); }}>
          <ProviderMark id={id} kind={cat.providers[id]?.kind} />
          <span className="grow model-text"><span>{providerName(id, cat.providers[id])}</span><span className="sub">{current ? current[1].label || current[1].model : id}</span></span>
          <span className="sub provider-count" title={t("composer.model.count", { n:num(choices.length) })}>{num(choices.length)}</span>
          {current && <Icon name="check" size={16} />}
          <Icon name="chevron" size={14} />
        </button>;
      })}
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
