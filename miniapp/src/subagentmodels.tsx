// Which models a session's helpers may run on: every model, only the session's own, or a chosen few.
// One control in two places. In a session's Details it is that session's choice, with a way back to the
// default; in Settings it is the default itself. Self-contained, so it can move with either.
//
// It was a three-way switch over a dropdown whose closed face joined the chosen names with commas and
// an "Allowed: …" line that did the same; past three models both were cut off, the list had no search
// and named a model by nothing but its label. The three ways are now labelled choices that say what
// each one allows, and "chosen" opens the searchable list with every model's provider, window and price.

import { useEffect, useMemo, useState } from "react";
import { api, Preset, PriceView, ProviderConf, QuickModelsView, SubagentModelsChoice, SubagentModelsMode, SubagentModelsView } from "./api";
import { plural, t } from "./i18n";
import { modelItems } from "./modelitems";
import { InheritNote, SelectionList, type InheritState, type SelectionGroup, type SelectionItem } from "./ui/index";

const MODES: SubagentModelsMode[] = ["all", "session", "chosen"];

/** The three ways and, for "chosen", the list. Saves on every change. `own` names the session's model
 *  where there is one session to speak of. */
export function SubagentModelsPicker({ value, items, groups, onChange, disabled, own, inherit }: {
  value: SubagentModelsChoice;
  items: SelectionItem[];
  groups?: SelectionGroup[];
  onChange: (next: SubagentModelsChoice) => void;
  disabled?: boolean;
  own?: string;
  inherit?: InheritState;
}) {
  const known = value.presets.filter((id) => items.some((item) => item.id === id)).length;
  const explain: Record<SubagentModelsMode, string> = {
    all: plural("subm.mode.all.sub", items.length),
    session: own ? t("subm.mode.session.named", { model: own }) : t("subm.mode.session.sub"),
    chosen: value.mode === "chosen" ? plural("subm.mode.chosen.n", known) : t("subm.mode.chosen.sub"),
  };
  return (
    <div className="subagent-models">
      {inherit && <InheritNote inherit={inherit} disabled={disabled} />}
      <div className="subm-modes" role="radiogroup" aria-label={t("subm.title")}>
        {MODES.map((mode) => (
          <label key={mode} className={`subm-mode ${value.mode === mode ? "on" : ""}`}>
            <input type="radio" name={`subm-${own ?? "default"}`} checked={value.mode === mode} disabled={disabled}
              onChange={() => onChange({ mode, presets: mode === "chosen" ? value.presets : [] })} />
            <span className="sel-text">
              <span className="sel-name">{t(`subm.mode.${mode}`)}</span>
              <span className="sel-detail">{explain[mode]}</span>
            </span>
          </label>
        ))}
      </div>
      {value.mode === "chosen" && (
        <SelectionList
          label={t("subm.chosen")}
          placeholder={t("subm.search")}
          items={items}
          groups={groups}
          value={value.presets}
          disabled={disabled}
          chosenLabel={t("subm.chosen.list")}
          emptyChosen={t("subm.nothing")}
          onChange={(presets) => onChange({ mode: "chosen", presets })}
        />
      )}
    </div>
  );
}

/** The models QuickAgent may run on, in order: the first is the one it takes when the agent names none,
 *  and an empty list turns the tool off. Ordered, because the first one is the default. */
