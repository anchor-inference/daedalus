import { useEffect, useState } from "react";
import { Switch } from "./ui/components";
import { Row } from "./settingsrow";
import { api } from "./api";
import { modelSize } from "./format";
import { t } from "./i18n";

type SearchSettings = {
  mode: "off" | "local"; paused: boolean; reason: string; busy: boolean;
  indexed: number; pending: number; label: string; size_bytes: number;
  licence: string; installed: boolean;
  progress?: { state: string; done_bytes: number; total_bytes: number; error: string };
};

export function SearchComponent() {
  const [state, setState] = useState<SearchSettings | null>(null);
  const [problem, setProblem] = useState("");
  useEffect(() => {
    let alive = true;
    const load = () => api.get<SearchSettings>("/api/conversation-search/settings").then((value) => { if (alive) setState(value); }).catch(() => { if (alive) setProblem(t("search.failed")); });
    void load();
    const timer = window.setInterval(() => void load(), 2000);
    return () => { alive = false; window.clearInterval(timer); };
  }, []);
  async function action(path: string, method: "post" | "put" | "delete", body?: unknown) {
    setProblem("");
    try { setState(method === "delete" ? await api.delete<SearchSettings>(path) : await api[method]<SearchSettings>(path, body)); }
    catch { setProblem(t("search.failed")); }
  }
  const progress = state?.progress;
  const downloading = progress && ["queued", "downloading", "verifying", "unpacking"].includes(progress.state);
  return <div className="comp-card search-component">
    <div className="comp-head"><b>{t("search.component")}</b></div>
    <p className="sub">{t("search.local.hint")}</p>
    {problem && <p className="sub attn">{problem}</p>}
    {state && <>
      <div className="sub">{state.label} · {state.licence} · {modelSize(state.size_bytes)}</div>
      {/* Switches like every other on/off in Settings; a bare checkbox here was the one of its kind. */}
      <Row title={t("search.local")}>
        <Switch checked={state.mode === "local"} onChange={(on) => void action("/api/conversation-search/settings", "put", { mode: on ? "local" : "off", paused: state.paused })} label={t("search.local")} />
      </Row>
      <div className="sub" role="status">{t(`search.reason.${state.reason}`)}</div>
      {state.reason === "no_runtime" && <code className="mono">{"uv sync --frozen --inexact --extra speech"}</code>}
      {state.mode === "local" && <>
        <div className="sub">{t("search.progress", { n: state.indexed, pending: state.pending })}</div>
        {state.busy && <div className="sub">{t("search.waiting")}</div>}
        <Row title={t("search.pause")}>
          <Switch checked={state.paused} onChange={(paused) => void action("/api/conversation-search/settings", "put", { mode: state.mode, paused })} label={t("search.pause")} />
        </Row>
      </>}
      {downloading && <><progress max={progress.total_bytes || 1} value={progress.done_bytes} aria-label={t("search.download")} /><div className="sub">{modelSize(progress.done_bytes)} / {modelSize(progress.total_bytes)}</div></>}
      {progress?.state === "failed" && <div className="sub attn">{t("search.download.failed")}</div>}
      <div className="btnrow">
        {!state.installed && !downloading && <button className="btn small primary" onClick={() => void action("/api/conversation-search/model", "post")}>{t("search.download")}</button>}
        {downloading && <button className="btn small" onClick={() => void action("/api/conversation-search/model/cancel", "post")}>{t("common.cancel")}</button>}
        {state.installed && <button className="btn small" onClick={() => void action("/api/conversation-search/model", "delete")}>{t("search.remove")}</button>}
      </div>
    </>}
  </div>;
}
