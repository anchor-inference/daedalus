import { useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { launchFeedback, type ProjectTask } from "./board";

type Target = { id: string; name: string; harness: string; permission_mode: string };
type Options = { source_attempt_id: string | null; source_harness?: string; source_released?: boolean;
  entity_revision: number; contract_revision?: number; targets: Target[] };
type Preview = { ready: boolean; preview_digest: string; blockers: string[]; packet: {
  source_result: { id: string; outcome: string; original_digest: string } | null;
  artifacts: { artifact_key: string; digest: string }[]; criteria: unknown[]; requirements: unknown[];
  open_obligations: unknown[];
  workspace: { branch: string | null; base_ref: string | null }; target_permission_mode: string;
  source_permission_mode: string;
  history_portability: string; workspace_transfer: string; cost_state: string };
  capability: { available: boolean; reason: string; version?: string; login?: string; cost: string };
  budget: { total: { state: string; available_usd: string | null } } | null;
  resource: { state: "none" | "ready" | "blocked"; reason?: string; profile_revision?: number } };
type Intent = { task_id: string; body: { source_attempt_id: string; target_staff_id: string;
  preview_digest: string; expected_entity_revision: number; client_operation_id: string } };
type Effect = { state: string; error?: string; wait_reason?: string };

function key(taskId: string): string { return `daedalus.runtime.handoff.${taskId}`; }

function saved(taskId: string): Intent | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key(taskId)) ?? "null");
    return value?.task_id === taskId && typeof value.body?.client_operation_id === "string" ? value as Intent : null;
  } catch { return null; }
}

function keep(taskId: string, intent: Intent | null): void {
  try { if (intent) sessionStorage.setItem(key(taskId), JSON.stringify(intent)); else sessionStorage.removeItem(key(taskId)); }
  catch { /* the mounted intent still keeps its exact operation identity */ }
}

