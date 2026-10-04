import { useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";

type Handoff = { handoff_id: string; state: "blocked" | "preparing" | "retirement_pending" | "completed";
  blocker: string | null; old_active: boolean; new_active: boolean; receipt_id: string | null;
  schedules_needing_approval: number };
type Command = { reason: string; client_operation_id: string; expected_entity_revision: number;
  expected_coordinator_session_id: string };
type Reply = { state: Handoff["state"]; blocker?: string; receipt_id: string | null; session_id?: string };

function storedReason(key: string): string {
  try { return localStorage.getItem(key)?.slice(0, 300) ?? ""; } catch { return ""; }
}

function storedCommand(key: string): Command | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? "null");
    return value && typeof value.client_operation_id === "string" && typeof value.reason === "string"
      && Number.isInteger(value.expected_entity_revision) && typeof value.expected_coordinator_session_id === "string"
      ? value as Command : null;
  } catch { return null; }
}

export function CoordinatorHandoff({ projectId, revision, sessionId, onChanged, toast }: {
  projectId: string; revision: number; sessionId: string; onChanged: () => Promise<void>; toast: (text: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [collision, setCollision] = useState(false);
  const reasonKey = `daedalus.coordinator.handoff.reason.${projectId}`;
  const pendingKey = `daedalus.coordinator.handoff.pending.${projectId}`;
  const [reason, setReason] = useState(() => storedReason(reasonKey));
  const [pending, setPending] = useState<Command | null>(() => storedCommand(pendingKey));
  const offline = useOffline();
  const path = `/api/projects/${encodeURIComponent(projectId)}/orchestrator/replace`;
  const status = useQuery<{ handoff: Handoff | null }>(open ? path : null, { pollMs: 5000, staleMs: 0 });
  const current = !status.error && status.data?.handoff ? status.data.handoff : null;
  const active = !!sessionId && Number.isInteger(revision) && revision > 0;

  function edit(value: string) {
    setReason(value);
    try { if (value) localStorage.setItem(reasonKey, value); else localStorage.removeItem(reasonKey); }
    catch { /* The mounted draft is still editable. */ }
  }

  function remember(command: Command | null) {
    setPending(command);
    try { if (command) sessionStorage.setItem(pendingKey, JSON.stringify(command)); else sessionStorage.removeItem(pendingKey); }
    catch { /* This page keeps the exact command until it is closed. */ }
  }

  async function submit(command: Command) {
    if (busy || offline) return;
    setBusy(true);
    try {
      const reply = await api.post<Reply>(path, command);
      remember(null);
      setConflict(false);
      setCollision(false);
      void status.refresh(); void onChanged();
      if (reply.state === "blocked") toast(t(`coordinatorHandoff.blocker.${reply.blocker ?? "unknown"}`));
      else {
        edit("");
        toast(t("coordinatorHandoff.committed"));
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        const detail = error.data.detail;
        const reason = typeof detail === "object" && detail !== null && "reason" in detail ? String(detail.reason) : error.message;
        if (reason.includes("reused with a different request")) setCollision(true);
        else { remember(null); setConflict(true); }
        void status.refresh(); void onChanged();
      }
      toast(errorText(error));
    } finally { setBusy(false); }
  }

  async function begin() {
    if (!active || busy || pending || offline || status.error || conflict) return;
    if (!(await confirmAsync(t("coordinatorHandoff.confirm.title"), {
      body: t("coordinatorHandoff.confirm.body"), action: t("coordinatorHandoff.action"),
    }))) return;
    const command = { reason: reason.trim(), client_operation_id: crypto.randomUUID(),
      expected_entity_revision: revision, expected_coordinator_session_id: sessionId };
    remember(command);
    await submit(command);
  }

  const blocker = current?.blocker ?? "unknown";
  return <details className="sheet-section" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("coordinatorHandoff.title")}</summary>
    {open && <div className="project-extension-list">
      <p className="sub">{t("coordinatorHandoff.intro")}</p>
      {offline && <div className="result-warning" role="status">{t("coordinatorHandoff.offline")}</div>}
      {status.error && <div className="result-warning" role="status">{t("coordinatorHandoff.readFailed")} <button type="button" className="linkbtn" onClick={() => status.refresh()}>{t("common.retry")}</button></div>}
      {!status.error && !status.data && <div className="sub">{t("common.loading")}</div>}
      {current && <div role="status" className={current.state === "blocked" ? "result-warning" : "sub"}>
        {t(`coordinatorHandoff.state.${current.state}`)}{current.state === "blocked" ? ` · ${t(`coordinatorHandoff.blocker.${blocker}`)}` : ""}
      </div>}
      {current?.new_active && current.schedules_needing_approval > 0 && <p className="sub">{t("coordinatorHandoff.schedules", { count: current.schedules_needing_approval })}</p>}
      {pending && <div role="status" className="result-warning">{t(collision ? "coordinatorHandoff.collision" : "coordinatorHandoff.pending")}
        <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => {
          if (collision) { remember(null); setCollision(false); setConflict(true); void status.refresh(); void onChanged(); }
          else void submit(pending);
        }}>{t(collision ? "coordinatorHandoff.review" : "coordinatorHandoff.retry")}</button></div>}
      {conflict && <div role="status" className="result-warning">{t("coordinatorHandoff.conflict")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => {
        setBusy(true);
        void Promise.all([status.refresh(), onChanged()]).then(() => setConflict(false)).finally(() => setBusy(false));
      }}>{t("coordinatorHandoff.review")}</button></div>}
      {!active && <p className="sub">{t("coordinatorHandoff.noOffice")}</p>}
      {active && <>
        <label className="field">{t("coordinatorHandoff.reason")}
          <textarea className="field" maxLength={300} value={reason} onChange={(event) => edit(event.target.value)} disabled={busy || !!pending} />
        </label>
        <button type="button" className="btn small" disabled={offline || busy || !!pending || !!status.error || !status.data || conflict} onClick={() => void begin()}>{t("coordinatorHandoff.action")}</button>
      </>}
      <p className="sub">{t("coordinatorHandoff.readinessLimit")}</p>
      {current?.receipt_id && <details><summary>{t("coordinatorHandoff.receipt")}</summary><div className="mono">{current.receipt_id}</div></details>}
    </div>}
  </details>;
}
