import { useState } from "react";
import { api, ApiError } from "../api";
import { locale, t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";

type BundleId = "planning" | "execution" | "execution_project" | "review";
type Bundle = { id: string; operations: string[]; effects: string[]; scope_kind: "project" | "task"; max_expires_at: string; blockers: string[] };
type Grant = { grant_id: string; generation: number; session_id: string; scope: { kind: string; id: string }; operations: string[]; effects: string[];
  expires_at: string; revoked_at: string | null; state: "active" | "expired" | "revoked" | "stale"; receipt_id: string | null;
  parent_grant_id: string | null; parent_grant_generation: number | null };
type Authority = { project_id: string; entity_revision: number; current_coordinator_session_id: string | null;
  available_bundles: Bundle[]; grants: Grant[]; readiness_blockers: string[] };
type Pending = { path: string; body: Record<string, unknown>; kind: "approve" | "revoke"; label: string };

const bundleIds: BundleId[] = ["planning", "execution", "execution_project", "review"];
const rights: Record<BundleId, { scope_kind: "project" | "task"; operations: string[]; effects: string[] }> = {
  planning: { scope_kind: "project", operations: ["board.task.create", "board.task.update"], effects: [] },
  execution: { scope_kind: "task", operations: ["task.launch"], effects: ["execution.start"] },
  execution_project: { scope_kind: "project", operations: ["task.launch"], effects: ["execution.start"] },
  review: { scope_kind: "project", operations: ["review.verdict", "review.return"], effects: [] },
};

function same(a: string[], b: string[]): boolean {
  return a.length === b.length && [...a].sort().join("\0") === [...b].sort().join("\0");
}

function stored(key: string, base: string): Pending | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? "null");
    const validPath = value?.path === base || (typeof value?.path === "string" &&
      new RegExp(`^${base.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/[^/]+/revoke$`).test(value.path));
    return value && validPath && typeof value.body?.client_operation_id === "string"
      && (value.kind === "approve" || value.kind === "revoke") ? value as Pending : null;
  } catch { return null; }
}

function when(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString(locale(), { dateStyle: "medium", timeStyle: "short" }) : t("authority.time.unknown");
}

function bundleName(id: string): string {
  return bundleIds.includes(id as BundleId) ? t(`authority.bundle.${id}`) : t("authority.bundle.unknown");
}

function grantName(grant: Grant): string {
  for (const id of bundleIds) {
    const rule = rights[id];
    if (grant.scope.kind === rule.scope_kind && same(grant.operations, rule.operations) && same(grant.effects, rule.effects))
      return bundleName(id);
  }
  return bundleName("");
}

