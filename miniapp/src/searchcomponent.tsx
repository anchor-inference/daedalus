import { useEffect, useState } from "react";
import { Badge, Button, Switch } from "./ui/primitives";
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
  return <div className="comp-item search-component" data-component="conversation-search">
    <Row
      className="comp-row"
      title={<><span className="comp-name">{t("search.component")}</span>{state && <span className="comp-state"><Badge tone={state.installed ? "live" : undefined}>{t(state.installed ? "comp.state.installed" : "comp.state.missing")}</Badge></span>}</>}
      desc={<>
        <span className="comp-note">{t("search.local.hint")}</span>
        {state && <span className="comp-note">{state.label} · {state.licence} · {modelSize(state.size_bytes)}</span>}
      </>}
    >
      {state && !state.installed && !downloading && <Button size="sm" variant="primary" onClick={() => void action("/api/conversation-search/model", "post")}>{t("search.download")}</Button>}
      {downloading && <Button size="sm" onClick={() => void action("/api/conversation-search/model/cancel", "post")}>{t("common.cancel")}</Button>}
      {state?.installed && <Button size="sm" onClick={() => void action("/api/conversation-search/model", "delete")}>{t("search.remove")}</Button>}
    </Row>
    {state && <>
      {/* Switches like every other on/off in Settings; a bare checkbox here was the one of its kind. */}
      <Row title={t("search.local")} desc={<span role="status">{t(`search.reason.${state.reason}`)}</span>}>
        <Switch checked={state.mode === "local"} onChange={(on) => void action("/api/conversation-search/settings", "put", { mode: on ? "local" : "off", paused: state.paused })} label={t("search.local")} />
      </Row>
      {state.mode === "local" && (
        <Row title={t("search.pause")} desc={<>{t("search.progress", { n: state.indexed, pending: state.pending })}{state.busy && <> · {t("search.waiting")}</>}</>}>
          <Switch checked={state.paused} onChange={(paused) => void action("/api/conversation-search/settings", "put", { mode: state.mode, paused })} label={t("search.pause")} />
        </Row>
      )}
    </>}
    {(problem || state?.reason === "no_runtime" || downloading || progress?.state === "failed") && (
      <div className="comp-extra">
        {problem && <div className="sub attn comp-line">{problem}</div>}
        {state?.reason === "no_runtime" && <code className="mono comp-fix">{"uv sync --frozen --inexact --extra speech"}</code>}
        {downloading && progress && <><progress max={progress.total_bytes || 1} value={progress.done_bytes} aria-label={t("search.download")} /><div className="sub">{modelSize(progress.done_bytes)} / {modelSize(progress.total_bytes)}</div></>}
        {progress?.state === "failed" && <div className="sub attn comp-line">{t("search.download.failed")}</div>}
      </div>
    )}
  </div>;
}
