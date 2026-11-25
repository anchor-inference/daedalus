import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Sheet } from "../ui/dialogs";
import { Button, Select } from "../ui/primitives";
import { WithEffort } from "../taskeffort";
import { Row, SetBlock, SetFoot, SetGroup } from "../settingsrow";
import { t } from "../i18n";
import { duration } from "../format";
import { DiffView } from "../previewparts";
import { errorText, sentence } from "../ui";
import "./promptchange.css";

type Proposal = {
  id: string;
  state: "planning" | "validating" | "ready" | "failed" | "cancelled" | "applied";
  instruction: string;
  preset: string;
  started_at: number;
  updated_at: number;
  stage: string;
  summary?: string;
  diff?: string;
  error?: string;
};
type View = { proposal: Proposal | null; models: { id: string; label: string }[] };

// The shared formatter rolls minutes into hours; a hand-rolled one here read "187m 12s" on a long run.
function elapsed(since: number) {
  return duration(Date.now() - since * 1000);
}

export function PromptChange({ onApplied }: { onApplied: (rules: string) => void }) {
  const [view, setView] = useState<View | null>(null);
  const [instruction, setInstruction] = useState("");
  const [preset, setPreset] = useState("");
  const [effort, setEffort] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [review, setReview] = useState(false);
  const [clock, setClock] = useState(0);
  const opened = useRef("");
  const hydrated = useRef(false);
  const load = useCallback(async () => {
    const next = await api.get<View>("/api/prompt-change");
    setView(next);
    setPreset((current) => next.models.some((model) => model.id === current) ? current : next.models.find((model) => model.id === next.proposal?.preset)?.id || next.models[0]?.id || "");
    if (!hydrated.current) {
      hydrated.current = true;
      if (["planning", "validating"].includes(next.proposal?.state ?? "")) setInstruction(next.proposal!.instruction);
    }
    if (next.proposal?.state === "ready" && next.proposal.id !== opened.current) {
      opened.current = next.proposal.id;
      setReview(true);
    }
  }, []);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { await load(); } catch { /* the page-wide offline state owns connectivity */ }
      if (alive) timer = setTimeout(poll, ["planning", "validating"].includes(view?.proposal?.state ?? "") ? 2000 : 5000);
    };
    void poll();
    return () => { alive = false; clearTimeout(timer); };
  }, [load, view?.proposal?.state]);
  useEffect(() => {
    if (!["planning", "validating"].includes(view?.proposal?.state ?? "")) return;
    const timer = setInterval(() => setClock((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [view?.proposal?.state]);

  async function request() {
    setBusy(true);
    setProblem("");
    try {
      await api.post("/api/prompt-change/request", { instruction, preset, effort });
      await load();
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setBusy(false);
    }
  }
  async function cancel() {
    const proposal = view?.proposal;
    if (!proposal) return;
    setBusy(true);
    setProblem("");
    try {
      await api.post(`/api/prompt-change/${proposal.id}/cancel`);
      setReview(false);
      setInstruction("");
      await load();
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setBusy(false);
    }
  }
  async function apply() {
    const proposal = view?.proposal;
    if (!proposal) return;
    setBusy(true);
    setProblem("");
    try {
      const result = await api.post<{ applied: true; rules: string }>(`/api/prompt-change/${proposal.id}/approve`);
      onApplied(result.rules);
      setReview(false);
      setInstruction("");
      await load();
    } catch (error) {
      setProblem(errorText(error));
    } finally {
      setBusy(false);
    }
  }

  const proposal = view?.proposal;
  const planning = proposal?.state === "planning" || proposal?.state === "validating";
  const ready = proposal?.state === "ready";
  void clock;
  return <>
    <SetGroup title={t("settings.rules.assistant.title")} help={t("settings.rules.assistant.sub")} className="prompt-change" data-setting="prompt.change">
      <SetBlock>
        <label className="field-label" htmlFor="prompt-change-request">{t("settings.rules.assistant.request")}</label>
        <textarea id="prompt-change-request" className="field" rows={3} maxLength={2000} value={instruction} disabled={planning || busy} onChange={(event) => setInstruction(event.target.value)} placeholder={t("settings.rules.assistant.placeholder")} />
      </SetBlock>
      <Row title={t("settings.rules.assistant.model")} stack>
        <WithEffort id="prompt-change-effort" value={effort} onChange={setEffort} fallback="off" disabled={planning || busy}>
          <Select<string> className="set-value" label={t("settings.rules.assistant.model")} value={preset} disabled={planning || busy} onChange={setPreset}
            options={(view?.models ?? []).map((model) => ({ id: model.id, label: model.label }))} />
        </WithEffort>
      </Row>
      {planning && <SetBlock className="prompt-change-status"><span className="live-dot" /><span className="prompt-change-text" role="status"><b>{t("settings.rules.assistant.working")}</b><span className="sub">{t("settings.rules.assistant.elapsed", { time: elapsed(proposal.started_at) })}</span></span><Button size="sm" disabled={busy} onClick={() => void cancel()}>{t("common.cancel")}</Button></SetBlock>}
      {ready && <SetBlock className="prompt-change-ready"><span className="prompt-change-text"><b>{t("settings.rules.assistant.ready")}</b><span className="sub clamp-2">{proposal.summary}</span></span><Button size="sm" onClick={() => setReview(true)}>{t("settings.rules.assistant.review")}</Button></SetBlock>}
      {proposal?.state === "failed" && <SetBlock><span className="sub attn" role="alert">{sentence(proposal.error ?? "")}</span></SetBlock>}
      {problem && <SetBlock><span className="sub attn" role="alert">{problem}</span></SetBlock>}
      <SetFoot>
        <Button variant="primary" icon="bolt" disabled={planning || busy || !instruction.trim() || !preset} onClick={() => void request()}>
          {t("settings.rules.assistant.prepare")}
        </Button>
      </SetFoot>
    </SetGroup>
    {review && ready && proposal.diff && <Sheet title={t("settings.rules.review.title")} size="xl" className="prompt-review-sheet" onClose={() => !busy && setReview(false)}>
      <div className="prompt-review">
        <p>{proposal.summary}</p>
        <div className="prompt-review-diff"><DiffView text={proposal.diff} /></div>
        <p className="sub">{t("settings.rules.review.hint")}</p>
        {problem && <div className="sub attn" role="alert">{problem}</div>}
        <div className="sheet-foot">
          <Button disabled={busy} onClick={() => void cancel()}>{t("settings.rules.review.reject")}</Button>
          <Button variant="primary" icon="check" disabled={busy} onClick={() => void apply()}>{t("settings.rules.review.apply")}</Button>
        </div>
      </div>
    </Sheet>}
  </>;
}