export function CoordinatorAuthority({ projectId, toast }: { projectId: string; toast: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  const [bundleId, setBundleId] = useState<BundleId>("planning");
  const [taskId, setTaskId] = useState("");
  const [hours, setHours] = useState(1);
  const [withdrawId, setWithdrawId] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const pendingKey = `daedalus.coordinator.authority.${projectId}`;
  const base = `/api/projects/${encodeURIComponent(projectId)}/orchestrator/authority`;
  const [pending, setPending] = useState<Pending | null>(() => stored(pendingKey, base));
  const offline = useOffline();
  const authority = useQuery<Authority>(open ? base : null, { pollMs: 5000, staleMs: 0 });
  const tasks = useQuery<{ tasks: { id: string; title: string; status: string }[] }>(open ? `/api/projects/${encodeURIComponent(projectId)}/board?include_done=1` : null, { staleMs: 5000 });
  const current = authority.data?.project_id === projectId && !authority.error ? authority.data : null;
  const bundle = current?.available_bundles.find((item) => item.id === bundleId);
  const rule = rights[bundleId];
  const validBundle = !!bundle && bundle.scope_kind === rule.scope_kind && same(bundle.operations, rule.operations) && same(bundle.effects, rule.effects);
  const candidates = (tasks.data?.tasks ?? []).filter((task) => task.status !== "done" && task.status !== "dropped");
  const taskReady = bundleId !== "execution" || (!tasks.error && candidates.some((task) => task.id === taskId));
  const expiryLimit = Date.parse(bundle?.max_expires_at ?? "");
  const canApprove = !offline && !busy && !pending && !!current?.current_coordinator_session_id && !!validBundle
    && !bundle?.blockers.length && taskReady && Number.isInteger(current.entity_revision) && current.entity_revision > 0 && Number.isFinite(expiryLimit)
    && expiryLimit > Date.now() + 120000;
  const active = current?.grants.filter((grant) => grant.state === "active") ?? [];
  const past = current?.grants.filter((grant) => grant.state !== "active") ?? [];

  function remember(next: Pending | null) {
    setPending(next);
    try {
      if (next) sessionStorage.setItem(pendingKey, JSON.stringify(next));
      else sessionStorage.removeItem(pendingKey);
    } catch { /* a mounted view still retains the exact request */ }
  }

  async function submit(intent: Pending) {
    if (busy || offline) return;
    setBusy(true);
    try {
      await api.post(intent.path, intent.body);
      remember(null);
      setReason(""); setWithdrawId("");
      toast(t(intent.kind === "approve" ? "authority.approval.recorded" : "authority.withdrawal.recorded"));
      authority.refresh();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        remember(null);
        authority.refresh();
      }
      toast(errorText(error));
    } finally { setBusy(false); }
  }

  async function approve() {
    if (!canApprove || !current || !bundle || !current.current_coordinator_session_id) return;
    const expiry = new Date(Math.min(Date.now() + hours * 3600000, expiryLimit - 60000)).toISOString();
    const scope = bundle.scope_kind === "task" ? candidates.find((task) => task.id === taskId)?.title ?? "" : t("authority.scope.project");
    if (!(await confirmAsync(t("authority.approve.confirm"), {
      body: t("authority.approve.preview", { bundle: bundleName(bundleId), scope, expiry: when(expiry), rights: [...bundle.operations, ...bundle.effects].join(", ") }),
      action: t("authority.approve"),
    }))) return;
    const intent: Pending = { path: base, kind: "approve", label: bundleName(bundleId),
      body: { client_operation_id: crypto.randomUUID(), expected_entity_revision: current.entity_revision,
        expected_coordinator_session_id: current.current_coordinator_session_id,
        bundle_id: bundleId, task_id: bundle.scope_kind === "task" ? taskId : null, expires_at: expiry } };
    remember(intent);
    await submit(intent);
  }

  async function withdraw(grant: Grant) {
    if (busy || offline || pending || !current || !reason.trim() || grant.revoked_at) return;
    if (!(await confirmAsync(t("authority.withdraw.confirm"), {
      body: t("authority.withdraw.preview", { bundle: grantName(grant), expiry: when(grant.expires_at) }),
      action: t("authority.withdraw"), danger: true,
    }))) return;
    const intent: Pending = { path: `${base}/${encodeURIComponent(grant.grant_id)}/revoke`, kind: "revoke", label: grantName(grant),
      body: { client_operation_id: crypto.randomUUID(), expected_entity_revision: current.entity_revision,
        expected_coordinator_session_id: current.current_coordinator_session_id,
        expected_grant_generation: grant.generation, reason: reason.trim() } };
    remember(intent);
    await submit(intent);
  }

  function grantRow(grant: Grant) {
    const chosen = withdrawId === grant.grant_id;
    return <li key={grant.grant_id} className="project-extension">
      <b>{grantName(grant)}</b> · {t(`authority.state.${grant.state}`)}
      <div className="sub">{grant.scope.kind === "task" ? candidates.find((task) => task.id === grant.scope.id)?.title ?? t("authority.scope.task") : t("authority.scope.project")} · {t("authority.until", { time: when(grant.expires_at) })}</div>
      <details><summary>{t("authority.exact")}</summary>
        <div className="mono">{[...grant.operations, ...grant.effects].join(" · ")}</div>
        <div className="mono">{grant.grant_id} · {grant.session_id} · {grant.scope.id}{grant.receipt_id ? ` · ${grant.receipt_id}` : ""}</div>
      </details>
      {!grant.revoked_at && <>{chosen ? <div>
        <label className="field">{t("authority.reason")}<textarea className="field" maxLength={1000} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
        <button type="button" className="btn small danger" disabled={offline || busy || !!pending || !current || !reason.trim()} onClick={() => void withdraw(grant)}>{t("authority.withdraw")}</button>
      </div> : <button type="button" className="btn small ghost" disabled={offline || busy || !!pending || !current} onClick={() => { setWithdrawId(grant.grant_id); setReason(""); }}>{t("authority.withdraw")}</button>}</>}
    </li>;
  }

  return <details className="sheet-section" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("authority.title")}</summary>
    {open && <div className="project-extension-list">
      <p className="sub">{t("authority.intro")}</p>
      {offline && <div className="result-warning" role="status">{t("authority.offline")}</div>}
      {authority.error && <div className="result-warning" role="status">{t("authority.readFailed")} <button type="button" className="linkbtn" onClick={() => authority.refresh()}>{t("common.retry")}</button></div>}
      {!authority.error && !current && <div className="sub">{t("common.loading")}</div>}
      {pending && <div className="result-warning" role="status">{t("authority.pending", { action: pending.label })} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void submit(pending)}>{t("authority.retry")}</button></div>}
      {current && <>
        {current.current_coordinator_session_id ? <p>{t("authority.activeCount", { n: active.length })}</p>
          : <p className="result-warning">{t("authority.noCoordinator")}</p>}
        <ul className="plain-list">{active.map(grantRow)}</ul>
        {past.length > 0 && <details><summary>{t("authority.past", { n: past.length })}</summary><ul className="plain-list">{past.map(grantRow)}</ul></details>}
        <details><summary>{t("authority.add")}</summary>
          <label className="field">{t("authority.bundle")}
            <select className="field" value={bundleId} onChange={(event) => { setBundleId(event.target.value as BundleId); setTaskId(""); }}>
              {current.available_bundles.filter((item) => bundleIds.includes(item.id as BundleId)).map((item) => <option key={item.id} value={item.id}>{bundleName(item.id)}</option>)}
            </select>
          </label>
          <p className="sub">{t(`authority.bundle.help.${bundleId}`)}</p>
          {!validBundle && <div className="result-warning" role="status">{t("authority.definitionChanged")}</div>}
          {bundleId === "execution" && <label className="field">{t("authority.task")}
            <select className="field" value={taskId} disabled={!!tasks.error} onChange={(event) => setTaskId(event.target.value)}>
              <option value="">{t("authority.task.choose")}</option>
              {candidates.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}
            </select>
          </label>}
          {bundleId === "execution" && tasks.error && <div className="result-warning" role="status">{t("authority.tasksFailed")} <button type="button" className="linkbtn" onClick={() => tasks.refresh()}>{t("common.retry")}</button></div>}
          <label className="field">{t("authority.expiry")}
            <select className="field" value={hours} onChange={(event) => setHours(Number(event.target.value))}>
              {[1, 8, 23].map((value) => <option key={value} value={value}>{t("authority.hours", { n: value })}</option>)}
            </select>
          </label>
          <details><summary>{t("authority.exact")}</summary><div className="mono">{[...(bundle?.operations ?? []), ...(bundle?.effects ?? [])].join(" · ")}</div></details>
          <button type="button" className="btn small" disabled={!canApprove} onClick={() => void approve()}>{t("authority.approve")}</button>
        </details>
      </>}
    </div>}
  </details>;
}
