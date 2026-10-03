import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import type { ProjectTask } from "./board";

type Kind = "task" | "review" | "check" | "approval";
type StepDraft = { kind: Kind; task_id: string; receipt_status: "accepted" | "rejected" };
type Node = { id: string; kind: Kind; task_id: string; check?: { receipt_status: "accepted" | "rejected" } };
type Definition = { nodes: Node[]; edges: string[][]; budget: { max_steps: number; max_parallel: number } };
type ListedRun = { run_id: string; status: string; created_at: string };
type RunList = { task_entity_revision: number; items: ListedRun[]; next_before: string | null };
type Step = { node_id: string; task_id: string; kind: Kind; status: string; step_revision: number; source_contract_revision: number | null; current_input_digest: string; source_current: boolean; projected_state: string; can_approve: boolean; approval_blocker: string | null; receipt_id: string | null; input_digest: string };
type Run = { run_id: string; status: string; projection_current: boolean; superseded_by: string | null; steps: Step[]; definition_value: Definition };
type Intent = { id: string; fingerprint: string; body: Record<string, unknown> };

const runPath = (id: string) => `/api/board-workflows/runs/${encodeURIComponent(id)}`;
const listPath = (id: string) => `/api/board/${encodeURIComponent(id)}/board-workflows`;
const statusKey = (status: string) => (["pending", "ready", "running", "blocked", "completed", "cancelled", "failed"].includes(status) ? `taskWorkflow.status.${status}` : "taskWorkflow.status.unknown");
const reconcileKey = (decision: string) => decision === "reuse" ? "taskWorkflow.reconcile.reuse" : "taskWorkflow.reconcile.needs_review";
const reasonKey = (reason: string) => (["matching_verified_receipt", "step_not_completed", "source_changed", "effect_missing_or_stale", "artifact_provenance_unknown", "effect_receipt_unknown", "effect_receipt_missing"].includes(reason) ? `taskWorkflow.reason.${reason}` : "taskWorkflow.reason.unknown");
const createKey = (taskId: string) => `task-workflow-create:${taskId}`;
const commandKey = (runId: string) => `task-workflow-command:${runId}`;

function stored<T>(key: string): T | null {
  try { return JSON.parse(sessionStorage.getItem(key) ?? "null") as T | null; }
  catch { return null; }
}

function remember(key: string, value: unknown | null) {
  try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, JSON.stringify(value)); }
  catch { /* The in-memory command still supports an exact retry in this page. */ }
}

function definition(steps: StepDraft[]): Definition {
  const nodes = steps.map((step, index): Node => ({
    id: `step${index + 1}`, kind: step.kind, task_id: step.task_id,
    ...(step.kind === "check" ? { check: { receipt_status: step.receipt_status } } : {}),
  }));
  return { nodes, edges: nodes.slice(1).map((node, index) => [nodes[index].id, node.id]), budget: { max_steps: nodes.length, max_parallel: 1 } };
}

/** A task's persisted workflow is available only when the operator opens its details. */
export function TaskWorkflow({ projectId, task, tasks }: { projectId: string; task: ProjectTask; tasks: ProjectTask[] }) {
  const offline = useOffline();
  const [open, setOpen] = useState(false);
  return <section className="task-workflow">
    <button type="button" className="pboard-fold section-title" aria-expanded={open} onClick={() => setOpen(!open)}>{t("taskWorkflow.title")}</button>
    {open && <WorkflowContent projectId={projectId} task={task} tasks={tasks} offline={offline} />}
  </section>;
}

