import { useState } from "react";
import { t } from "../i18n";
import { useQuery } from "../store";
import type { ProjectTask } from "./board";

type Fault = { kind: string; diagnostic_ref?: string; created_at?: string; cancelled_by?: string };
type Diagnostics = { attempt_id: string; task_id: string; state: string; faults: Fault[]; truncated: boolean };

/** Load only the host's allowlisted fault categories when the operator opens an attempt. */
export function TaskDiagnostics({ task }: { task: ProjectTask }) {
  const [open, setOpen] = useState(false);
  const attemptId = task.current_attempt_id;
  const query = useQuery<Diagnostics>(open && attemptId
    ? `/api/board/${encodeURIComponent(task.id)}/attempts/${encodeURIComponent(attemptId)}/diagnostics`
    : null);
  if (!attemptId || task.status === "done" || task.status === "dropped") return null;
  return <details className="result-details" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("pboard.attempt.title")}</summary>
    {open && (query.loading ? <p className="sub">{t("common.loading")}</p>
      : query.error ? <p className="result-warning" role="alert">{t("pboard.attempt.unavailable")} <button type="button" className="linkbtn" onClick={() => void query.refresh()}>{t("common.retry")}</button></p>
        : query.data ? <div className="sub">
          <p>{t("pboard.attempt.state", { state: t(`pboard.attempt.state.${query.data.state}`) })}</p>
          {query.data.faults.length === 0 ? <p>{t("pboard.attempt.none")}</p>
            : <ul>{query.data.faults.map((fault, index) => <li key={`${fault.diagnostic_ref ?? fault.kind}-${index}`}>
              {t(`pboard.attempt.kind.${fault.kind}`)}{fault.diagnostic_ref && <> · <code>{fault.diagnostic_ref}</code></>}
            </li>)}</ul>}
          {query.data.truncated && <p>{t("pboard.attempt.more")}</p>}
        </div> : null)}
  </details>;
}
