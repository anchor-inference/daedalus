import { useState } from "react";
import { t } from "../i18n";
import { useQuery } from "../store";

type Capacity = { cap: number; active: number; reserved: number; available: number };
type QueueEntry = { project_id: string; role_class: "coordinator" | "reviewer" | "worker"; position: number };
type Queue = { entries: QueueEntry[] };

/** Show host-wide admission near a launch, and reveal waiting order only when it matters. */
export function HostCapacity({ projectId, pending }: { projectId: string; pending: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const capacity = useQuery<Capacity>("/api/admission/capacity", { pollMs: 10000, staleMs: 3000 });
  const slots = capacity.data;
  const queue = useQuery<Queue>(expanded || pending || slots?.available === 0 ? "/api/admission/queue" : null, { pollMs: 10000, staleMs: 3000 });
  const places = queue.data?.entries.filter((entry) => entry.project_id === projectId) ?? [];
  const full = slots?.available === 0;

  return (
    <div className={`host-capacity ${full || places.length ? "waiting" : ""}`} role="status">
      <div className="host-capacity-line">
        <span>
          {slots ? t("pboard.capacity.slots", { available: slots.available, cap: slots.cap }) :
            capacity.error ? t("pboard.capacity.unavailable") : t("pboard.capacity.loading")}
          {places.length > 0 && <> · {t("pboard.capacity.position", { position: places[0].position })}</>}
          {full && places.length === 0 && <> · {t("pboard.capacity.full")}</>}
        </span>
        <button type="button" className="linkbtn" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          {t(expanded ? "pboard.capacity.less" : "pboard.capacity.details")}
        </button>
      </div>
      {expanded && (
        <div className="host-capacity-details">
          {slots && <div>{t("pboard.capacity.inUse", { active: slots.active, reserved: slots.reserved })}</div>}
          {capacity.error && <button type="button" className="linkbtn" onClick={capacity.refresh}>{t("common.retry")}</button>}
          {queue.data && (queue.data.entries.length ? (
            <ol>
              {queue.data.entries.map((entry) => (
                <li key={`${entry.position}-${entry.project_id}`}>
                  {entry.project_id === projectId ? t("pboard.capacity.thisProject") : t("pboard.capacity.otherProject")}
                  {" · "}{t(`pboard.capacity.role.${entry.role_class}`)}
                </li>
              ))}
            </ol>
          ) : <div>{t("pboard.capacity.empty")}</div>)}
          {queue.error && <div>{t("pboard.capacity.queueUnavailable")} <button type="button" className="linkbtn" onClick={queue.refresh}>{t("common.retry")}</button></div>}
        </div>
      )}
    </div>
  );
}
