// Settings → Translation: every choice of what language the agent writes and is written to in.
//
// It is a section of its own because, tucked at the foot of Chat & scheduler with the answer language
// two sections away under Working rules, nobody found it: the same concern was split in two and the
// search knew neither half by the word "translate".
//
// The answer language is the plain case: the agent answers in a language whatever you wrote in. The
// card under it is talking to the agent in another language: the operator writes and reads their own,
// and a session set to another language has each message translated on its way to the agent and the
// final answer on its way back. The translator is a model of its own, a cheap fast one by default,
// because it runs twice a turn and a strong model there is money spent on a dictionary's work.

import { type Settings } from "../api";
import { lang, t } from "../i18n";
import { ModelPicker } from "../modelitems";
import { WithEffort } from "../taskeffort";
import { NumRow, Row } from "../settingsrow";
import { Select } from "../ui/index";
import { AGENT_LANGUAGES, languageName } from "../agentlang";

type SaveProps = { s: Settings; save: (patch: Partial<Settings>) => void; presetLabel: (id: string) => string };

export const TRANSLATION_DEFAULTS: NonNullable<Settings["translation"]> = { operator_language: "ru", agent_language: "", preset: "", effort: "", fallback_preset: "", blocking_timeout_seconds: 30, timeout_seconds: 120 };

/** The answer languages the page offers; any other the configuration names is shown as it is written,
 *  since the prompt takes the name as it stands ("Always answer the operator in …"). */
const ANSWER_LANGUAGES = ["auto", "English", "Russian"];

export function TranslationSettings({ s, save, presetLabel }: SaveProps) {
  return (
    <>
      <AnswerLanguage s={s} save={save} />
      <AgentLanguage s={s} save={save} presetLabel={presetLabel} />
    </>
  );
}

/** The language of the agent's answers: the answers, not this page, which follows the app's language
 *  picker. A value the list does not name (set by SettingsChange, which takes any language) stays
 *  offered under its own name rather than shown as Automatic. */
function AnswerLanguage({ s, save }: Omit<SaveProps, "presetLabel">) {
  const value = s.answer_language || "auto";
  return (
    <div className="card" data-card="answer-language">
      <Row title={t("settings.answerlang")} desc={t("settings.answerlang.sub")} stack data-setting="answer_language">
        <Select
          id="answer-language"
          label={t("settings.answerlang")}
          value={value}
          onChange={(answer_language) => save({ answer_language })}
          options={[
            ...ANSWER_LANGUAGES.map((id) => ({ id, label: t(`settings.answerlang.${id}`) })),
            ...(ANSWER_LANGUAGES.includes(value) ? [] : [{ id: value, label: value }]),
          ]}
        />
      </Row>
    </div>
  );
}

function AgentLanguage({ s, save, presetLabel }: SaveProps) {
  const current = { ...TRANSLATION_DEFAULTS, ...s.translation };
  const put = (patch: Partial<typeof current>) => {
    // `suggested` and `fallback_suggested` are the host's to say; sent back they would be stored as if they were settings.
    const next = { ...current, ...patch };
    delete next.suggested;
    delete next.fallback_suggested;
    save({ translation: next });
  };
  const ui = lang();
  // The app's own language first, said to be that: it is the one an operator most likely means.
  const ordered = [ui, ...AGENT_LANGUAGES.filter((code) => code !== ui)];
  const suggested = s.translation?.suggested ?? "";
  const fallbackSuggested = s.translation?.fallback_suggested ?? "";
  return (
    <div className="card" data-card="agent-language">
      <div className="section-title">{t("settings.translation")}</div>
      <Row title={t("settings.translation.operator")} desc={t("settings.translation.operator.sub")} stack data-setting="translation.operator_language">
        <Select id="translation-operator" label={t("settings.translation.operator")} value={current.operator_language} onChange={(operator_language) => put({ operator_language })}
          options={ordered.map((code) => ({ id: code, label: code === ui ? t("settings.translation.applang", { lang: languageName(code) }) : languageName(code) }))} />
      </Row>
      <Row title={t("settings.translation.agent")} desc={t("settings.translation.agent.sub")} stack data-setting="translation.agent_language">
        <Select id="translation-agent" label={t("settings.translation.agent")} value={current.agent_language} onChange={(agent_language) => put({ agent_language })}
          options={[{ id: "", label: t("agentlang.off") }, ...AGENT_LANGUAGES.filter((code) => code !== current.operator_language).map((code) => ({ id: code, label: languageName(code) }))]} />
      </Row>
      {/* The same picker as every other single model in Settings, so the translator is found by name,
          provider or model id like the rest; empty is Automatic, naming the model the host would use. */}
      <Row title={t("settings.translation.preset")} desc={t("settings.translation.preset.sub")} stack data-setting="translation.preset">
        <WithEffort id="translation-effort" value={current.effort} onChange={(effort) => put({ effort })} fallback="off">
          <ModelPicker
            id="translation-preset"
            label={t("settings.translation.preset")}
            catalogue={s}
            value={current.preset}
            onChange={(preset) => put({ preset })}
            defaultOption={{ label: suggested ? t("settings.translation.preset.auto", { name: presetLabel(suggested) }) : t("settings.translation.preset.auto.plain") }}
          />
        </WithEffort>
      </Row>
      {/* The rescue when the translator is slow: asked with no thinking whatever the effort above, since it
          is there to be quick, so it has no effort of its own. Automatic names the route the host would use. */}
      <Row title={t("settings.translation.fallback")} desc={t("settings.translation.fallback.sub")} stack data-setting="translation.fallback_preset">
        <ModelPicker
          id="translation-fallback"
          label={t("settings.translation.fallback")}
          catalogue={s}
          value={current.fallback_preset ?? ""}
          onChange={(fallback_preset) => put({ fallback_preset })}
          defaultOption={{ label: fallbackSuggested ? t("settings.translation.preset.auto", { name: presetLabel(fallbackSuggested) }) : t("settings.translation.preset.auto.plain") }}
        />
      </Row>
      <NumRow id="translation-blocking-timeout" setting="translation.blocking_timeout_seconds" title={t("settings.translation.blocking")} desc={t("settings.translation.blocking.sub")} unit={t("settings.unit.seconds")}
        value={current.blocking_timeout_seconds ?? 30} min={5} step={5} onSave={(blocking_timeout_seconds) => put({ blocking_timeout_seconds })} />
      <NumRow id="translation-timeout" setting="translation.timeout_seconds" title={t("settings.translation.timeout")} desc={t("settings.translation.timeout.sub")} unit={t("settings.unit.seconds")}
        value={current.timeout_seconds} min={5} step={5} onSave={(timeout_seconds) => put({ timeout_seconds })} />
    </div>
  );
}
