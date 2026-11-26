// The model, chosen from inside the composer. The chip names the model in use and its reasoning; the
// picker is one box of a fixed size: a search, a strip of tabs (Favourites, then one per provider),
// six rows of that tab's models, the reasoning as one row of choices, and the way to the settings.
// Search spans every provider. While another model stands in for the configured one the chip is in
// the attention colour and says so, and the list offers the way back first.
//
// The box never changes size. It used to: a provider list that grew upward to 79 % of the window, a
// search that reached 93 %, a width that jumped from 288 to 248 px as a provider was opened, and an
// effort panel pushed against the window's foot, away from the row it belonged to.

import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { api, ModelFallback, Preset, ProviderConf } from "./api";
import { MenuRow, Popover, Segmented, toast } from "./ui/primitives";
import { REASONING_EFFORTS, effortIndex } from "./models";
import { Icon } from "./icons";
import { errorText } from "./ui";
import { shortModel, tokens } from "./format";
import { DICT, num, t } from "./i18n";
import { ProviderMark, providerName } from "./ui/provider-mark";
import { effortWord } from "./effortselect";
import { SubscriptionAccount, accountName, statusOf, useSubscriptionAccounts } from "./accounts";
import { navigate, pathFor } from "./router";

export type ModelChoice = { clear: true } | { preset: string } | { model: string };

/** The subscription account of the session's model, as the host reports it with the effort. */
export type AccountChoice = {
  /** The key-proxy upstream the model goes through; "" for a model with no accounts, which shows no row. */
  upstream: string;
  /** The account the operator chose for this session; "" while it follows the model's own. */
  chosen: string;
  /** The account "the model's own" is today, named beside "default". */
  fallback: string;
  onChoose: (account: string) => void;
};

/** Where "Add model" leads: Settings → Models with its add sheet already open. A model
 *  typed by hand into the picker used to stand in for this, and it ran a session on a provider/model
 *  pair nothing had described, with no price, no context window and no way to pick it again. */
export const ADD_MODEL_PATH = pathFor("settings", "models", { add: "1" });

/** The picker's one width; its height is the stylesheet's (`.model-picker`), the same in every state. */
export const PICKER_WIDTH = 336;

const MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

type Catalogue = { presets: Record<string, Preset>; providers: Record<string, ProviderConf>; global: string; globalId: string };

export type ModelSelectProps = {
  /** The model the session is set to, as the host names it. */
  model: string;
  fallback: ModelFallback | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onChoose: (choice: ModelChoice) => void;
  effort?: string;
  thinking?: boolean;
  onChooseEffort?: (effort: string) => void;
  account?: AccountChoice;
  /** A control elsewhere that opened the list, such as the panel strip's model chip: the popover hangs
   *  below it, aligned to its left edge. Null hangs it from this component's own chip. */
  anchor?: HTMLElement | null;
  /** Whether the chip itself is drawn; the list can still be opened from an outside anchor without it. */
  pill?: boolean;
};

/** Whether a preset is the one the session is set to: by its label, or by its provider/model pair. */
function isCurrent(id: string, p: Preset, model: string): boolean {
  if (!model) return false;
  return model === id || model === p.label || model === p.model || model === `${p.provider}/${p.model}`;
}

