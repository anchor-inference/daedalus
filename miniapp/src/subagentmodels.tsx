// Which models a session's helpers may run on: every model, only the session's own, or a chosen few.
// One control in two places. In a session's Details it is that session's choice, with a way back to the
// default; in Settings it is the default itself. Self-contained, so it can move with either.

import { useEffect, useState } from "react";
import { api, SubagentModelsChoice, SubagentModelsMode, SubagentModelsView } from "./api";
import { t } from "./i18n";
import { MultiDropdown, Segmented } from "./ui/index";

const MODES: SubagentModelsMode[] = ["all", "session", "chosen"];

/** The three-way choice and, for "chosen", the list. Saves on every change. */
export function SubagentModelsPicker({ value, options, onChange, disabled }: {
  value: SubagentModelsChoice;
  options: { id: string; label: string }[];
  onChange: (next: SubagentModelsChoice) => void;
  disabled?: boolean;
}) {
  return (
    <div className="subagent-models">
      <Segmented
        label={t("subm.title")}
        value={value.mode}
        options={MODES.map((mode) => ({ id: mode, label: t(`subm.mode.${mode}`) }))}
        onChange={(mode) => { if (!disabled) onChange({ mode, presets: mode === "chosen" ? value.presets : [] }); }}
      />
      {value.mode === "chosen" && (
        <MultiDropdown
          label={t("subm.chosen")}
          none={t("subm.none")}
          values={value.presets.filter((id) => options.some((o) => o.id === id))}
          options={options}
          disabled={disabled}
          onChange={(presets) => onChange({ mode: "chosen", presets })}
        />
      )}
    </div>
  );
}

/** The session's own choice, read and saved through its endpoint. */
export function SessionSubagentModels({ sessionId, toast }: { sessionId: string; toast: (text: string) => void }) {
  const [view, setView] = useState<SubagentModelsView | null>(null);
  const url = `/api/sessions/${encodeURIComponent(sessionId)}/subagent-models`;
  // An answer of any other shape leaves the section out rather than taking the whole Details down with it.
  const take = (v: SubagentModelsView) => setView(Array.isArray(v?.allowed) && Array.isArray(v?.options) ? v : null);
  useEffect(() => {
    let current = true;
    api.get<SubagentModelsView>(url).then((v) => { if (current) take(v); }).catch(() => undefined);
    return () => { current = false; };
  }, [url]);
  if (!view) return null;
  async function save(body: { mode: SubagentModelsMode | null; presets: string[] }) {
    try { take(await api.put<SubagentModelsView>(url, body)); } catch (e) { toast((e as Error).message); }
  }
  const named = (ids: string[]) => ids.map((id) => view.options.find((o) => o.id === id)?.label ?? id).join(", ");
  return (
    <div className="dt-subagent-models" data-setting="session.subagent_models">
      <div className="dt-control">
        <span className="dt-subagent-models-title grow">{t("subm.title")}</span>
        {view.inherited
          ? <span className="sub">{t("subm.inherited")}</span>
          : <button className="linkbtn" onClick={() => void save({ mode: null, presets: [] })}>{t("subm.reset")}</button>}
      </div>
      <SubagentModelsPicker value={view} options={view.options} onChange={(next) => void save(next)} />
      <div className="sub faint">
        {view.from_parent ? t("subm.parent") + " " : ""}
        {view.allowed.length ? t("subm.allowed", { models: named(view.allowed) }) : view.mode === "session" ? t("subm.ownonly") : t("subm.nothing")}
      </div>
    </div>
  );
}
