// Settings → Chat: talking to the agent in another language. The operator writes and reads their own;
// a session set to another language has each message translated on its way to the agent and the final
// answer on its way back. The translator is a model of its own, a cheap fast one by default, because
// it runs twice a turn and a strong model there is money spent on a dictionary's work.

import { type Settings } from "../api";
import { lang, t } from "../i18n";
import { NumRow, Row } from "../settingsrow";
import { Dropdown } from "../ui/index";
import { AGENT_LANGUAGES, languageName } from "../agentlang";

type SaveProps = { s: Settings; save: (patch: Partial<Settings>) => void; presetLabel: (id: string) => string };

export const TRANSLATION_DEFAULTS: NonNullable<Settings["translation"]> = { operator_language: "ru", agent_language: "", preset: "", timeout_seconds: 120 };

export function TranslationSettings({ s, save, presetLabel }: SaveProps) {
  const current = { ...TRANSLATION_DEFAULTS, ...s.translation };
  const put = (patch: Partial<typeof current>) => {
    // `suggested` is the host's to say; sent back it would be stored as if it were a setting.
    const next = { ...current, ...patch };
    delete next.suggested;
    save({ translation: next });
  };
  const ui = lang();
  // The app's own language first, said to be that: it is the one an operator most likely means.
  const ordered = [ui, ...AGENT_LANGUAGES.filter((code) => code !== ui)];
  const presets = Object.keys(s.presets ?? {});
  const suggested = s.translation?.suggested ?? "";
  return (
    <div className="card" data-section="translation">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.translation")}</div>
      <Row title={t("settings.translation.operator")} desc={t("settings.translation.operator.sub")} stack data-setting="translation.operator_language">
        <Dropdown id="translation-operator" label={t("settings.translation.operator")} value={current.operator_language} onChange={(operator_language) => put({ operator_language })}
          options={ordered.map((code) => ({ id: code, label: code === ui ? t("settings.translation.applang", { lang: languageName(code) }) : languageName(code) }))} />
      </Row>
      <Row title={t("settings.translation.agent")} desc={t("settings.translation.agent.sub")} stack data-setting="translation.agent_language">
        <Dropdown id="translation-agent" label={t("settings.translation.agent")} value={current.agent_language} onChange={(agent_language) => put({ agent_language })}
          options={[{ id: "", label: t("agentlang.off") }, ...AGENT_LANGUAGES.filter((code) => code !== current.operator_language).map((code) => ({ id: code, label: languageName(code) }))]} />
      </Row>
      <Row title={t("settings.translation.preset")} desc={t("settings.translation.preset.sub")} stack data-setting="translation.preset">
        <Dropdown id="translation-preset" label={t("settings.translation.preset")} value={current.preset} onChange={(preset) => put({ preset })}
          options={[{ id: "", label: suggested ? t("settings.translation.preset.auto", { name: presetLabel(suggested) }) : t("settings.translation.preset.auto.plain") }, ...presets.map((p) => ({ id: p, label: presetLabel(p) }))]} />
      </Row>
      <NumRow id="translation-timeout" setting="translation.timeout_seconds" title={t("settings.translation.timeout")} desc={t("settings.translation.timeout.sub")} unit={t("settings.unit.seconds")}
        value={current.timeout_seconds} min={5} step={5} onSave={(timeout_seconds) => put({ timeout_seconds })} />
    </div>
  );
}