export function ModelSelect({ model, fallback, open, onOpenChange, onChoose, effort, thinking, onChooseEffort, account, anchor = null, pill = true }: ModelSelectProps) {
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
        setCat({ presets, providers: st.providers ?? {}, globalId, global: def ? def.label || `${def.provider}/${def.model}` : globalId || t("settings.heartbeat.default") });
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
  // A star answers at once and is written through the preset's own route; the next read of the
  // settings brings the host's word, and a refusal puts the star back.
  const star = (id: string, on: boolean) => {
    setCat((c) => (c && c.presets[id] ? { ...c, presets: { ...c.presets, [id]: { ...c.presets[id], favourite: on } } } : c));
    api.put(`/api/presets/${encodeURIComponent(id)}`, { favourite: on }).catch((e) => {
      setCat((c) => (c && c.presets[id] ? { ...c, presets: { ...c.presets, [id]: { ...c.presets[id], favourite: !on } } } : c));
      toast(errorText(e));
    });
  };
  // While a fallback answers, the chip names the stand-in and then the configured model behind an
  // arrow, each in its own span. It used to be one sentence, "via DeepSeek Flash (fallback from
  // Claude Opus 5)", which a phone cut to "via DeepSeek Fla…" and lost the configured model entirely.
  const label = fallback ? (
    <>
      <span className="model-to truncate">{shortModel(fallback.to, 18)}</span>
      <span className="model-from truncate">{t("session.model.fallback", { from: shortModel(fallback.from, 18) })}</span>
    </>
  ) : shortModel(model, 22);
  const reason = fallback ? (DICT[`session.model.reason.${fallback.reason}`] ? t(`session.model.reason.${fallback.reason}`) : fallback.reason) : "";
  const title = fallback ? `${t("session.model.fallback.turn", { to: fallback.to, from: fallback.from })} — ${reason}` : t("composer.model.title");
  const ownOpen = open && !anchor;
  return (
    <>
      {pill && <button ref={trigger} type="button" className={`model-select ${fallback ? "attn" : ""} ${ownOpen ? "on" : ""}`} onClick={() => onOpenChange(!ownOpen)} title={title} aria-label={t("session.model.for")} aria-haspopup="dialog" aria-expanded={ownOpen}>
        {fallback && <span className="model-dot" aria-hidden />}
        {/* The effort is its own span so a long model name is what gives way: in one span the
            ellipsis ate the effort first and the chip read "DeepSeek Flash · l…". */}
        <span className={`model-label truncate ${fallback ? "model-fallback" : ""}`}>{label}</span>
        {onChooseEffort && <span className="model-effort">{effortWord(effort, thinking)}</span>}
        <Icon name="chevron" size={14} />
      </button>}
      {open && (
        <Popover anchor={anchor ?? trigger.current} onClose={() => onOpenChange(false)} className="model-picker" align={anchor ? "start" : "end"} width={PICKER_WIDTH} label={t("session.model.for")}>
          <ModelPicker cat={cat} failed={failed} model={model} fallback={fallback} onPick={pick} onStar={star} onClose={() => onOpenChange(false)}
            effort={effort} thinking={thinking} onChooseEffort={onChooseEffort} account={account?.upstream ? account : undefined} />
        </Popover>
      )}
    </>
  );
}

/** One row of the list: a model, the configured default, or the way back from a stand-in. */
type Item = { key: string; label: string; meta: string; title: string; preset?: string; current: boolean; favourite?: boolean; restore?: boolean; onPick: () => void };

/** What a provider needs before it answers, in a word: a plan, a key, nothing (a local server). */
function keyState(conf: ProviderConf | undefined): string {
  if (!conf) return "";
  if (conf.kind === "local" || conf.kind === "llamacpp" || conf.kind === "vllm" || conf.kind === "ollama") return t("composer.model.nokey");
  if (conf.billing === "subscription") return t("composer.model.subscription");
  return t(conf.api_key_set ? "composer.model.keyready" : "composer.model.keymissing");
}

const FAVOURITES = "favourites";
const FREE = "free";

