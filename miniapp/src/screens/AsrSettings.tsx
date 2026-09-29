// Settings → speech-to-text for voice notes: which transcriber turns a recording into words, the
// cloud endpoint's own fields, and what is tried when the transcriber fails.
//
// Self-contained on purpose: it takes the settings and a save, fetches the installed local models
// itself, and draws one card, so it can be moved wherever the settings end up without taking
// anything of the tab it sits in with it.

import { useEffect, useState } from "react";
import type { Settings } from "../api";
import { t } from "../i18n";
import { go } from "../shell";
import { pathFor } from "../router";
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
  const cloudInUse = transcriber === CLOUD || fallback === CLOUD;
  const anyLocal = models.some((m) => m.installed);
  const primary = transcriberChoices(models, transcriber);
  const second = transcriberChoices(models, fallback, transcriber);

  return (
    <div className="card asr-card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.asr.title")}</div>
      <div className="sub">{t("settings.asr.sub")}</div>
      <div className="grid2">
        <div>
          <label className="field" htmlFor="asr-transcriber">{t("settings.asr.engine")}</label>
          <select id="asr-transcriber" className="field" value={transcriber} onChange={(e) => put({ transcriber: e.target.value, fallback: fallback === e.target.value ? "" : fallback })}>
            {primary.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
        <div>
          <label className="field" htmlFor="asr-fallback">{t("settings.asr.fallback")}</label>
          <select id="asr-fallback" className="field" value={fallback} onChange={(e) => put({ fallback: e.target.value })}>
            <option value="">{t("settings.asr.fallback.none")}</option>
            {second.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
          </select>
        </div>
      </div>
      {!anyLocal && (
        <div className="sub">
          {t("settings.asr.nolocal")}{" "}
          <a href={pathFor("voice")} onClick={(e) => go(e, pathFor("voice"))}>{t("settings.asr.nolocal.link")}</a>
        </div>
      )}
      {cloudInUse && (
        <>
          <div className="section-title">{t("settings.asr.cloud")}</div>
          <label className="field" htmlFor="asr-provider">{t("settings.asr.provider")}</label>
          <select id="asr-provider" className="field" value={asr.provider || ""} onChange={(e) => put({ provider: e.target.value })}>
            <option value="">{t("settings.asr.custom")}</option>
            {Object.keys(s.providers).map((pid) => (
              <option key={pid} value={pid}>{pid}{s.providers[pid].base_url ? ` · ${s.providers[pid].base_url.replace(/^https?:\/\//, "")}` : ""}</option>
            ))}
          </select>
          {!asr.provider && (
            <>
              <label className="field">{t("settings.asr.url")}</label>
              <input className="field" defaultValue={asr.url} placeholder="https://api.openai.com/v1" onBlur={(e) => put({ url: e.target.value.trim() })} />
              <label className="field">{t(asr.api_key_set ? "settings.asr.key.set" : "settings.asr.key")}</label>
              <input className="field" type="password" defaultValue="" placeholder={asr.api_key_set ? "••••••" : ""} onBlur={(e) => e.target.value && save({ asr: { ...asr, api_key: e.target.value } })} />
            </>
          )}
          <div className="grid2">
            <div>
              <label className="field">{t("settings.asr.model")}</label>
              <input className="field" defaultValue={asr.model} placeholder={asr.provider === "openrouter" ? "openai/whisper-1" : "whisper-1"} onBlur={(e) => put({ model: e.target.value.trim() })} />
            </div>
            <div>
              <label className="field">{t("settings.asr.language")}</label>
              <input className="field" defaultValue={asr.language} onBlur={(e) => put({ language: e.target.value.trim() })} />
            </div>
          </div>
        </>
      )}
      <div className="btnrow">
        <button className={`btn small ${asr.autosend ? "primary" : ""}`} onClick={() => put({ autosend: !asr.autosend })}>
          {t("settings.asr.autosend", { state: t(asr.autosend ? "common.on" : "common.off") })}
        </button>
      </div>
    </div>
  );
}
