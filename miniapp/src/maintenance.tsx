import { useEffect, useState } from "react";
import { api } from "./api";
import { Sheet } from "./ui/dialogs";
import { t } from "./i18n";
import "./maintenance.css";

type Notice = { id: string; stage: string; restart_at: number };

/** Lives in the authenticated shell, so navigation never hides a scheduled restart. */
export function MaintenanceNotice() {
  const [notice, setNotice] = useState<Notice | null>(null);
  const [dismissed, setDismissed] = useState("");
  const [now, setNow] = useState(Date.now() / 1000);
  const [offset, setOffset] = useState(0);
  useEffect(() => {
    let alive = true;
    let inFlight = false;
    const poll = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const result = await api.get<{ notice: Notice | null; server_time: number }>("/api/maintenance");
        if (alive) {
          setNotice(result.notice);
          setOffset((result.server_time || Date.now() / 1000) - Date.now() / 1000);
        }
      } catch {
        // Keep the last warning through the very outage it announces.
      } finally { inFlight = false; }
    };
    void poll();
    const timer = setInterval(() => void poll(), 3000);
    const clock = setInterval(() => setNow(Date.now() / 1000), 1000);
    const focus = () => void poll();
    window.addEventListener("focus", focus);
    document.addEventListener("visibilitychange", focus);
    return () => { alive = false; clearInterval(timer); clearInterval(clock); window.removeEventListener("focus", focus); document.removeEventListener("visibilitychange", focus); };
  }, []);
  if (!notice) return null;
  const seconds = Math.max(0, Math.ceil(notice.restart_at - now - offset));
  const message = notice.stage === "restart_pending" && seconds > 0 ? t("deps.restartIn", { n: String(seconds) }) : t("deps.restartNow");
  return <>
    <div className="maintenance-strip" role="status">
      <span>{message}</span>
      <button className="btn" onClick={() => setDismissed("")}>{t("deps.noticeDetails")}</button>
    </div>
    {dismissed !== notice.id && <Sheet title={t("deps.restartTitle")} onClose={() => setDismissed(notice.id)}>
      <p className="maintenance-countdown" role="status">{message}</p>
      <p>{t("deps.restartWarning")}</p>
      <button className="btn primary" onClick={() => setDismissed(notice.id)}>{t("deps.noticeUnderstood")}</button>
    </Sheet>}
  </>;
}