function ModelPicker({ cat, failed, model, fallback, onPick, onStar, onClose, effort, thinking, onChooseEffort, account }: {
  cat: Catalogue | null; failed: string | null; model: string; fallback: ModelFallback | null; onPick: (c: ModelChoice) => void; onStar: (id: string, on: boolean) => void; onClose: () => void;
  effort?: string; thinking?: boolean; onChooseEffort?: (value: string) => void; account?: AccountChoice;
}) {
  const box = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const strip = useRef<HTMLDivElement>(null);
  const listId = useId();
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const entries = useMemo(() => Object.entries(cat?.presets ?? {}), [cat]);
  const currentEntry = entries.find(([id, p]) => isCurrent(id, p, model)) ?? null;
  // The tabs: Favourites first, then each provider in the order the catalogue names them, the free
  // models last as a tab of their own (they are tried in turn, not run on one provider).
  const providers = useMemo(() => [...new Set(entries.filter(([, p]) => !p.free_only).map(([, p]) => p.provider))], [entries]);
  const hasFree = entries.some(([, p]) => p.free_only);
  const favourites = entries.filter(([, p]) => p.favourite);
  // The tab it opens on: Favourites when there are any, else the provider of the model in use.
  const opening = favourites.length ? FAVOURITES : currentEntry && !currentEntry[1].free_only ? currentEntry[1].provider : providers[0] ?? FAVOURITES;
  const shown = tab ?? opening;
  const searching = query.trim() !== "";
  const configured = fallback ? (entries.find(([id, p]) => isCurrent(id, p, fallback.from)) ?? null) : null;

  const items: Item[] = useMemo(() => {
    if (!cat) return [];
    const out: Item[] = [];
    if (fallback) {
      out.push({ key: "restore", restore: true, current: false, label: t("composer.model.restore", { model: shortModel(fallback.from, 28) }), meta: "", title: t("composer.model.configured", { model: fallback.from }),
        onPick: () => onPick(configured ? { preset: configured[0] } : { model: fallback.from }) });
    }
    const row = ([id, p]: [string, Preset], mixed: boolean): Item => ({
      key: id, preset: id, current: isCurrent(id, p, model), favourite: !!p.favourite, label: p.label || p.model,
      // Where the rows come from several providers (Favourites, a search) the row says how it is paid
      // for, which also tells the two Anthropic routes apart; inside a provider it says the window.
      meta: mixed ? (p.free_only ? t("composer.model.free.short") : keyState(cat.providers[p.provider]) || providerName(p.provider, cat.providers[p.provider])) : p.context_window > 0 ? tokens(p.context_window) : "",
      title: `${providerName(p.provider, cat.providers[p.provider])} · ${p.provider}/${p.model}${p.context_window > 0 ? ` · ${t("composer.model.context", { n: num(p.context_window) })}` : ""}`,
      onPick: () => onPick({ preset: id }),
    });
    const global: Item = { key: "global", current: false, label: t("session.model.global"), meta: shortModel(cat.global, 18), title: cat.global, onPick: () => onPick({ clear: true }) };
    if (searching) {
      const needle = query.trim().toLocaleLowerCase();
      for (const entry of entries) {
        const [id, p] = entry;
        if (`${id} ${p.label} ${p.provider} ${providerName(p.provider, cat.providers[p.provider])} ${p.model}`.toLocaleLowerCase().includes(needle)) out.push(row(entry, true));
      }
      return out;
    }
    const defaultProvider = cat.presets[cat.globalId]?.provider;
    if (shown === FAVOURITES) {
      // The configured default where no provider tab can offer it: it is not one of the presets.
      if (cat.globalId && !cat.presets[cat.globalId]) out.push(global);
      for (const entry of favourites) out.push(row(entry, true));
    } else if (shown === FREE) {
      for (const entry of entries.filter(([, p]) => p.free_only)) out.push(row(entry, true));
    } else {
      if (defaultProvider === shown && !cat.presets[cat.globalId]?.free_only) out.push(global);
      for (const entry of entries.filter(([, p]) => p.provider === shown && !p.free_only)) out.push(row(entry, false));
    }
    return out;
  }, [cat, entries, favourites, shown, searching, query, model, fallback, configured, onPick]);

  // Only the keyboard scrolls the list to the highlighted row. Under the pointer a row half out of the
  // box would scroll up as it was pressed, and the release would land on the row that slid there.
  const byKeys = useRef(true);
  // The highlighted row follows the model in use whenever the list changes under it, else the first.
  const listKey = `${shown}\u0000${query}\u0000${items.length}`;
  useLayoutEffect(() => {
    const current = items.findIndex((item) => item.current);
    byKeys.current = true;
    setActive(searching ? 0 : Math.max(0, current));
  }, [listKey]);
  useLayoutEffect(() => {
    if (!byKeys.current) return;
    reveal(list.current, list.current?.querySelector<HTMLElement>(`[data-index="${active}"]`), "y");
  }, [active, listKey]);
  // The chosen tab stays in sight when the strip scrolls: fourteen providers do not fit in 336 px.
  useLayoutEffect(() => {
    reveal(strip.current, strip.current?.querySelector<HTMLElement>("[aria-selected='true']"), "x");
  }, [shown, searching]);
  // The popover gives the keyboard to its first control once it is placed, and on a page's first open
  // that was before the models had arrived; once they arrive the search takes it, unless the reader
  // has already moved. A phone's sheet is left alone: a focused field there raises the keyboard.
  const loaded = !!cat;
  useEffect(() => {
    if (!loaded || inSheet(box.current) || box.current?.contains(document.activeElement)) return;
    search.current?.focus({ preventScroll: true });
  }, [loaded]);

  const tabs = [FAVOURITES, ...providers, ...(hasFree ? [FREE] : [])];
  const choose = (next: string) => {
    setTab(next);
    setQuery("");
    if (!inSheet(box.current)) search.current?.focus({ preventScroll: true });
  };
  const tabName = (id: string) => (id === FAVOURITES ? t("composer.model.favourites") : id === FREE ? t("composer.model.free") : providerName(id, cat?.providers[id]));
  const heading = searching
    ? items.filter((item) => !item.restore).length ? t("composer.model.found", { n: String(items.filter((item) => !item.restore).length) }) : t("shell.search.nomatch")
    : shown === FAVOURITES || shown === FREE ? tabName(shown) : [tabName(shown), keyState(cat?.providers[shown])].filter(Boolean).join(" · ");
  const empty = !cat ? (failed ?? t("common.loading")) : items.length ? "" : searching ? "" : shown === FAVOURITES ? t("composer.model.favourites.empty") : t("shell.search.nomatch");
  const activeItem = items[active];

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.nativeEvent.isComposing) return;
    if (e.key === "Tab") {
      // The keyboard goes round the box rather than out of it (the popover would close on Tab):
      // the search, the reasoning, the account, the way to the settings, and back to the search.
      const stops = Array.from(box.current?.querySelectorAll<HTMLElement>("[data-stop]:not(:disabled)") ?? []);
      const at = stops.findIndex((el) => el === document.activeElement || el.contains(document.activeElement));
      const next = stops[(at + (e.shiftKey ? -1 : 1) + stops.length) % stops.length];
      e.preventDefault();
      e.stopPropagation();
      focusStop(next);
      return;
    }
    if (e.target !== search.current) {
      // The reasoning walks its own choices with the arrows; the popover's list walk must not take them.
      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key)) e.stopPropagation();
      return;
    }
    const last = items.length - 1;
    const move = (to: number) => { e.preventDefault(); byKeys.current = true; setActive(Math.max(0, Math.min(last, to))); };
    if (e.key === "ArrowDown") move(active + 1);
    else if (e.key === "ArrowUp") move(active - 1);
    else if (e.key === "PageDown") move(active + 6);
    else if (e.key === "PageUp") move(active - 6);
    else if (e.key === "Enter") { e.preventDefault(); activeItem?.onPick(); }
    else if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !searching) {
      // An empty search has no caret to move, so the side arrows walk the tabs.
      e.preventDefault();
      const i = tabs.indexOf(shown);
      choose(tabs[(i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length]);
    } else if (e.key === "*" && !searching && activeItem?.preset) {
      e.preventDefault();
      onStar(activeItem.preset, !activeItem.favourite);
    }
  }

  const efforts = onChooseEffort ? <EffortRow presetThinks={currentEntry ? currentEntry[1].thinking !== false : true} effort={effort} thinking={thinking} onChoose={onChooseEffort} account={account} /> : null;
  const settingsKey = MAC ? "⌘ ," : "Ctrl ,";
  return (
    <div ref={box} className="model-picker-body" onKeyDown={onKeyDown}>
      <label className="model-picker-search">
        <Icon name="search" size={16} />
        <input ref={search} data-stop className="model-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("composer.model.search")} aria-label={t("composer.model.search")}
          role="combobox" aria-expanded="true" aria-controls={listId} aria-autocomplete="list" aria-activedescendant={activeItem ? `${listId}-${active}` : undefined} />
      </label>
      <div className="model-picker-tabs">
        <div ref={strip} className="model-picker-tablist" role="tablist" aria-label={t("composer.model.providers")}>
          {tabs.map((id) => {
            const on = !searching && id === shown;
            const name = tabName(id);
            return <button key={id} type="button" role="tab" tabIndex={-1} aria-selected={on} className={`model-tab ${on ? "on" : ""}`} data-provider={id === FAVOURITES || id === FREE ? undefined : id} data-tab={id === FAVOURITES || id === FREE ? id : undefined}
              title={name} aria-label={name} onClick={() => choose(id)}>
              {id === FAVOURITES ? <Icon name="star" size={16} /> : id === FREE ? <Icon name="gift" size={16} /> : <ProviderMark id={id} kind={cat?.providers[id]?.kind} />}
            </button>;
          })}
        </div>
        <button type="button" tabIndex={-1} className="model-tab model-add" title={t("composer.model.add")} aria-label={t("composer.model.add")} onClick={() => { onClose(); navigate(ADD_MODEL_PATH); }}>
          <Icon name="plus" size={14} />
        </button>
      </div>
      <div className="model-picker-title" role="status">{heading}</div>
      <div ref={list} id={listId} className="model-picker-list" role="listbox" aria-label={heading}>
        {items.map((item, i) => (
          <div key={item.key} id={`${listId}-${i}`} data-index={i} data-preset={item.preset} role="option" aria-selected={item.current}
            className={`model-row ${item.current ? "on" : ""} ${i === active ? "active" : ""} ${item.restore ? "restore" : ""}`}
            title={item.title} onPointerMove={() => { if (active !== i) { byKeys.current = false; setActive(i); } }} onClick={item.onPick}>
            {item.restore && <Icon name="undo" size={14} />}
            <span className="model-row-name">{item.label}</span>
            {item.meta && <span className="model-row-meta">{item.meta}</span>}
            {item.preset && (
              <button type="button" tabIndex={-1} className={`model-star ${item.favourite ? "on" : ""}`} aria-pressed={item.favourite}
                aria-label={t(item.favourite ? "composer.model.unstar" : "composer.model.star")} title={t(item.favourite ? "composer.model.unstar" : "composer.model.star")}
                onClick={(e) => { e.stopPropagation(); onStar(item.preset!, !item.favourite); }}>
                <Icon name="star" size={14} />
              </button>
            )}
            <span className="model-row-check" aria-hidden="true">{item.current && <Icon name="check" size={16} />}</span>
          </div>
        ))}
        {empty && <div className="model-picker-empty" role="status">{empty}</div>}
      </div>
      {efforts}
      <button type="button" data-stop className="model-picker-foot" onClick={() => { onClose(); navigate(pathFor("settings", "models")); }}>
        <span className="grow">{t("composer.model.settings")}</span>
        <kbd className="kbd">{settingsKey}</kbd>
      </button>
    </div>
  );
}