export function RuntimeHandoff({ task, onChanged, toast }: { task: ProjectTask;
  onChanged: () => void; toast: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  const [targetId, setTargetId] = useState("");
  const [pending, setPending] = useState<Intent | null>(() => saved(task.id));
  const [effectId, setEffectId] = useState("");
  const [busy, setBusy] = useState(false);
  const [warning, setWarning] = useState("");
  const offline = useOffline();
  const base = `/api/board/${encodeURIComponent(task.id)}`;
  const options = useQuery<Options>(open ? `${base}/handoff-options` : null, { staleMs: 0, pollMs: 10000 });
  const sourceId = options.data?.source_attempt_id;
  const preview = useQuery<Preview>(open && sourceId && targetId ?
    `${base}/handoff-preview?source_attempt_id=${encodeURIComponent(sourceId)}&target_staff_id=${encodeURIComponent(targetId)}` : null,
    { staleMs: 0, pollMs: 10000 });
  const effect = useQuery<Effect>(effectId ? `/api/control/effects/${encodeURIComponent(effectId)}` : null,
    { staleMs: 0, pollMs: 5000 });
  const target = options.data?.targets.find((item) => item.id === targetId);
  const current = options.data && options.data.entity_revision === task.entity_revision &&
    options.data.source_released && sourceId && target && preview.data?.ready && !preview.error;

  async function send(intent: Intent) {
    if (busy || offline) return;
    setBusy(true);
    setWarning("");
    try {
      const receipt = await api.post<{ effect_id: string; handoff_id: string; state: string }>(
        `${base}/continue-elsewhere`, intent.body);
      if (!receipt.effect_id || !receipt.handoff_id || receipt.state !== "queued") throw new Error(t("handoff.badReceipt"));
      keep(task.id, null);
      setPending(null);
      setEffectId(receipt.effect_id);
      toast(t("handoff.queued"));
      options.refresh();
      onChanged();
    } catch (error) {
      setWarning(errorText(error));
      if (error instanceof ApiError && error.status === 409 &&
          !error.message.includes("command identity was reused with a different request")) {
        keep(task.id, null);
        setPending(null);
        options.refresh();
        preview.refresh();
        onChanged();
      }
    } finally { setBusy(false); }
  }

  async function approve() {
    if (!current || busy || offline || pending || !sourceId || !targetId || !preview.data) return;
    if (!(await confirmAsync(t("handoff.confirmTitle"), { body: t("handoff.confirmBody", {
      name: target.name, runtime: target.harness, permission: preview.data.packet.target_permission_mode,
    }), action: t("handoff.continue") }))) return;
    const intent: Intent = { task_id: task.id, body: {
      source_attempt_id: sourceId, target_staff_id: targetId,
      preview_digest: preview.data.preview_digest,
      expected_entity_revision: options.data!.entity_revision,
      client_operation_id: crypto.randomUUID(),
    } };
    setPending(intent);
    keep(task.id, intent);
    void send(intent);
  }

  if (!["todo", "blocked", "doing"].includes(task.status) || !task.project_id) return null;
  return <details className="result-details" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("handoff.title")}</summary>
    <p className="sub">{t("handoff.help")}</p>
    {options.error && <p className="result-warning" role="alert">{errorText(options.error)}</p>}
    {open && options.data && <>
      {!sourceId && <p className="sub">{t("handoff.noSource")}</p>}
      {sourceId && !options.data.source_released && <p className="result-warning">{t("handoff.waitExit")}</p>}
      {sourceId && options.data.targets.length === 0 && <p className="sub">{t("handoff.noTarget")}</p>}
      {sourceId && options.data.targets.length > 0 && <label className="field">{t("handoff.target")}
        <select value={targetId} disabled={busy || !!pending} onChange={(event) => setTargetId(event.target.value)}>
          <option value="">{t("handoff.choose")}</option>
          {options.data.targets.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.harness}</option>)}
        </select>
      </label>}
      {preview.error && <p className="result-warning" role="alert">{errorText(preview.error)}</p>}
      {preview.data && target && <div className="sub">
        <p>{t("handoff.evidence", { reports: preview.data.packet.source_result ? "1" : "0",
          artifacts: String(preview.data.packet.artifacts.length), criteria: String(preview.data.packet.criteria.length),
          requirements: String(preview.data.packet.requirements.length),
          obligations: String(preview.data.packet.open_obligations.length) })}</p>
        <p>{t("handoff.limitations")}</p>
        <p>{t(preview.data.packet.cost_state === "priced_at_native_inference" ? "handoff.priced" : "handoff.unpriced")}</p>
        {preview.data.budget && <p>{t("handoff.budget", {
          available: preview.data.budget.total.state === "known" && preview.data.budget.total.available_usd !== null ?
            `$${preview.data.budget.total.available_usd}` : t("handoff.unknown") })}</p>}
        {preview.data.resource.state === "blocked" && <p className="result-warning">{t("handoff.resourceBlocked", {
          reason: preview.data.resource.reason || t("handoff.unknown") })}</p>}
        {!preview.data.ready && preview.data.resource.state !== "blocked" &&
          <p className="result-warning">{preview.data.capability.reason || preview.data.blockers.join(", ")}</p>}
        {/* The runtime's version, the access modes and the old branch matter when something goes wrong, not on
            every read; what blocks the continuation stays above. */}
        <details><summary>{t("common.details")}</summary>
          <p>{t("handoff.capability", { runtime: target.harness, version: preview.data.capability.version || t("handoff.unknown"),
            login: preview.data.capability.login || t("handoff.unknown") })}</p>
          <p>{t("handoff.permission", { previous: preview.data.packet.source_permission_mode,
            next: preview.data.packet.target_permission_mode })}</p>
          <p>{t("handoff.workspace", { branch: preview.data.packet.workspace.branch || t("handoff.unknown"),
            base: preview.data.packet.workspace.base_ref || t("handoff.unknown") })}</p>
          {preview.data.resource.state === "ready" && <p>{t("handoff.resourceReady", {
            revision: String(preview.data.resource.profile_revision) })}</p>}
        </details>
      </div>}
      <button type="button" className="btn small" disabled={!current || busy || offline || !!pending}
        onClick={() => void approve()}>{t("handoff.continue")}</button>
    </>}
    {pending && <p className="result-warning" role="status">{t("handoff.pending")}
      <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void send(pending)}>{t("common.retry")}</button></p>}
    {effectId && <p className="sub" role="status">{t("handoff.effect", { state: t(effect.data || effect.error ? launchFeedback(effect.data, !!effect.error).key : "pboard.launch.pending") })}
      {effect.data?.error && ` · ${effect.data.error}`}</p>}
    {warning && <p className="result-warning" role="alert">{warning}</p>}
  </details>;
}
