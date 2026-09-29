// The choices the speech-to-text card offers, kept apart from the card so they can be read back
// without a page around them.

import { t } from "./i18n";
import type { SpeechModel } from "./sttview";

/** The value that names the endpoint rather than a local model. */
export const CLOUD = "cloud";

/** The choices for one select: the endpoint, then every installed model, then whatever is configured but gone. */
export function transcriberChoices(models: Pick<SpeechModel, "id" | "label" | "installed">[], current: string, exclude = ""): { value: string; label: string; missing: boolean }[] {
  const installed = models.filter((m) => m.installed && m.id !== exclude);
  const out = [
    ...(exclude === CLOUD ? [] : [{ value: CLOUD, label: t("settings.asr.engine.cloud"), missing: false }]),
    ...installed.map((m) => ({ value: m.id, label: t("settings.asr.engine.local", { name: m.label }), missing: false })),
  ];
  if (current && current !== exclude && !out.some((o) => o.value === current)) {
    // A model deleted on the Voice page stays visible as what is configured, marked, rather than the
    // select silently showing something else as chosen.
    out.push({ value: current, label: t("settings.asr.engine.missing", { name: current }), missing: true });
  }
  return out;
}
