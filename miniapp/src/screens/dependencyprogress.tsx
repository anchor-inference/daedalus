import { useEffect, useState } from "react";
import { t } from "../i18n";
import { duration } from "../format";

export type Progress = { started_at?: number; updated_at?: number; last_output_at?: number; restart_at?: number; stage?: string; detail_stage?: string; progress?: { at: number; stage: string; detail?: string }[]; log?: string };
/** A stopwatch reading, "4:07" or "3:07:12". Minutes alone ran past sixty: a long build read "187:12",
 *  which nobody parses as three hours, so an hour or more gets its own field. */
export function clock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor(whole / 60) % 60;
  const rest = String(whole % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}
/** The dot's colour for a stage that is not moving: a patch waiting for the operator is the one state
 *  that asks for something, and in the neutral grey of a passive line it read as nothing pending. */
const RESTING_TONE: Record<string, string> = { ready: "waiting", restart_pending: "waiting", completed: "ok", failed: "failed" };

export function DependencyProgress({ value, active, installation = false }: { value: Progress; active: boolean; installation?: boolean }) {
  const [now, setNow] = useState(Date.now() / 1000);
  useEffect(() => { if (!active) return; const timer = setInterval(() => setNow(Date.now() / 1000), 1000); return () => clearInterval(timer); }, [active]);
  const events = value.progress || [];
  const stage = value.stage === "building" ? value.detail_stage || "building" : value.stage || events.at(-1)?.stage;
  const started = value.started_at || events[0]?.at;
  const elapsed = started ? clock((active ? now : value.updated_at || now) - started) : null;
  const last = Math.max(value.updated_at || 0, value.last_output_at || 0);
  // An installation's card is already titled "Installation failed"; a second "Operation failed" under it
  // read as a second failure.
  const heading = stage && !(installation && stage === "failed");
  return <div className="deps-progress">
    {heading && <div className="deps-progress-heading" role="status"><span><span className={active ? "live-dot" : `dot ${RESTING_TONE[stage] ?? ""}`.trim()} /><b>{t(`deps.stage.${stage}`)}</b></span>{elapsed && <time>{elapsed}</time>}</div>}
    {active && <div className="deps-progress-rail" aria-hidden="true"><i /></div>}
    <div className="deps-progress-meta">
      {active && last > 0 && <span>{t("deps.lastActivity", { n: duration((now - last) * 1000) })}</span>}
      {active && installation && <span>{stage === "restarting" ? t("deps.restartNow") : value.restart_at ? t("deps.restartIn", { n: String(Math.max(0, Math.ceil(value.restart_at - now))) }) : t("deps.estimateShort")}</span>}
    </div>
    {(events.length > 0 || value.log) && <div className="deps-progress-disclosures">
    {events.length > 0 && <details><summary>{t("deps.activityCount", { n: String(events.length) })}</summary><ol className="deps-events">
      {events.map((event, index) => <li key={index}><time>{clock(event.at - (started || event.at))}</time><span>{t(`deps.stage.${event.stage === "building" && event.detail ? event.detail : event.stage}`)}{event.detail && event.stage !== "building" && <small>{event.detail}</small>}</span></li>)}
    </ol></details>}
    {value.log && <details><summary>{t("deps.installLog")}</summary><pre className="deps-live-log">{value.log}</pre></details>}
    </div>}
  </div>;
}