/** Scroll a list just enough to show one of its items. `scrollIntoView` would scroll every scrolled
 *  ancestor too, the conversation under the popover among them. The list is its items' offset parent. */
function reveal(box: HTMLElement | null, item: HTMLElement | null | undefined, axis: "x" | "y") {
  if (!box || !item) return;
  const start = axis === "x" ? item.offsetLeft : item.offsetTop;
  const end = start + (axis === "x" ? item.offsetWidth : item.offsetHeight);
  const from = axis === "x" ? box.scrollLeft : box.scrollTop;
  const size = axis === "x" ? box.clientWidth : box.clientHeight;
  const to = start < from ? start : end > from + size ? end - size : from;
  if (to === from) return;
  if (axis === "x") box.scrollLeft = to;
  else box.scrollTop = to;
}

function inSheet(el: HTMLElement | null): boolean {
  return !!el?.closest(".sheet");
}

/** A stop of the box's own Tab ring: a field or button takes the focus; the reasoning gives it to its chosen choice. */
function focusStop(el: HTMLElement | undefined) {
  if (!el) return;
  if (el.matches("input, button")) { el.focus({ preventScroll: true }); return; }
  (el.querySelector<HTMLElement>("[aria-checked='true']:not(:disabled)") ?? el.querySelector<HTMLElement>("button:not(:disabled)"))?.focus({ preventScroll: true });
}