export function QuickModelsPicker({ value, items, groups, onChange, disabled, inherit }: {
  value: string[];
  items: SelectionItem[];
  groups?: SelectionGroup[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  inherit?: InheritState;
}) {
  return (
    <div className="quick-models">
      {inherit && <InheritNote inherit={inherit} disabled={disabled} />}
      <SelectionList
        id="quick-models"
        label={t("qagent.title")}
        placeholder={t("subm.search")}
        ordered
        items={items}
        groups={groups}
        value={value}
        disabled={disabled}
        chosenLabel={t("qagent.chosen")}
        emptyChosen={t("qagent.none")}
        onChange={onChange}
      />
    </div>
  );
}

type Catalogue = { presets?: Record<string, Preset>; providers?: Record<string, ProviderConf>; prices?: Record<string, PriceView> };

/** The session's own quick-agent models, read and saved through its endpoint; beside the helper models
 *  in Details because both say which models the agent may hand work to. */
export function SessionQuickModels({ sessionId, toast, catalogue }: { sessionId: string; toast: (text: string) => void; catalogue: Catalogue | null }) {
  const [view, setView] = useState<QuickModelsView | null>(null);
  const url = `/api/sessions/${encodeURIComponent(sessionId)}/quick-models`;
  const take = (v: QuickModelsView) => setView(Array.isArray(v?.presets) && Array.isArray(v?.options) ? v : null);
  useEffect(() => {
    let current = true;
    api.get<QuickModelsView>(url).then((v) => { if (current) take(v); }).catch(() => undefined);
    return () => { current = false; };
  }, [url]);
  const { items, groups } = useMemo(() => {
    if (!view) return { items: [], groups: [] };
    const presets = catalogue?.presets ?? {};
    const rich = modelItems({ presets, providers: catalogue?.providers, prices: catalogue?.prices }, (id) => view.options.some((o) => o.id === id));
    const plain = view.options.filter((o) => !(o.id in presets)).map((o) => ({ id: o.id, label: o.label }));
    return { items: [...plain, ...rich.items], groups: rich.groups };
  }, [view, catalogue]);
  if (!view) return null;
  async function save(presets: string[] | null) {
    try { take(await api.put<QuickModelsView>(url, { presets })); } catch (e) { toast((e as Error).message); }
  }
  const fallback = view.default.length ? plural("subm.mode.chosen.n", view.default.length) : t("qagent.off");
  return (
    <div className="dt-quick-models" data-setting="session.quick_models">
      <div className="section-title">{t("qagent.title")}</div>
      <p className="dt-hint">{t("qagent.session.sub")}</p>
      <QuickModelsPicker
        value={view.presets}
        items={items}
        groups={groups}
        inherit={{ active: view.inherited, label: t("subm.default.is", { what: fallback }), onUse: () => void save(null) }}
        onChange={(next) => void save(next)}
      />
    </div>
  );
}

/** The session's own choice, read and saved through its endpoint. */
export function SessionSubagentModels({ sessionId, toast }: { sessionId: string; toast: (text: string) => void }) {
  const [view, setView] = useState<SubagentModelsView | null>(null);
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const url = `/api/sessions/${encodeURIComponent(sessionId)}/subagent-models`;
  // An answer of any other shape leaves the section out rather than taking the whole Details down with it.
  const take = (v: SubagentModelsView) => setView(Array.isArray(v?.allowed) && Array.isArray(v?.options) ? v : null);
  useEffect(() => {
    let current = true;
    api.get<SubagentModelsView>(url).then((v) => { if (current) take(v); }).catch(() => undefined);
    // The models' providers, windows and prices come from the settings; without them the rows still
    // name every model the session may choose, by its label alone.
    api.get<Catalogue>("/api/settings").then((s) => { if (current) setCatalogue(s); }).catch(() => undefined);
    return () => { current = false; };
  }, [url]);
  const { items, groups } = useMemo(() => {
    if (!view) return { items: [], groups: [] };
    const presets = catalogue?.presets ?? {};
    const rich = modelItems({ presets, providers: catalogue?.providers, prices: catalogue?.prices }, (id) => view.options.some((o) => o.id === id));
    const plain = view.options.filter((o) => !(o.id in presets)).map((o) => ({ id: o.id, label: o.label }));
    return { items: [...plain, ...rich.items], groups: rich.groups };
  }, [view, catalogue]);
  if (!view) return null;
  async function save(body: { mode: SubagentModelsMode | null; presets: string[] }) {
    try { take(await api.put<SubagentModelsView>(url, body)); } catch (e) { toast((e as Error).message); }
  }
  const nameOf = (id: string | null) => (id ? items.find((item) => item.id === id)?.label ?? id : "");
  const fallback = view.default.mode === "chosen" ? plural("subm.mode.chosen.n", view.default.presets.length) : t(`subm.mode.${view.default.mode}`);
  return (
    <>
    <div className="dt-subagent-models" data-setting="session.subagent_models">
      <SubagentModelsPicker
        value={view}
        items={items}
        groups={groups}
        own={nameOf(view.session_preset) || undefined}
        inherit={{ active: view.inherited, label: t("subm.default.is", { what: fallback }), onUse: () => void save({ mode: null, presets: [] }) }}
        onChange={(next) => void save(next)}
      />
      {view.from_parent && <div className="sub faint">{t("subm.parent")}</div>}
    </div>
    {!view.from_parent && <SessionQuickModels sessionId={sessionId} toast={toast} catalogue={catalogue} />}
    </>
  );
}