function WorkflowContent({ projectId, task, tasks, offline }: { projectId: string; task: ProjectTask; tasks: ProjectTask[]; offline: boolean }) {
  const list = useQuery<RunList>(listPath(task.id), { pollMs: 10000, staleMs: 0 });
  const [selected, setSelected] = useState<string | null>(null);
  const [steps, setSteps] = useState<StepDraft[]>([
    { kind: "task", task_id: task.id, receipt_status: "accepted" },
    { kind: "approval", task_id: task.id, receipt_status: "accepted" },
  ]);
  const [advanced, setAdvanced] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [older, setOlder] = useState<ListedRun[]>([]);
  const [olderBefore, setOlderBefore] = useState<string | null>(null);
  const [pending, setPending] = useState<Intent | null>(() => stored<Intent>(createKey(task.id)));
  const intent = useRef<Intent | null>(pending);
  const runs = [...(list.data?.items ?? []), ...older.filter((run) => !list.data?.items.some((item) => item.run_id === run.run_id))];
  const active = runs[0] && ["pending", "running", "blocked"].includes(runs[0].status) ? runs[0] : undefined;
  const runId = selected ?? active?.run_id ?? runs[0]?.run_id ?? null;
  const nextBefore = older.length ? olderBefore : list.data?.next_before;
  const canCreate = ["todo", "doing"].includes(task.status) && !active;
  const availableTasks = tasks.filter((item) => item.status !== "dropped");

  const changeStep = (index: number, patch: Partial<StepDraft>) => {
    if (pending) return;
    setSteps((prior) => prior.map((item, at) => at === index ? { ...item, ...patch } : item));
    intent.current = null;
    setPending(null);
    remember(createKey(task.id), null);
  };
  async function create(retry = false) {
    if (offline || busy || !list.data || (!retry && !canCreate)) return;
    const built = definition(steps);
    const body = {
      project_id: projectId, task_id: task.id, ...built,
      expected_entity_revision: list.data.task_entity_revision,
    };
    const fingerprint = JSON.stringify(body);
    if (!retry && intent.current?.fingerprint !== fingerprint) intent.current = { id: crypto.randomUUID(), fingerprint, body };
    if (retry && pending) intent.current = pending;
    if (!intent.current) return;
    const command = intent.current;
    remember(createKey(task.id), command);
    setBusy(true);
    setMessage("");
    try {
      await api.post("/api/board-workflows/validate", retry ? Object.fromEntries(Object.entries(command.body).filter(([key]) => ["nodes", "edges", "budget"].includes(key))) : built);
      const created = await api.post<{ run_id: string }>("/api/board-workflows/runs", { ...command.body, client_operation_id: command.id });
      intent.current = null;
      setPending(null);
      remember(createKey(task.id), null);
      setSelected(created.run_id);
      list.refresh();
    } catch (error) {
      setMessage(errorText(error));
      if (error instanceof ApiError && error.status === 409) {
        intent.current = null;
        setPending(null);
        remember(createKey(task.id), null);
        list.refresh();
      } else { setPending(command); remember(createKey(task.id), command); list.refresh(); }
    } finally { setBusy(false); }
  }
  async function loadOlder() {
    if (!nextBefore || busy || offline) return;
    setBusy(true);
    setMessage("");
    try {
      const page = await api.get<RunList>(`${listPath(task.id)}?before=${encodeURIComponent(nextBefore)}`);
      setOlder((prior) => [...prior, ...page.items.filter((item) => !prior.some((old) => old.run_id === item.run_id))]);
      setOlderBefore(page.next_before);
    } catch (failure) { setMessage(errorText(failure)); }
    finally { setBusy(false); }
  }

  return <div className="task-workflow-body">
    <p className="sub">{t("taskWorkflow.intro")}</p>
    {list.error && <div className="result-warning" role="status">{t("taskWorkflow.unavailable")} <button type="button" className="linkbtn" onClick={list.refresh}>{t("common.retry")}</button></div>}
    {list.data && <>
      {runId && <RunView key={runId} runId={runId} tasks={tasks} revision={list.data.task_entity_revision} offline={offline} onChange={list.refresh} />}
      {runs.length > 1 && <label className="field">{t("taskWorkflow.previous")}
        <select className="field" value={runId ?? ""} onChange={(event) => setSelected(event.target.value)}>
          {runs.map((item) => <option key={item.run_id} value={item.run_id}>{t(statusKey(item.status))} · {new Date(item.created_at).toLocaleString()}</option>)}
        </select>
      </label>}
      {nextBefore && <button type="button" className="linkbtn" disabled={busy || offline} onClick={() => void loadOlder()}>{t("taskWorkflow.older")}</button>}
      {canCreate && <div className="task-workflow-create">
        <p className="sub">{t("taskWorkflow.default")}</p>
        <details open={advanced} onToggle={(event) => setAdvanced(event.currentTarget.open)}>
          <summary>{t("taskWorkflow.custom")}</summary>
          <p className="sub">{t("taskWorkflow.sequence")}</p>
          {steps.map((step, index) => <div className="task-workflow-step" key={index}>
            <span className="sub">{index + 1}</span>
            <select className="field" aria-label={t("taskWorkflow.kind")} value={step.kind} disabled={!!pending} onChange={(event) => changeStep(index, { kind: event.target.value as Kind })}>
              {(["task", "review", "check", "approval"] as Kind[]).map((kind) => <option key={kind} value={kind}>{t(`taskWorkflow.kind.${kind}`)}</option>)}
            </select>
            <select className="field" aria-label={t("taskWorkflow.sourceTask")} value={step.task_id} disabled={!!pending} onChange={(event) => changeStep(index, { task_id: event.target.value })}>
              {availableTasks.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.title}</option>)}
            </select>
            {step.kind === "check" && <select className="field" aria-label={t("taskWorkflow.receiptStatus")} value={step.receipt_status} disabled={!!pending} onChange={(event) => changeStep(index, { receipt_status: event.target.value as StepDraft["receipt_status"] })}>
              <option value="accepted">{t("taskWorkflow.receipt.accepted")}</option><option value="rejected">{t("taskWorkflow.receipt.rejected")}</option>
            </select>}
            <button className="btn small ghost" type="button" disabled={!!pending || steps.length === 1} onClick={() => { setSteps(steps.filter((_, at) => at !== index)); intent.current = null; setPending(null); remember(createKey(task.id), null); }}>{t("taskWorkflow.removeStep")}</button>
          </div>)}
          {steps.length < 8 && <button className="btn small" type="button" disabled={!!pending} onClick={() => { setSteps([...steps, { kind: "approval", task_id: task.id, receipt_status: "accepted" }]); intent.current = null; setPending(null); remember(createKey(task.id), null); }}>{t("taskWorkflow.addStep")}</button>}
        </details>
        <div className="btnrow"><button className="btn small primary" type="button" disabled={offline || busy || !!list.error || !!pending} onClick={() => void create()}>{t("taskWorkflow.create")}</button></div>
      </div>}
      {pending && <div className="result-warning" role="status">{t("taskWorkflow.commandUnknown")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void create(true)}>{t("common.retry")}</button></div>}
      {message && <div className="result-warning" role="alert">{message}</div>}
      {offline && <div className="result-warning" role="status">{t("result.block.unconfirmed")}</div>}
    </>}
  </div>;
}

