// Settings → Voice & speech → voice notes: which transcriber turns a recording into words, what is
// tried when it fails, and the cloud endpoint's own fields under whichever of the two chose it.
//
// Self-contained on purpose: it takes the settings and a save, fetches the installed local models
// itself, and draws one card, so it can be moved wherever the settings end up without taking
// anything of the tab it sits in with it.

import { useEffect, useState } from "react";
import type { Settings } from "../api";
import { t } from "../i18n";
import { Dropdown, Switch } from "../ui/index";
import { Row, TextBlock } from "../settingsrow";
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
  // The catalogue of local models is further down the same page; the link takes the reader there
  // rather than to another screen.
  const toCatalogue = (e: React.MouseEvent) => {
    e.preventDefault();
    document.getElementById("speech-models")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const second = transcriberChoices(models, fallback, transcriber);

  // The endpoint's own fields, under whichever row chose it: the transcriber's when it is the
  // transcriber, the fallback's when only the fallback uses it.
  const cloudFields = (
    <div className="settings-disclosure asr-cloud">
      <Row title={t("settings.asr.provider")} stack>
        <Dropdown
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
          <TextBlock label={t("settings.asr.url")} value={asr.url} placeholder="https://api.openai.com/v1" onSave={(url) => put({ url })} />
          <div className="settings-block">
            <label className="field">{t(asr.api_key_set ? "settings.asr.key.set" : "settings.asr.key")}</label>
            <input className="field" type="password" defaultValue="" placeholder={asr.api_key_set ? "••••••" : ""} onBlur={(e) => e.target.value && save({ asr: { ...asr, api_key: e.target.value } })} />
          </div>
        </>
      )}
      <div className="grid2 settings-block">
        <div>
          <label className="field">{t("settings.asr.model")}</label>
          <input className="field" defaultValue={asr.model} placeholder={asr.provider === "openrouter" ? "openai/whisper-1" : "whisper-1"} onBlur={(e) => put({ model: e.target.value.trim() })} />
        </div>
        <div>
          <label className="field">{t("settings.asr.language")}</label>
          <input className="field" defaultValue={asr.language} onBlur={(e) => put({ language: e.target.value.trim() })} />
        </div>
      </div>
    </div>
  );

  return (
    <div className="card asr-card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.asr.title")}</div>
      <div className="sub">{t("settings.asr.sub")}</div>
      <Row data-setting="asr.transcriber" title={t("settings.asr.engine")} desc={!anyLocal ? <>{t("settings.asr.nolocal")} <a href="#speech-models" onClick={toCatalogue}>{t("settings.asr.nolocal.link")}</a></> : undefined} stack>
        <Dropdown id="asr-transcriber" label={t("settings.asr.engine")} value={transcriber} invalid={primary.some((o) => o.missing && o.value === transcriber)} onChange={(next) => put({ transcriber: next, fallback: fallback === next ? "" : fallback })} options={primary.map((o) => ({ id: o.value, label: o.label }))} />
      </Row>
      {transcriber === CLOUD && cloudFields}
      <Row title={t("settings.asr.fallback")} desc={t("settings.asr.fallback.sub")} stack>
        <Dropdown id="asr-fallback" label={t("settings.asr.fallback")} value={fallback} invalid={second.some((o) => o.missing && o.value === fallback)} onChange={(next) => put({ fallback: next })} options={[{ id: "", label: t("settings.asr.fallback.none") }, ...second.map((o) => ({ id: o.value, label: o.label }))]} />
      </Row>
      {transcriber !== CLOUD && fallback === CLOUD && cloudFields}
      <Row title={t("settings.asr.autosend")} desc={t("settings.asr.autosend.sub")}>
        <Switch checked={asr.autosend} onChange={(autosend) => put({ autosend })} label={t("settings.asr.autosend")} />
      </Row>
    </div>
  );
}
