import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { absTime } from "../format";
import { t } from "../i18n";
import { navigate, projectHome } from "../router";
import { invalidate, useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import type { ProjectTask, TeamMember } from "./board";
import type { ResultReceipt } from "./ResultFlow";
import { ComparisonReview } from "./ComparisonReview";

type Contract = { task_id: string; contract_revision: number; entity_revision: number; folder_id?: string | null; checklist?: { id: string; text: string }[] };
type Candidate = { result_id: string; outcome: string; original_digest: string; checks: unknown[] };
type Alternative = { slot: number; attempt_id: string; state: string; reserved_microusd: number;
  observed_cost_microusd: number | null; physical_exit_verified: boolean; results: Candidate[] };
type Slot = { slot: number; staff_id: string; attempt_id: string | null; funding_state: string; launch_state: string | null; launch_started_at: string | null };
type Group = { group_id: string; task_id: string; contract_revision: number; state: string;
  budget_cap_microusd: number; reserved_microusd: number; selected_result_id: string | null;
  alternatives: Alternative[]; blockers: string[]; slots: Slot[]; created_at?: string };
type History = { task_id: string; groups: Group[]; next_before: string | null };
type LaunchBody = { client_operation_id: string; expected_entity_revision: number; contract_revision: number;
  budget_cap_usd: string; alternatives: { staff_id: string; allowance_usd: string }[] };
type PendingLaunch = { task_id: string; body: LaunchBody };
type PendingClose = { group_id: string; body: { client_operation_id: string; expected_entity_revision: number } };
type Capacity = { project: { id: string; concurrency: number; concurrency_cap: number; orchestrator: boolean } };

function money(micros: number | null | undefined): string {
  if (typeof micros !== "number" || !Number.isSafeInteger(micros) || micros < 0) return t("pair.unknown");
  if (micros % 10_000 === 0) return `$${(micros / 1_000_000).toFixed(2)}`;
  return `$${(micros / 1_000_000).toFixed(6).replace(/0+$/, "")}`;
}

function costBound(group: Group): "within" | "over" | "unknown" {
  if (group.alternatives.length !== 2 || !Number.isSafeInteger(group.budget_cap_microusd) || group.budget_cap_microusd < 0 ||
      group.alternatives.some((item) => !Number.isSafeInteger(item.observed_cost_microusd) || item.observed_cost_microusd === null || item.observed_cost_microusd < 0)) return "unknown";
  const observed = group.alternatives.reduce((sum, item) => sum + BigInt(item.observed_cost_microusd!), 0n);
  return observed > BigInt(group.budget_cap_microusd) ? "over" : "within";
}

function micros(value: string): bigint | null {
  if (!/^(?:0|[1-9]\d{0,8})(?:\.\d{1,6})?$/.test(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  const amount = BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(6, "0"));
  return amount > 0n ? amount : null;
}

function readLaunch(taskId: string): PendingLaunch | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(`daedalus.pair.launch.${taskId}`) ?? "null");
    return value?.task_id === taskId && typeof value.body?.client_operation_id === "string" ? value as PendingLaunch : null;
  } catch { return null; }
}

function keepLaunch(taskId: string, intent: PendingLaunch | null): void {
  try {
    if (intent) sessionStorage.setItem(`daedalus.pair.launch.${taskId}`, JSON.stringify(intent));
    else sessionStorage.removeItem(`daedalus.pair.launch.${taskId}`);
  } catch { /* the mounted component still retains the exact command */ }
}

function closeKey(taskId: string): string { return `daedalus.pair.close.${taskId}`; }

function readClose(taskId: string): PendingClose | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(closeKey(taskId)) ?? "null");
    return value && typeof value.group_id === "string" && typeof value.body?.client_operation_id === "string" ? value as PendingClose : null;
  } catch { return null; }
}

function keepClose(taskId: string, intent: PendingClose | null): void {
  try { if (intent) sessionStorage.setItem(closeKey(taskId), JSON.stringify(intent)); else sessionStorage.removeItem(closeKey(taskId)); }
  catch { /* retain the mounted intent */ }
}

function blocker(code: string): string {
  const kind = code.split(":", 1)[0];
  return t(`pair.block.${["cost_unknown", "cost_cap_exceeded", "exit_unknown", "result_missing", "alternatives_missing", "contract_stale"].includes(kind) ? kind : "other"}`);
}

