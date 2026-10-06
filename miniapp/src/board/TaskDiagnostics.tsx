import { useState } from "react";
import { bytes } from "../format";
import { t } from "../i18n";
import { useQuery } from "../store";
import { copyText } from "../ui/components";
import type { ProjectTask } from "./board";

type Fault = { kind: string; diagnostic_ref?: string; created_at?: string; cancelled_by?: string };
type Diagnostics = { attempt_id: string; task_id: string; state: string; faults: Fault[]; truncated: boolean };
type Observation = { observation_kind: string; memory_peak_bytes: number | null; oom_kills: number | null; pids_max_events: number | null };
type Resources = { kind: string; limits?: { memory_bytes?: number; process_count?: number }; observation?: Observation | null };

/** Why the host stopped a worker, in words, when a resource limit did it; nothing otherwise. */
export function stopReason(resources: Resources | null | undefined): string | null {
  const seen = resources?.observation;
  if (!seen) return null;
  if (seen.oom_kills) return t("pboard.attempt.oom", { limit: resources?.limits?.memory_bytes ? bytes(resources.limits.memory_bytes) : "?" });
  if (seen.pids_max_events) return t("pboard.attempt.pids", { limit: String(resources?.limits?.process_count ?? "?") });
  return null;
}

/** Load only the host's allowlisted fault categories when the operator opens an attempt. */
export function TaskDiagnostics({ task, toast }: { task: ProjectTask; toast?: (text: string) => void }) {
  const [open, setOpen] = useState(false);
  const attemptId = task.current_attempt_id;
  const base = attemptId ? `/api/board/${encodeURIComponent(task.id)}/attempts/${encodeURIComponent(attemptId)}` : "";
  const query = useQuery<Diagnostics>(open && attemptId ? `${base}/diagnostics` : null);
  const resources = useQuery<Resources>(open && attemptId ? `${base}/resources` : null);
  if (!attemptId || task.status === "done" || task.status === "dropped") return null;
  const reason = stopReason(resources.data);
  // What a bug report needs, already redacted by the host: the fault list and the resource record.
  async function copy() {
    const ok = await copyText(JSON.stringify({ task_id: task.id, diagnostics: query.data, resources: resources.data ?? null }, null, 2));
    toast?.(t(ok ? "pboard.attempt.copied" : "pboard.attempt.copyFailed"));
  }
  return <details className="result-details" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("pboard.attempt.title")}</summary>
    {open && (query.loading ? <p className="sub">{t("common.loading")}</p>
      : query.error ? <p className="result-warning" role="alert">{t("pboard.attempt.unavailable")} <button type="button" className="linkbtn" onClick={() => void query.refresh()}>{t("common.retry")}</button></p>
        : query.data ? <div className="sub">
          <p>{t("pboard.attempt.state", { state: t(`pboard.attempt.state.${query.data.state}`) })}</p>
          {reason && <p className="result-warning">{reason}</p>}
          {query.data.faults.length === 0 ? <p>{t("pboard.attempt.none")}</p>
            : <ul>{query.data.faults.map((fault, index) => <li key={`${fault.diagnostic_ref ?? fault.kind}-${index}`}>
              {t(`pboard.attempt.kind.${fault.kind}`)}{fault.diagnostic_ref && <> · <code>{fault.diagnostic_ref}</code></>}
            </li>)}</ul>}
          {query.data.truncated && <p>{t("pboard.attempt.more")}</p>}
          <button type="button" className="btn small" onClick={() => void copy()}>{t("pboard.attempt.copy")}</button>
        </div> : null)}
  </details>;
}