/**
 * The reasoning as one row of choices: the efforts the host takes, "Off" first where the model in use
 * reasons (it can then be told not to). A model that does not reason has nothing to set, and the row
 * says so in its place rather than offering choices it would ignore.
 */
function EffortRow({ presetThinks, effort, thinking, onChoose, account }: { presetThinks: boolean; effort?: string; thinking?: boolean; onChoose: (value: string) => void; account?: AccountChoice }) {
  const value = thinking ? REASONING_EFFORTS[effortIndex(effort)] : "off";
  const options = [...(presetThinks ? ["off"] : []), ...REASONING_EFFORTS].map((id) => ({ id, label: t(id === "off" ? "add.effort.off" : `add.effort.${id}`) }));
  return (
    <div className="model-picker-effort">
      <div className="model-picker-label">
        <span className="grow">{t("composer.effort.short")}</span>
        {account && <AccountChip choice={account} />}
      </div>
      {presetThinks || thinking
        ? <div data-stop className="model-effort-stop"><Segmented className="model-effort-seg" label={t("composer.effort")} value={value} options={options} onChange={onChoose} /></div>
        : <div className="model-effort-none sub">{t("composer.effort.none")}</div>}
    </div>
  );
}

/** The accounts of the session's subscription, names only: the quota reading asks every vendor and the picker cannot wait for it. */
function useUpstreamAccounts(upstream: string): SubscriptionAccount[] {
  const { data } = useSubscriptionAccounts(false);
  return data?.upstreams.find((row) => row.upstream === upstream)?.accounts ?? [];
}

