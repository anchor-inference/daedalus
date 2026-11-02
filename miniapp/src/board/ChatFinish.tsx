// A card its own chat finished: which chat, when, and the result it gave, with the operator's way to
// take the card back to work. The operator's Close and a team's accepted report are decisions the
// operator saw being made; a chat finishing its own task is not, so the card spells it out instead of
// leaving it to be read out of the result history.

import { useState } from "react";
import { api } from "../api";
import { absTime, relTime } from "../format";
import { t } from "../i18n";
import { useOffline } from "../store";
import { errorText } from "../ui";

export type ChatFinishInfo = { session_id: string; title: string | null; note: string; at: string };

export function ChatFinish({ task, onOpenSession, onChanged, toast }: {
  task: { id: string; status: string; entity_revision?: number; finished_by?: ChatFinishInfo | null };
  onOpenSession: (sessionId: string) => void; onChanged: () => void; toast: (text: string) => void;
}) {
  const offline = useOffline();
  const [busy, setBusy] = useState(false);
  const finished = task.finished_by;
  if (!finished || task.status !== "done") return null;
  const revision = task.entity_revision;
  async function reopen() {
    if (busy || offline || typeof revision !== "number") return;
    setBusy(true);
    try {
      // The host takes a chat's finish back only on the operator's word: the result stays in the
      // history, a return is recorded against it, and the card is open again.
      await api.put(`/api/board/${encodeURIComponent(task.id)}`, { status: "todo", expected_entity_revision: revision, client_operation_id: crypto.randomUUID() });
      toast(t("board.finishedBy.reopened"));
      onChanged();
    } catch (error) {
      toast(errorText(error));
    } finally { setBusy(false); }
  }
  return (
    <section className="sheet-section chat-finish">
      <div className="sheet-section-title">{t("board.finishedBy.title")}</div>
      <div className="sub">
        {t("board.finishedBy.who")} <button type="button" className="linkbtn" onClick={() => onOpenSession(finished.session_id)}>{finished.title || t("board.of.gone")}</button>
        <span className="sep"> · </span><span title={absTime(finished.at)}>{relTime(finished.at)}</span>
      </div>
      {finished.note ? <div className="proposal-text">{finished.note}</div> : <div className="sub">{t("board.finishedBy.noNote")}</div>}
      <div className="btnrow">
        <button type="button" className="btn small" disabled={busy || offline || typeof revision !== "number"} onClick={() => void reopen()}>{t("board.finishedBy.reopen")}</button>
      </div>
    </section>
  );
}