export function TaskComparison({ task, staff, onChanged, toast }: { task: ProjectTask; staff: TeamMember[];
  onChanged: () => void; toast: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [capacityOpenedAt, setCapacityOpenedAt] = useState(0);
  const [capacityBusy, setCapacityBusy] = useState(false);
  const [capacityWarning, setCapacityWarning] = useState("");
  const [first, setFirst] = useState("");
  const [second, setSecond] = useState("");
  const [firstAllowance, setFirstAllowance] = useState("");
  const [secondAllowance, setSecondAllowance] = useState("");
  const [cap, setCap] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [older, setOlder] = useState<Group[]>([]);
  const [olderBefore, setOlderBefore] = useState<string | null>(null);
  const [olderBusy, setOlderBusy] = useState(false);
  const [olderError, setOlderError] = useState("");
  const [busy, setBusy] = useState(false);
  const [warning, setWarning] = useState("");
  const [pending, setPending] = useState<PendingLaunch | null>(() => readLaunch(task.id));
  const [pendingClose, setPendingClose] = useState<PendingClose | null>(() => readClose(task.id));
  const offline = useOffline();
  const base = `/api/board/${encodeURIComponent(task.id)}`;
  const capacityKey = task.project_id ? `/api/projects/${encodeURIComponent(task.project_id)}/staff?archived=0` : null;
  const capacity = useQuery<Capacity>(open && preparing ? capacityKey : null, { pollMs: 10000, staleMs: 0 });
  const projectCapacity = !capacity.error && !!capacity.updatedAt && capacity.updatedAt >= capacityOpenedAt &&
    capacity.data?.project.id === task.project_id &&
    Number.isSafeInteger(capacity.data.project.concurrency) && capacity.data.project.concurrency >= 1 &&
    Number.isSafeInteger(capacity.data.project.concurrency_cap) && capacity.data.project.concurrency_cap >= capacity.data.project.concurrency ?
      capacity.data.project : null;
  const contract = useQuery<Contract>(open ? `${base}/contract` : null, { staleMs: 0 });
  const history = useQuery<History>(open ? `${base}/comparisons?limit=20` : null, { pollMs: 5000, staleMs: 0 });
  const groups = [...(history.data?.groups ?? []), ...older.filter((item) => !(history.data?.groups ?? []).some((fresh) => fresh.group_id === item.group_id))];
  const groupId = selectedId || groups[0]?.group_id || "";
  const detail = useQuery<Group>(open && groupId ? `${base}/comparisons/${encodeURIComponent(groupId)}` : null,
    { pollMs: 5000, staleMs: 0 });
  const results = useQuery<(ResultReceipt & { attempt_id: string | null })[]>(open && groupId ? `${base}/results` : null,
    { pollMs: 5000, staleMs: 0 });
  const workers = staff.filter((member) => member.harness === "daedalus" && member.isolation === "worktree");
  const firstCost = micros(firstAllowance);
  const secondCost = micros(secondAllowance);
  const capCost = micros(cap);
  const current = contract.data && !contract.error && contract.data.task_id === task.id &&
    contract.data.entity_revision === task.entity_revision ? contract.data : null;
  const hasOpenGroup = groups.some((group) => ["planned", "active", "ready"].includes(group.state));
  useEffect(() => {
    if (capacityWarning && projectCapacity && projectCapacity.concurrency >= 2) setCapacityWarning("");
  }, [capacityWarning, projectCapacity]);
  const canLaunch = !offline && !busy && !pending && !capacityWarning && !history.error && !contract.error && !!history.data && !!current &&
    !capacityBusy && !!projectCapacity && projectCapacity.concurrency >= 2 &&
    !!current.folder_id && task.status === "todo" && !hasOpenGroup && workers.length >= 2 &&
    !!first && !!second && first !== second && !!firstCost && !!secondCost && !!capCost && firstCost + secondCost <= capCost;

  async function raiseCapacity() {
    if (offline || capacityBusy || !capacityKey || !task.project_id || !projectCapacity ||
        !projectCapacity.orchestrator || projectCapacity.concurrency >= 2) return;
    if (!(await confirmAsync(t("pair.capacity.confirm.title"), { body: t("pair.capacity.confirm.body"),
      action: t("pair.capacity.raise") }))) return;
    setCapacityBusy(true);
    setCapacityWarning("");
    try {
      const updated = await api.patch<{ project_id: string; concurrency: number }>(
        `/api/projects/${encodeURIComponent(task.project_id)}/orchestrator`,
        { concurrency: 2, concurrency_cap: Math.max(2, projectCapacity.concurrency_cap) });
      if (updated.project_id !== task.project_id || updated.concurrency !== 2) throw new Error(t("pair.capacity.unconfirmed"));
    } catch (error) {
      setCapacityWarning(errorText(error));
    } finally {
      invalidate(capacityKey);
      await capacity.refresh();
      setCapacityBusy(false);
    }
  }

  async function sendLaunch(intent: PendingLaunch) {
    if (offline || busy) return;
    setBusy(true);
    setWarning("");
    try {
      const receipt = await api.post<{ group_id: string; state: string; receipt_id: string }>(`${base}/comparisons`, intent.body);
      if (!receipt.group_id || !receipt.receipt_id) throw new Error(t("pair.badReceipt"));
      setPending(null);
      keepLaunch(task.id, null);
      setSelectedId(receipt.group_id);
      toast(t("pair.queued"));
      history.refresh();
      detail.refresh();
      contract.refresh();
      onChanged();
    } catch (error) {
      setWarning(errorText(error));
      history.refresh();
      contract.refresh();
    } finally { setBusy(false); }
  }

  async function launch() {
    if (!canLaunch || !current || !firstCost || !secondCost || !capCost) return;
    const a = workers.find((member) => member.id === first)?.name ?? "";
    const b = workers.find((member) => member.id === second)?.name ?? "";
    if (!(await confirmAsync(t("pair.confirm.title"), { body: t("pair.confirm.body", { first: a, second: b,
      firstCost: firstAllowance, secondCost: secondAllowance, cap }), action: t("pair.start") }))) return;
    const intent: PendingLaunch = { task_id: task.id, body: {
      client_operation_id: crypto.randomUUID(), expected_entity_revision: current.entity_revision,
      contract_revision: current.contract_revision, budget_cap_usd: cap,
      alternatives: [{ staff_id: first, allowance_usd: firstAllowance }, { staff_id: second, allowance_usd: secondAllowance }],
    } };
    setPending(intent);
    keepLaunch(task.id, intent);
    await sendLaunch(intent);
  }

  async function abandonPending() {
    if (!pending || !history.data || history.error) return;
    if (!(await confirmAsync(t("pair.abandon.title"), { body: t("pair.abandon.body"), action: t("pair.abandon") }))) return;
    setPending(null);
    keepLaunch(task.id, null);
  }

  async function loadOlder() {
    const before = olderBefore ?? history.data?.next_before;
    if (!before || olderBusy) return;
    setOlderBusy(true);
    setOlderError("");
    try {
      const page = await api.get<History>(`${base}/comparisons?limit=20&before=${encodeURIComponent(before)}`);
      if (page.task_id !== task.id || !Array.isArray(page.groups)) throw new Error(t("pair.readFailed"));
      setOlder((previous) => [...previous, ...page.groups.filter((item) => !previous.some((old) => old.group_id === item.group_id))]);
      setOlderBefore(page.next_before);
    } catch (error) { setOlderError(errorText(error)); }
    finally { setOlderBusy(false); }
  }

  async function closeGroup(intent?: PendingClose) {
    if (offline || busy || (!intent && (!current || !selected || !["planned", "active", "ready"].includes(selected.state)))) return;
    if (!intent) {
      if (!current || !selected) return;
      if (!(await confirmAsync(t("pair.close.title"), { body: t("pair.close.body"), action: t("pair.close") }))) return;
      intent = { group_id: selected.group_id, body: { client_operation_id: crypto.randomUUID(), expected_entity_revision: current.entity_revision } };
      setPendingClose(intent);
      keepClose(task.id, intent);
    }
    setBusy(true);
    try {
      const receipt = await api.post<{ receipt_id: string; state: string }>(`${base}/comparisons/${encodeURIComponent(intent.group_id)}/close`, intent.body);
      if (!receipt.receipt_id || receipt.state !== "blocked") throw new Error(t("pair.badReceipt"));
      setPendingClose(null);
      keepClose(task.id, null);
      history.refresh();
      detail.refresh();
      contract.refresh();
      onChanged();
      toast(t("pair.closed"));
    } catch (error) { setWarning(errorText(error)); history.refresh(); detail.refresh(); }
    finally { setBusy(false); }
  }

  async function discardClose() {
    if (!pendingClose || !(await confirmAsync(t("pair.abandon.title"), { body: t("pair.close.abandon.body"), action: t("pair.abandon") }))) return;
    setPendingClose(null);
    keepClose(task.id, null);
  }

  const selected = detail.data?.group_id === groupId && !detail.error ? detail.data : null;
  const resultRows = Array.isArray(results.data) ? results.data : [];
  const noRead = offline || !!history.error || !!contract.error || !!detail.error || !!results.error;

  return <details className="result-details" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("pair.title")}</summary>
    {open && <div className="project-extension-list">
      <p className="sub">{t("pair.intro")}</p>
      {(offline || history.error || contract.error) && <div className="result-warning" role="status">{t("pair.readFailed")} <button type="button" className="linkbtn" onClick={() => { history.refresh(); contract.refresh(); }}>{t("common.retry")}</button></div>}
      {pending && <div className="result-warning" role="status">{t("pair.pending")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void sendLaunch(pending)}>{t("common.retry")}</button> <button type="button" className="linkbtn" disabled={offline || busy || !history.data || !!history.error} onClick={() => void abandonPending()}>{t("pair.abandon")}</button></div>}
      {warning && <div className="result-warning" role="status">{warning}</div>}
      <details onToggle={(event) => { setPreparing(event.currentTarget.open); if (event.currentTarget.open) setCapacityOpenedAt(Date.now()); }}><summary>{t("pair.prepare")}</summary>
        <p className="sub">{t("pair.budgetHint")}</p>
        {preparing && <div className="sub" role="status">
          {projectCapacity ? t("pair.capacity.current", { n: projectCapacity.concurrency, cap: projectCapacity.concurrency_cap }) :
            t(capacity.error || !task.project_id ? "pair.capacity.unavailable" : "pair.capacity.loading")}
          {projectCapacity && projectCapacity.concurrency < 2 && projectCapacity.orchestrator && <>
            {" "}{t("pair.capacity.needTwo")}{" "}
            <button type="button" className="linkbtn" disabled={offline || capacityBusy || busy || !!pending}
              onClick={() => void raiseCapacity()}>{t("pair.capacity.raise")}</button>
          </>}
          {projectCapacity && projectCapacity.concurrency < 2 && !projectCapacity.orchestrator && task.project_id && <>
            {" "}{t("pair.capacity.enableHint")}{" "}
            <button type="button" className="linkbtn" onClick={() => navigate(projectHome(task.project_id!))}>{t("pair.capacity.setup")}</button>
          </>}
          {!projectCapacity && !!capacityKey && <>
            {" "}<button type="button" className="linkbtn" disabled={offline || capacityBusy} onClick={() => void capacity.refresh()}>{t("common.retry")}</button>
          </>}
        </div>}
        {capacityWarning && <div className="result-warning" role="status">{t("pair.capacity.unconfirmed")} {capacityWarning}</div>}
        {workers.length < 2 && <div className="result-warning">{t("pair.twoWorkers")}</div>}
        {!current?.folder_id && <div className="result-warning">{t("pair.folderMissing")}</div>}
        {task.status !== "todo" && <div className="result-warning">{t("pair.taskUnavailable")}</div>}
        {hasOpenGroup && <div className="result-warning">{t("pair.groupOpen")}</div>}
        <label className="field">{t("pair.first")}
          <select className="field" value={first} onChange={(event) => setFirst(event.target.value)}><option value="">{t("pair.chooseWorker")}</option>{workers.map((member) => <option key={member.id} value={member.id}>{member.name}</option>)}</select>
        </label>
        <label className="field">{t("pair.firstAllowance")}
          <input className="field" inputMode="decimal" value={firstAllowance} onChange={(event) => setFirstAllowance(event.target.value)} placeholder="0.00" />
        </label>
        <label className="field">{t("pair.second")}
          <select className="field" value={second} onChange={(event) => setSecond(event.target.value)}><option value="">{t("pair.chooseWorker")}</option>{workers.filter((member) => member.id !== first).map((member) => <option key={member.id} value={member.id}>{member.name}</option>)}</select>
        </label>
        <label className="field">{t("pair.secondAllowance")}
          <input className="field" inputMode="decimal" value={secondAllowance} onChange={(event) => setSecondAllowance(event.target.value)} placeholder="0.00" />
        </label>
        <label className="field">{t("pair.cap")}
          <input className="field" inputMode="decimal" value={cap} onChange={(event) => setCap(event.target.value)} placeholder="0.00" />
        </label>
        {!!firstCost && !!secondCost && !!capCost && firstCost + secondCost > capCost && <div className="result-warning">{t("pair.capTooSmall")}</div>}
        <button type="button" className="btn small" disabled={!canLaunch} onClick={() => void launch()}>{t("pair.start")}</button>
      </details>
      {groups.length > 0 && <>
        <label className="field">{t("pair.history")}
          <select className="field" value={groupId} onChange={(event) => setSelectedId(event.target.value)}>{groups.map((group) => <option key={group.group_id} value={group.group_id}>{group.created_at ? absTime(group.created_at) : t("pair.recent")} · {t(`pair.state.${group.state}`)}</option>)}</select>
        </label>
        {(olderBefore ?? history.data?.next_before) && <button type="button" className="linkbtn" disabled={olderBusy} onClick={() => void loadOlder()}>{t("pair.older")}</button>}
        {olderError && <div className="result-warning">{olderError}</div>}
      </>}
      {selected && <>
        <div className="sub">{t(`pair.state.${selected.state}`)} · {t("pair.reserved", { amount: money(selected.reserved_microusd), cap: money(selected.budget_cap_microusd) })}</div>
        {selected.blockers.length > 0 && <ul className="plain-list">{[...new Set(selected.blockers.map(blocker))].map((reason) => <li key={reason} className="result-warning">{reason}</li>)}</ul>}
        {costBound(selected) === "over" && !selected.blockers.some((item) => item.startsWith("cost_cap_exceeded")) && <div className="result-warning">{t("pair.costExceeded")}</div>}
        {selected.slots.map((slot) => {
          const alternative = selected.alternatives.find((item) => item.slot === slot.slot);
          const latest = alternative?.results.at(-1);
          const result = latest && alternative ? resultRows.find((item) => item.result_id === latest.result_id && item.attempt_id === alternative.attempt_id) ?? null : null;
          const worker = staff.find((item) => item.id === slot.staff_id)?.name ?? t("pair.workerUnknown");
          return <details key={slot.slot} className="result-details"><summary>{worker} · {t("pair.slot", { n: slot.slot })}</summary>
            <div className="sub">{t("pair.launchState", { state: slot.launch_state ? t(`pair.effect.${slot.launch_state}`) : t("pair.unknown") })}</div>
            <div className="sub">{t("pair.cost", { amount: money(alternative?.observed_cost_microusd), allowance: money(alternative?.reserved_microusd) })}</div>
            <div className="sub">{alternative?.physical_exit_verified ? t("pair.exitObserved") : t("pair.exitUnknown")}</div>
            {alternative?.results.map((candidate) => <div className="sub" key={candidate.result_id}>{candidate.result_id === latest?.result_id ? t("pair.latestResult") : t("pair.earlierResult")} · {t(`result.outcome.${candidate.outcome}`)}</div>)}
            {result && <div className="project-extension"><b>{t(`result.outcome.${result.outcome}`)}</b>
              <p className="sub">{result.original_preview || t("pair.noPreview")}</p>
              <div className="sub">{t(`result.verification.${result.verification}`)} · {result.verdict_accepted ? t("pair.reviewed") : t("pair.reviewPending")}</div>
              <div className="sub">{t("pair.artifacts", { n: result.artifacts.length })}</div></div>}
            <ComparisonReview key={`${selected.group_id}:${slot.slot}`} task={task} groupId={selected.group_id}
              slot={slot.slot} candidate={result} contract={current} attemptId={alternative?.attempt_id ?? null}
              groupOpen={["planned", "active", "ready"].includes(selected.state)} groupReady={selected.blockers.length === 0 && costBound(selected) === "within" &&
                ["active", "ready"].includes(selected.state)} onChanged={() => { detail.refresh(); results.refresh(); contract.refresh(); onChanged(); }} toast={toast} />
          </details>;
        })}
        {(["planned", "active", "ready"].includes(selected.state) || pendingClose?.group_id === selected.group_id) && <div className="btnrow">
          <button type="button" className="btn small" disabled={offline || busy || (!current && !pendingClose) || (!!pendingClose && pendingClose.group_id !== selected.group_id)}
            onClick={() => void closeGroup(pendingClose?.group_id === selected.group_id ? pendingClose : undefined)}>{pendingClose?.group_id === selected.group_id ? t("pair.closeRetry") : t("pair.close")}</button>
          <span className="sub">{t("pair.closeHint")}</span>
          {pendingClose?.group_id === selected.group_id && <button type="button" className="linkbtn" disabled={busy} onClick={() => void discardClose()}>{t("pair.abandon")}</button>}
        </div>}
        {["planned", "active", "ready"].includes(selected.state) && <div className="sub">{t("pair.selectionPending")}</div>}
        <details><summary>{t("pair.details")}</summary><code style={{ overflowWrap: "anywhere" }}>{selected.group_id}</code><ul>{selected.blockers.map((item) => <li key={item}><code>{item}</code></li>)}</ul></details>
      </>}
      {groupId && !selected && <div className="result-warning" role="status">{t("pair.readFailed")} <button type="button" className="linkbtn" onClick={() => detail.refresh()}>{t("common.retry")}</button></div>}
      {noRead && <div className="sub">{t("pair.uncertain")}</div>}
    </div>}
  </details>;
}
