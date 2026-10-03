import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { useOffline } from "../store";
import { errorText } from "../ui";
import type { ProjectTask } from "./board";
import type { ResultContract } from "./EvidenceReview";
import type { ResultReceipt } from "./ResultFlow";

type Intent = { body: { client_operation_id: string; expected_entity_revision: number;
  verdict_id: string; contract_revision: number; reason: string }; result_id: string };

function saved<T>(key: string): T | null {
  try { return JSON.parse(sessionStorage.getItem(key) ?? "null") as T | null; }
  catch { return null; }
}

function keep(key: string, value: unknown | null) {
  try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, JSON.stringify(value)); }
  catch { /* keep the mounted draft and exact command */ }
}

export function ManualReopen({ task, result, contract, onChanged, toast }: { task: ProjectTask;
  result: ResultReceipt; contract: ResultContract; onChanged: () => void; toast: (message: string) => void }) {
  const draftKey = `daedalus.manual.reopen.draft.${task.id}`;
  const pendingKey = `daedalus.manual.reopen.pending.${task.id}`;
  const [reason, setReason] = useState(() => saved<string>(draftKey) ?? "");
  const [pending, setPending] = useState<Intent | null>(() => saved<Intent>(pendingKey));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const offline = useOffline();
  const current = task.status === "done" && task.acceptance_state === "operator_approved" && !task.branch &&
    result.accepted && result.current_result_id === result.result_id && result.contract_revision === contract.contract_revision &&
    result.verdict_accepted === true && result.verification === "verified" && !!result.verdict_id;
  useEffect(() => keep(draftKey, reason), [draftKey, reason]);

  function remember(value: Intent | null) { setPending(value); keep(pendingKey, value); }

  async function send(intent: Intent) {
    if (busy || offline) return;
    setBusy(true);
    setMessage("");
    try {
      await api.post(`/api/board/${encodeURIComponent(task.id)}/results/${encodeURIComponent(intent.result_id)}/reopen`, intent.body);
      remember(null);
      setReason("");
      toast(t("manual.reopened"));
      onChanged();
    } catch (failure) {
      setMessage(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409 &&
          !failure.message.includes("command identity was reused with a different request")) {
        remember(null);
        onChanged();
      }
    } finally { setBusy(false); }
  }

  function reopen() {
    if (!current || busy || offline || pending || !reason.trim() || reason.trim().length > 2000 || !result.verdict_id) return;
    const intent = { result_id: result.result_id, body: { client_operation_id: crypto.randomUUID(),
      expected_entity_revision: contract.entity_revision, verdict_id: result.verdict_id,
      contract_revision: result.contract_revision, reason: reason.trim() } };
    remember(intent);
    void send(intent);
  }

  return <details className="result-details"><summary>{t("manual.reopen")}</summary>
    <p className="sub">{t("manual.reopenHelp")}</p>
    <label className="field">{t("manual.reopenReason")}<textarea className="field" rows={3} maxLength={2000} value={reason} disabled={busy || !!pending} onChange={(event) => setReason(event.target.value)} /></label>
    <button type="button" className="btn small warn" disabled={!current || busy || offline || !!pending || !reason.trim()} onClick={reopen}>{t("manual.reopenAction")}</button>
    {pending && <p className="result-warning" role="status">{t("manual.pending")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void send(pending)}>{t("common.retry")}</button></p>}
    {message && <p className="result-warning" role="alert">{message}</p>}
  </details>;
}

/** A committed reopen can remove its own result panel before a lost reply reaches the browser. */
export function ManualReopenRecovery({ task, onChanged, toast }: { task: ProjectTask;
  onChanged: () => void; toast: (message: string) => void }) {
  const key = `daedalus.manual.reopen.pending.${task.id}`;
  const [pending, setPending] = useState<Intent | null>(() => saved<Intent>(key));
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const offline = useOffline();
  if (!pending) return null;
  async function retry() {
    if (!pending || offline || busy) return;
    setBusy(true);
    setMessage("");
    try {
      await api.post(`/api/board/${encodeURIComponent(task.id)}/results/${encodeURIComponent(pending.result_id)}/reopen`, pending.body);
      keep(key, null);
      setPending(null);
      toast(t("manual.reopened"));
      onChanged();
    } catch (failure) {
      setMessage(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409 &&
          !failure.message.includes("command identity was reused with a different request")) {
        keep(key, null);
        setPending(null);
        onChanged();
      }
    } finally { setBusy(false); }
  }
  return <div className="result-warning" role="status">{t("manual.reopenPending")}
    <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void retry()}>{t("manual.retryReopen")}</button>
    {message && <p role="alert">{message}</p>}
  </div>;
}
