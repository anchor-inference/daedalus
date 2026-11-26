// Settings → Voice input: which transcriber turns a recording into words, what is
// tried when it fails, and the cloud endpoint's own fields under whichever of the two chose it.
//
// Self-contained on purpose: it takes the settings and a save, fetches the installed local models
// itself, and draws its own groups, so it can be moved wherever the settings end up without taking
// anything of the tab it sits in with it.

import { useEffect, useState } from "react";
import type { Settings } from "../api";
import { t } from "../i18n";
import { Select, Switch } from "../ui/primitives";
import { go } from "../ui/index";
import { pathFor } from "../router";
import { Row, SetGroup } from "../settingsrow";
import { SpeechModel, fetchSttView } from "../sttview";
import { CLOUD, transcriberChoices } from "../asrchoices";

type Asr = Settings["asr"];

export function AsrSettingsCard({ s, save }: { s: Settings; save: (patch: { asr: Partial<Asr> & Asr }) => Promise<void> | void }) {
  const asr = s.asr;
  const transcriber = asr.transcriber || CLOUD;
  const fallback = asr.fallback || "";
  const [models, setModels] = useState<SpeechModel[]>([]);
  useEffect(() => {
    let alive = true;
    fetchSttView().then((view) => { if (alive) setModels(view.models ?? []); }).catch(() => undefined);
    return () => { alive = false; };
  }, []);
  // The key is never sent back unless it is being set: an empty one means "keep what is there".
  const put = (patch: Partial<Asr>) => save({ asr: { ...asr, api_key: "", ...patch } });
  const anyLocal = models.some((m) => m.installed);
  const primary = transcriberChoices(models, transcriber);
  // The catalogue of local models is a section of its own, Recognition models; the link opens it on
  // the catalogue's row.
  const catalogue = `${pathFor("settings", "speech-models")}#stt.models`;
  const second = transcriberChoices(models, fallback, transcriber);

  // The endpoint's own fields, in a group of their own once either of the two rows has chosen it.
  const usesCloud = transcriber === CLOUD || fallback === CLOUD;

  return (
    <>
      <SetGroup className="asr-card" title={t("settings.asr.title")} help={t("settings.asr.sub")}>
        <Row data-setting="asr.transcriber" title={t("settings.asr.engine")} htmlFor="asr-transcriber" desc={!anyLocal ? <>{t("settings.asr.nolocal")} <a href={catalogue} onClick={(e) => go(e, catalogue)}>{t("settings.asr.nolocal.link")}</a></> : undefined}>
          <Select id="asr-transcriber" label={t("settings.asr.engine")} value={transcriber} invalid={primary.some((o) => o.missing && o.value === transcriber)} onChange={(next) => put({ transcriber: next, fallback: fallback === next ? "" : fallback })} options={primary.map((o) => ({ id: o.value, label: o.label }))} />
        </Row>
        <Row title={t("settings.asr.fallback")} htmlFor="asr-fallback" desc={t("settings.asr.fallback.sub")} data-setting="asr.fallback">
          <Select id="asr-fallback" label={t("settings.asr.fallback")} value={fallback} invalid={second.some((o) => o.missing && o.value === fallback)} onChange={(next) => put({ fallback: next })} options={[{ id: "", label: t("settings.asr.fallback.none") }, ...second.map((o) => ({ id: o.value, label: o.label }))]} />
        </Row>
        <Row title={t("settings.asr.autosend")} desc={t("settings.asr.autosend.sub")} data-setting="asr.autosend">
          <Switch checked={asr.autosend} onChange={(autosend) => put({ autosend })} label={t("settings.asr.autosend")} />
        </Row>
      </SetGroup>

      {usesCloud && (
        <SetGroup title={t("settings.asr.cloud")} help={t("settings.asr.cloud.sub")}>
          <Row title={t("settings.asr.provider")} htmlFor="asr-provider" data-setting="asr.provider">
            <Select
              id="asr-provider"
              label={t("settings.asr.provider")}
              value={asr.provider || ""}
              onChange={(provider) => put({ provider })}
              options={[
                { id: "", label: t("settings.asr.custom") },
                ...Object.keys(s.providers).map((pid) => ({ id: pid, label: pid, hint: s.providers[pid].base_url ? s.providers[pid].base_url.replace(/^https?:\/\//, "") : undefined })),
              ]}
            />
          </Row>
          {!asr.provider && (
            <>
              <Row title={t("settings.asr.url")} htmlFor="asr-url" data-setting="asr.url">
                <input id="asr-url" className="field" defaultValue={asr.url} placeholder="https://api.openai.com/v1" onBlur={(e) => e.target.value.trim() !== asr.url && put({ url: e.target.value.trim() })} />
              </Row>
              <Row title={t("settings.asr.key")} htmlFor="asr-key" desc={asr.api_key_set ? t("settings.asr.key.keep") : undefined} data-setting="asr.api_key">
                <input id="asr-key" className="field" type="password" defaultValue="" placeholder={asr.api_key_set ? "••••••" : ""} onBlur={(e) => e.target.value && save({ asr: { ...asr, api_key: e.target.value } })} />
              </Row>
            </>
          )}
          <Row title={t("settings.asr.model")} htmlFor="asr-model" data-setting="asr.model">
            <input id="asr-model" className="field" defaultValue={asr.model} placeholder={asr.provider === "openrouter" ? "openai/whisper-1" : "whisper-1"} onBlur={(e) => put({ model: e.target.value.trim() })} />
          </Row>
          <Row title={t("settings.asr.language")} desc={t("settings.asr.language.sub")} htmlFor="asr-language" data-setting="asr.language">
            <input id="asr-language" className="field" defaultValue={asr.language} onBlur={(e) => put({ language: e.target.value.trim() })} />
          </Row>
        </SetGroup>
      )}
    </>
  );
}