function chosenName(choice: AccountChoice, accounts: SubscriptionAccount[]): string {
  const chosen = accounts.find((a) => a.id === choice.chosen);
  if (chosen) return accountName(chosen);
  const own = accounts.find((a) => a.id === choice.fallback) ?? accounts.find((a) => a.default);
  return t("composer.account.default", { name: own ? accountName(own) : choice.fallback });
}

/**
 * The subscription account, on the reasoning's label line so the box keeps its size: nothing while the
 * subscription has one account, else the account in use, which opens the accounts beside the box —
 * "default" first, which follows Settings, then each account with what keeps it from answering.
 */
function AccountChip({ choice }: { choice: AccountChoice }) {
  const accounts = useUpstreamAccounts(choice.upstream);
  const button = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  if (accounts.length < 2) return null;
  const rows: { id: string; text: string; hint: ReactNode }[] = [{ id: "", text: chosenName({ ...choice, chosen: "" }, accounts), hint: t("composer.account.default.hint") },
    ...accounts.map((a) => { const status = statusOf(a); return { id: a.id, text: accountName(a), hint: status.tone === "ok" ? a.plan : status.text }; })];
  return (
    <span data-branch="account" className="model-account">
      <button ref={button} type="button" data-stop className="model-account-chip" aria-haspopup="menu" aria-expanded={open} title={t("composer.account.heading")} onClick={() => setOpen((o) => !o)}>
        <Icon name="user" size={14} /><span className="truncate">{chosenName(choice, accounts)}</span><Icon name="chevron" size={12} />
      </button>
      {open && (
        <Popover anchor={button.current} onClose={() => setOpen(false)} className="account-menu" side="right" align="end" label={t("composer.account.heading")}>
          <div className="menu-group">{t("composer.account.heading")}</div>
          {rows.map((row) => <MenuRow key={row.id || "default"} label={row.text} sub={row.hint || undefined} checked={row.id === choice.chosen} data={{ account: row.id || "default" }}
            onSelect={() => { setOpen(false); choice.onChoose(row.id); }} />)}
        </Popover>
      )}
    </span>
  );
}
