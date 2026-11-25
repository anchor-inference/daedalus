// The model the history summariser runs on, chosen in Settings → Context.
//
// It sits apart from the settings screen so the one surprising rule can be tested on its own: a
// preset that was chosen here and later deleted stays in the configuration (the runtime quietly
// summarises with the session's own model instead), so the picker must still show it — as missing —
// rather than silently displaying the first option and leaving the operator to believe it is in use.

import { t } from "./i18n";
import { ModelPicker, type ModelCatalogue } from "./modelitems";
import { Row } from "./settingsrow";
import { WithEffort } from "./taskeffort";

/** Whether the chosen summary model is one that no longer exists. */
export function compactionMissing(presets: ModelCatalogue["presets"] | undefined, current: string): boolean {
  return !!current && !(current in (presets ?? {}));
}

export function CompactionModelSelect({ catalogue, value, onSave, effort, onEffort }: { catalogue: ModelCatalogue; value: string; onSave: (preset: string) => void; effort?: string; onEffort: (effort: string) => void }) {
  const missing = compactionMissing(catalogue.presets, value);
  return (
    <Row
      title={t("settings.compaction.model")}
      desc={<>{missing && <span className="attn">{t("settings.compaction.model.missing.hint")} </span>}{t("settings.compaction.model.hint")}</>}
      stack
      data-setting="compaction.preset"
    >
      <WithEffort id="compaction-effort" value={effort} onChange={onEffort} fallback="off">
        <ModelPicker id="compaction-preset" label={t("settings.compaction.model")} catalogue={catalogue} value={value} onChange={onSave} defaultOption={{ label: t("settings.compaction.model.own") }} />
      </WithEffort>
    </Row>
  );
}
