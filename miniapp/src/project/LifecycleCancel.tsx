import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";

type Child = {
  child_kind: "task" | "execution_attempt";
  child_id: string;
  cancel_state: string;
  parent_kind?: string;
  parent_id?: string;
  generation: number;
  source_revision: number;
};

type Preview = {
  parent_kind: "task" | "project_goal";
  parent_id: string;
  entity_revision: number;
  source_revision: number;
  generation: number | null;
  cancel_state: string;
  children: Child[];
  owned_descendants?: Child[];
  preview_fingerprint?: string;
};

function stateText(state: string): string {
  if (state === "active") return t("lifecycle.active");
  if (state === "drained") return t("lifecycle.drained");
  if (state === "requested" || state === "acknowledged") return t("lifecycle.requested");
  return t("lifecycle.unknown");
}

export function LifecycleCancel({ kind, id, projectId, onDone, toast }: {
  kind: "task" | "project_goal";
  id: string;
  projectId: string;
  onDone?: () => void;
  toast: (message: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const operation = useRef<{ fingerprint: string; id: string } | null>(null);
  const offline = useOffline();
  const path = `/api/lifecycle/${kind}/${encodeURIComponent(id)}`;
  const preview = useQuery<Preview>(open ? path : null, { pollMs: 5000, staleMs: 0 });
  const board = useQuery<{ tasks: { id: string; title: string }[] }>(open && kind === "project_goal" ? `/api/projects/${encodeURIComponent(projectId)}/board?include_done=1` : null, { staleMs: 10000 });
  const current = preview.data;
  const rows = current?.owned_descendants ?? current?.children ?? [];
  const titles = Object.fromEntries((board.data?.tasks ?? []).map((task) => [task.id, task.title]));
  const revision = current?.entity_revision;
  const scopeKnown = !!current?.preview_fingerprint && Array.isArray(current.owned_descendants);
  const canCancel = !offline && !busy && !preview.error && current?.cancel_state === "active" && scopeKnown && rows.length > 0 && Number.isInteger(revision) && Number.isInteger(current.source_revision) && !!reason.trim();

  async function cancel() {
    if (!canCancel || !current || !revision) return;
    if (!(await confirmAsync(t(kind === "project_goal" ? "lifecycle.project.confirm" : "lifecycle.task.confirm"), {
      body: t("lifecycle.confirm.body", { n: rows.length }), action: t("lifecycle.action"), danger: true,
    }))) return;
    const body = { expected_entity_revision: revision, expected_source_revision: current.source_revision,
      preview_fingerprint: current.preview_fingerprint, reason: reason.trim() };
    const fingerprint = JSON.stringify(body);
    if (operation.current?.fingerprint !== fingerprint) operation.current = { fingerprint, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post(`${path}/cancel`, { ...body, client_operation_id: operation.current.id });
      operation.current = null;
      toast(t("lifecycle.requested"));
      preview.refresh();
      onDone?.();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        operation.current = null;
        preview.refresh();
      }
      toast(errorText(error));
    } finally { setBusy(false); }
  }

  return <details className="sheet-section" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t(kind === "project_goal" ? "lifecycle.project.title" : "lifecycle.task.title")}</summary>
    <p className="sub">{t(kind === "project_goal" ? "lifecycle.project.scope" : "lifecycle.task.scope")}</p>
    {open && <>
      {preview.error && <div className="result-warning" role="status">{t("lifecycle.read.failed")} <button type="button" className="linkbtn" onClick={() => preview.refresh()}>{t("common.retry")}</button></div>}
      {!preview.error && !current && <div className="sub">{t("lifecycle.read.pending")}</div>}
      {current && <>
        <div className="sub" role="status">{t("lifecycle.state")}: {stateText(current.cancel_state)}</div>
        {rows.length ? <ul className="plain-list">
          {rows.map((child, index) => <li key={`${child.child_kind}:${child.child_id}:${child.generation}`}>
            {child.child_kind === "task" ? titles[child.child_id] ?? t("lifecycle.child.task", { n: index + 1 }) : t("lifecycle.child.attempt", { n: index + 1 })}
            <span className="sub"> · {stateText(child.cancel_state)}</span>
            <details><summary className="sub">{t("lifecycle.child.details")}</summary><code style={{ overflowWrap: "anywhere" }}>{child.parent_kind}/{child.parent_id} → {child.child_kind}/{child.child_id} · {child.generation}/{child.source_revision}</code></details>
          </li>)}
        </ul> : <div className="sub">{t("lifecycle.empty")}</div>}
        {!scopeKnown && <div className="result-warning" role="status">{t("lifecycle.scope.unconfirmed")}</div>}
        {current.cancel_state === "active" && <>
          <label className="field" htmlFor={`lifecycle-reason-${id}`}>{t("lifecycle.reason")}</label>
          <textarea id={`lifecycle-reason-${id}`} className="field" rows={2} maxLength={1000} value={reason} onChange={(event) => setReason(event.target.value)} />
          <button type="button" className="btn small danger" disabled={!canCancel} onClick={() => void cancel()}>{t("lifecycle.action")}</button>
        </>}
      </>}
      {offline && <div className="result-warning" role="status">{t("result.block.unconfirmed")}</div>}
    </>}
  </details>;
}
