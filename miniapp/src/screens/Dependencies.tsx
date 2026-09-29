import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Sheet } from "../dialogs";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { errorText } from "../ui";
import { DependencyProgress, type Progress } from "./dependencyprogress";
import "./dependencies.css";

type Recipe = { python: string[]; system: string[] };
type Proposal = Progress & { id: string; state: string; request: string; preset?: string; error?: string; explanation?: string; proposal?: { patch: string; recipe: Recipe } };
type View = {
  capability: { mode: string; python: boolean; system: boolean; manager: string; reason: string };
  tools: { name: string; available: boolean; version: string }[];
  packages: [string, string][];
  recipe: Recipe;
  models: { id: string; label: string }[];
  proposal: Proposal | null;
  job: (Progress & { id: string; state: string; error: string }) | null;
};

export function DependenciesTab() {
  const [view, setView] = useState<View | null>(null);
  const [problem, setProblem] = useState("");
  const [offline, setOffline] = useState(false);
  const [request, setRequest] = useState("");
  const [preset, setPreset] = useState("");
  const [busy, setBusy] = useState(false);
  const [review, setReview] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const opened = useRef("");
  const hydrated = useRef(false);
  const sequence = useRef(0);
  const load = useCallback(async () => {
    const ticket = ++sequence.current;
    try {
      const next = await api.get<View>("/api/dependencies");
      if (ticket !== sequence.current) return;
      setView(next);
      setOffline(false);
      setPreset((current) => next.models.some((model) => model.id === current) ? current : next.models.find((model) => model.id === next.proposal?.preset)?.id || next.models[0]?.id || "");
      if (!hydrated.current) {
        hydrated.current = true;
        const proposalStillEditable = next.proposal && ["planning", "validating", "ready", "failed"].includes(next.proposal.state);
        // An accepted recipe remains in the durable receipt across restarts. Restoring its request
        // made a finished installation look like an unsent draft, while a reviewable proposal must
        // still survive a reload.
        setRequest(proposalStillEditable ? next.proposal!.request : "");
      }
      if (next.proposal?.state === "ready" && next.proposal.id !== opened.current) {
        opened.current = next.proposal.id;
        setAccepted(false);
        setReview(true);
      }
    } catch {
      if (ticket === sequence.current) setOffline(true);
    }
  }, []);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      await load();
      if (alive) timer = setTimeout(poll, 3000);
    };
    void poll();
    return () => { alive = false; ++sequence.current; clearTimeout(timer); };
  }, [load]);

  async function act(path: string, body: object = {}, clearRequest = false) {
    setBusy(true);
    setProblem("");
    try {
      await api.post(path, body);
      if (clearRequest) setRequest("");
      setReview(false);
      setAccepted(false);
      await load();
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  const proposal = view?.proposal;
  const planning = proposal?.state === "planning" || proposal?.state === "validating";
  const installing = view?.job?.state === "installing" || view?.job?.state === "restarting";
  const blocked = busy || planning || installing || offline;
  const available = view?.tools.filter((tool) => tool.available) ?? [];
  const missing = view?.tools.filter((tool) => !tool.available) ?? [];
  return <div className="dependencies">
    <div className="card deps-overview">
    <div><div className="section-title">{t("settings.sec.dependencies")}</div>
    <p className="sub">{t("deps.intro")}</p></div>
    {problem && <div className="sub attn" role="alert">{problem}</div>}
    {!view && !offline && <p className="sub">{t("common.loading")}</p>}
    {view && <>
      <div className="deps-mode"><Icon name={view.capability.mode === "native" ? "terminal" : "skill"} size={16} /><span><b>{t(view.capability.mode === "native" ? "deps.mode.native" : "deps.mode.docker")}</b><span className="sub">{t(view.capability.mode === "native" ? "deps.native" : "deps.docker")}</span></span></div>
      {!view.capability.python && <p className="sub attn">{view.capability.reason}</p>}
      {!view.capability.system && view.capability.mode === "native" && <p className="sub">{t("deps.noSystem")}</p>}
    </>}
    </div>
    {/* Losing the host is a failure of the same feature as a failed install, so it takes the same
        card, icon first, rather than a line of coloured text inside the description. */}
    {offline && <section className="card deps-job offline" role="status">
        <div className="deps-job-title"><span className="deps-job-icon"><Icon name="alert" size={18} /></span><span><b>{t("deps.offline.title")}</b><span className="sub">{t("deps.offline")}</span></span></div>
        {/* The poll retries on its own every few seconds; the button is for the reader who has just
            fixed the host and wants the answer now, and it gives the card something to do. */}
        <div className="btnrow"><button className="btn small" onClick={() => void load()}><Icon name="reload" size={14} /> {t("common.retry")}</button></div>
      </section>}
    {view && view.job && (installing || view.job.state === "failed") && <section className={`card deps-job ${view.job.state}`} aria-label={t("deps.installProgress")}>
        <div className="deps-job-title"><span className="deps-job-icon"><Icon name={view.job.state === "failed" ? "close" : "download"} size={18} /></span><span><b>{t(`deps.job.${view.job.state}`)}</b><span className="sub">{installing ? t("deps.installingHint") : view.job.error}</span></span></div>
        <DependencyProgress value={view.job} active={installing} installation />
      </section>}
    {view && <>
      {!installing && <div className="card deps-request-card">
      <div className="section-title">{t("deps.add.title")}</div><p className="sub">{t("deps.add.sub")}</p>
      <label className="deps-label" htmlFor="dependency-request">{t("deps.request")}</label>
      <textarea id="dependency-request" className="field" rows={3} maxLength={2000} value={request} disabled={blocked} onChange={(event) => setRequest(event.target.value)} placeholder={t("deps.placeholder")} />
      <div className="deps-request-actions"><select id="dependency-model" aria-label={t("deps.model")} className="field compact" value={preset} disabled={blocked} onChange={(event) => setPreset(event.target.value)}>
        {!view.models.length && <option value="">{t("deps.noModels")}</option>}
        {view.models.map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}
      </select><div className="btnrow">
        <button className="btn primary" disabled={blocked || !view.capability.python || !request.trim() || !preset} onClick={() => void act("/api/dependencies/request", { request, preset })}><Icon name="bolt" size={15} /> {t(planning ? "deps.planning" : "deps.prepare")}</button>
        {proposal && ["planning", "validating", "ready"].includes(proposal.state) && <button className="btn" disabled={busy || offline} onClick={() => void act(`/api/dependencies/${proposal.id}/cancel`)}>{t("common.cancel")}</button>}
        {proposal?.state === "ready" && <button className="btn" onClick={() => { setAccepted(false); setReview(true); }}>{t("deps.review")}</button>}
      </div></div>
      {proposal?.state === "failed" && <p className="sub attn" role="alert">{proposal.error}</p>}
      {proposal && <DependencyProgress value={proposal} active={planning} />}
      </div>}
      <div className="card deps-environment">
        <div className="deps-environment-head"><span><b>{t("deps.environment")}</b><span className="sub">{[plural("deps.environment.available", available.length), t("deps.environment.missing", { n: String(missing.length) }), plural("deps.environment.python", view.packages.length)].join(" · ")}</span></span>{view.job?.state === "completed" && <span className="chip"><Icon name="check" size={13} />{t("deps.current")}</span>}</div>
        {/* The missing ones are named beside the present ones: the summary counted them and nothing on
            the card said which they were. */}
        <div className="deps-tool-chips">
          {available.map((tool) => <span className="chip" key={tool.name}><code>{tool.name}</code></span>)}
          {missing.map((tool) => <span className="chip missing" key={tool.name} title={t("deps.missing")}><code>{tool.name}</code><span>{t("deps.missing")}</span></span>)}
        </div>
        <details><summary>{t("deps.environment.details")}</summary><div className="deps-tools">
          {view.tools.map((tool) => <div className="kv" key={tool.name}><code>{tool.name}</code><span className={tool.available ? "sub" : "sub faint"}>{tool.available ? tool.version : t("deps.missing")}</span></div>)}
        </div></details>
        <details><summary>{t("deps.pythonPackages", { n: String(view.packages.length) })}</summary>
          {view.packages.length ? view.packages.map(([name, version]) => <div className="kv" key={name}><code>{name}</code><span className="sub">{version}</span></div>) : <p className="sub">{t("deps.pythonEmpty")}</p>}
        </details>
        <details><summary>{t("deps.recipe")}</summary><pre>{JSON.stringify(view.recipe, null, 2)}</pre></details>
      </div>
    </>}
    {review && proposal?.state === "ready" && proposal.proposal && <Sheet title={t("deps.review")} onClose={() => !busy && setReview(false)}>
      <div className="dependencies">
        <p className="deps-explanation">{proposal.explanation}</p>
        <pre className="deps-patch">{proposal.proposal.patch}</pre>
        <p className="sub attn">{t("deps.warning")}</p>
        {view?.capability.mode === "native" && <p className="sub">{t("deps.nativeWarning")}</p>}
        {problem && <p className="sub attn" role="alert">{problem}</p>}
        <label className="deps-consent"><input type="checkbox" checked={accepted} disabled={busy} onChange={(event) => setAccepted(event.target.checked)} />{t("deps.consent")}</label>
        <div className="btnrow">
          <button className="btn primary" disabled={!accepted || busy || offline || installing} onClick={() => void act(`/api/dependencies/${proposal.id}/approve`, {}, true)}>{t("deps.install")}</button>
          <button className="btn" disabled={busy || offline} onClick={() => void act(`/api/dependencies/${proposal.id}/cancel`)}>{t("common.cancel")}</button>
        </div>
      </div>
    </Sheet>}
  </div>;
}
