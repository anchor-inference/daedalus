// Pick one of the sessions: the one to open beside the current chat, or the one to be a custom page's
// agent. A sheet with a filter over the sessions' titles, newest first as the list answers them.

import { useEffect, useState } from "react";
import { api, type SessionList, type SessionSummary } from "./api";
import { StatusLabel } from "./ui/components";
import { Sheet } from "./ui/dialogs";
import { t } from "./i18n";

export function SessionPicker({ title, exclude, onPick, onClose }: { title: string; exclude: string | null; onPick: (id: string) => void; onClose: () => void }) {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [filter, setFilter] = useState("");
  useEffect(() => {
    api.get<SessionList>("/api/sessions").then((listing) => setSessions(listing.sessions)).catch(() => setSessions([]));
  }, []);
  const q = filter.trim().toLowerCase();
  const items = (sessions ?? []).filter((s) => s.id !== exclude && (!q || s.title.toLowerCase().includes(q) || s.id.includes(q)));
  return (
    <Sheet title={title} onClose={onClose} size="narrow">
      <input className="field" autoFocus placeholder={t("app.beside.filter")} value={filter} onChange={(e) => setFilter(e.target.value)} style={{ marginBottom: 8 }} />
      {sessions === null && <div className="empty">{t("common.loading")}</div>}
      {sessions !== null && items.length === 0 && <div className="empty">{t("app.beside.empty")}</div>}
      {items.map((s) => (
        <button key={s.id} className="menu-item" onClick={() => onPick(s.id)} data-session-pick={s.id}>
          <span className="grow truncate">{s.title}</span>
          <StatusLabel status={s.status} />
        </button>
      ))}
    </Sheet>
  );
}