function RunView({ runId, tasks, revision, offline, onChange }: { runId: string; tasks: ProjectTask[]; revision: number; offline: boolean; onChange: () => void }) {
  const query = useQuery<Run>(runPath(runId), { pollMs: 5000, staleMs: 0 });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [unknown, setUnknown] = useState(() => !!stored(commandKey(runId)));
  const [reconciled, setReconciled] = useState<Record<string, { decision: string; reason: string }>>({});
  const command = useRef<{ path: string; body: Record<string, unknown> } | null>(stored(commandKey(runId)));
  async function mutate(path: string, body: Record<string, unknown>, retry = false) {
    if (offline || busy) return;
    if (unknown && !retry) return;
    const exact = command.current?.path === path && JSON.stringify({ ...command.current.body, client_operation_id: undefined }) === JSON.stringify(body);
    if (!exact) command.current = { path, body: { ...body, client_operation_id: crypto.randomUUID() } };
    const pending = command.current!;
    remember(commandKey(runId), pending);
    setBusy(true);
    setError("");
    try {
      await api.post(path, pending.body);
      command.current = null;
      setUnknown(false);
      remember(commandKey(runId), null);
      query.refresh();
      onChange();
    } catch (failure) {
      setError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409) { command.current = null; setUnknown(false); remember(commandKey(runId), null); query.refresh(); onChange(); }
      else setUnknown(true);
    } finally { setBusy(false); }
  }
  async function cancel() {
    if (!(await confirmAsync(t("taskWorkflow.cancelTitle"), { body: t("taskWorkflow.cancelBody"), action: t("taskWorkflow.cancel"), danger: true }))) return;
    await mutate(`${runPath(runId)}/cancel`, { expected_entity_revision: revision, reason: "operator_cancelled" });
  }
  async function reconcile(step: Step) {
    setError("");
    try {
      const result = await api.post<{ decision: string; reason: string }>(`${runPath(runId)}/reconcile`, { node_id: step.node_id, expected_input_hash: step.input_digest });
      setReconciled((prior) => ({ ...prior, [step.node_id]: result }));
    } catch (failure) { setError(errorText(failure)); }
  }
  return <div className="task-workflow-run">
    {query.error && <div className="result-warning" role="status">{t("taskWorkflow.unavailable")} <button type="button" className="linkbtn" onClick={query.refresh}>{t("common.retry")}</button></div>}
    {query.data && <>
      <div className="pboard-contract-head"><strong>{t(statusKey(query.data.status))}</strong><button type="button" className="linkbtn" onClick={query.refresh}>{t("common.retry")}</button></div>
      {query.data.superseded_by && <p className="sub">{t("taskWorkflow.superseded")}</p>}
      {!query.data.projection_current && <p className="result-warning">{t("taskWorkflow.sourceChanged")}</p>}
      <ol className="pboard-items">
        {query.data.steps.map((step) => <li key={step.node_id} className="pboard-item">
          <div className="pboard-item-line"><span className="pboard-item-text">{t(`taskWorkflow.kind.${step.kind}`)} · {tasks.find((candidate) => candidate.id === step.task_id)?.title ?? t("taskWorkflow.otherTask")}</span><span className="chip tiny">{t(statusKey(step.status))}</span></div>
          {step.kind === "approval" && step.can_approve && <button type="button" className="btn small primary" disabled={offline || busy || unknown || !!query.error || !Number.isInteger(revision)} onClick={() => void mutate(`${runPath(runId)}/steps/${encodeURIComponent(step.node_id)}/approve`, {
            expected_entity_revision: revision, expected_step_revision: step.step_revision,
            expected_source_contract_revision: step.source_contract_revision,
            expected_input_digest: step.current_input_digest,
          })}>{t("taskWorkflow.approve")}</button>}
          {step.kind === "approval" && !step.can_approve && step.status !== "completed" && <div className="sub">{t(`taskWorkflow.block.${step.approval_blocker ?? "unknown"}`)}</div>}
          {step.status === "completed" && !step.source_current && <div className="result-warning">{t("taskWorkflow.staleStep")}</div>}
          {step.status === "completed" && <div><button type="button" className="linkbtn" disabled={offline} onClick={() => void reconcile(step)}>{t("taskWorkflow.reconcile")}</button>
            {reconciled[step.node_id] && <span className="sub"> · {t(reconcileKey(reconciled[step.node_id].decision))}{reconciled[step.node_id].reason ? ` · ${t(reasonKey(reconciled[step.node_id].reason))}` : ""}</span>}</div>}
        </li>)}
      </ol>
      {["pending", "running", "blocked"].includes(query.data.status) && <button type="button" className="btn small warn" disabled={offline || busy || unknown || !!query.error || !Number.isInteger(revision)} onClick={() => void cancel()}>{t("taskWorkflow.cancel")}</button>}
    </>}
    {unknown && <div className="result-warning" role="status">{t("taskWorkflow.commandUnknown")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => command.current && void mutate(command.current.path, Object.fromEntries(Object.entries(command.current.body).filter(([key]) => key !== "client_operation_id")), true)}>{t("common.retry")}</button></div>}
    {error && <div className="result-warning" role="alert">{error}</div>}
  </div>;
}
