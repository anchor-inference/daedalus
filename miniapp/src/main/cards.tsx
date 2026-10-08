// The main chat's own parts: the projects being set up, and the dispatches under way as cards that say
// where each piece of work stands. Both sit in the conversation's flow, after its latest turn. The
// questions the projects put to the operator are not here: they wait in the panel's Questions tab,
// grouped by project, and the chat carries one line that opens it.

import { useState } from "react";
import { api, type Dispatch, type DispatchMessage, type MainView } from "../api";
import { t } from "../i18n";
import { Icon } from "../icons";
import { projectHome } from "../router";
import { go } from "../shell";
import { invalidate, useQuery } from "../store";
import { relTime } from "../format";
import { confirmAsync, errorText } from "../ui";
import { MAIN_KEY } from "./data";
import { dispatchState, goingDispatches } from "./model";

type Toast = (text: string) => void;

/**
 * What is under way in the main chat, in the conversation's own flow after its latest turn: the
 * projects being set up, and the work handed out. Nothing here is a strip above the chat any more:
 * the operator found the strip of answered and withdrawn lines and "N finished dispatches" useless,
 * and on a phone it took the screen. What is resolved leaves the flow — the reports that closed a
 * dispatch are already lines of the chat.
 */
export function MainFlow({ view, toast }: { view: MainView; toast: Toast }) {
  const going = goingDispatches(view.dispatches);
  if (!going.length && !view.setup.length) return null;
  return (
    <section className="main-flow" aria-label={t("main.board.label")}>
      {view.setup.map((p) => <SetupLine key={p.project_id} projectId={p.project_id} name={p.name} toast={toast} />)}
      {going.length > 0 && (
        <div className="main-dispatches">
          {going.map((d) => <DispatchCard key={d.id} dispatch={d} toast={toast} />)}
        </div>
      )}
    </section>
  );
}

/** Ending a project's setup by hand: the desktop's line and the phone's setup card share it. */
export function useFinishSetup(projectId: string, name: string, toast: Toast): { busy: boolean; finish: () => Promise<void> } {
  const [busy, setBusy] = useState(false);
  async function finish() {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`/api/projects/${encodeURIComponent(projectId)}/setup/finish`, {});
      toast(t("main.setup.finished", { name }));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
      invalidate(MAIN_KEY);
      invalidate("/api/projects");
    }
  }
  return { busy, finish };
}

/** A project that the main orchestrator is setting up, and the button that ends the setup by hand. */
export function SetupLine({ projectId, name, toast }: { projectId: string; name: string; toast: Toast }) {
  const { busy, finish } = useFinishSetup(projectId, name, toast);
  return (
    <div className="main-setup" data-setup={projectId}>
      <Icon name="conductor" size={14} />
      <span className="grow">{t("main.setup.line", { name })}</span>
      <button className="btn small" disabled={busy} onClick={() => void finish()}>{t("main.setup.finish")}</button>
    </div>
  );
}

/** One piece of work handed to a project: whose, what, where it stands, and what the project last said. */
export function DispatchCard({ dispatch, toast }: { dispatch: Dispatch; toast: Toast }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const state = dispatchState(dispatch);
  const going = dispatch.status === "open" || dispatch.status === "blocked";
  const said = dispatch.last;
  async function cancel() {
    if (busy || !(await confirmAsync(t("main.dispatch.cancel.confirm", { title: dispatch.title || `#${dispatch.seq}` }), { action: t("main.dispatch.cancel"), danger: true }))) return;
    setBusy(true);
    try {
      await api.post(`/api/dispatches/${encodeURIComponent(dispatch.id)}/cancel`, {});
      toast(t("main.dispatch.cancelled.toast"));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
      invalidate(MAIN_KEY);
    }
  }
  const home = projectHome(dispatch.project_id);
  return (
    <article className={`dispatch-card tone-${state.tone} ${going ? "going" : "over"}`} data-dispatch={dispatch.id}>
      <div className="dispatch-head">
        <a className="dispatch-project truncate" href={home} onClick={(e) => go(e, home)}>{dispatch.project_name || dispatch.project_id}</a>
        <span className="dispatch-seq num">#{dispatch.seq}</span>
        <span className={`dispatch-state tone-${state.tone}`}>{state.word}</span>
        <span className="dispatch-age faint">{relTime(dispatch.closed_at ?? dispatch.created_at)}</span>
      </div>
      <button className="dispatch-body" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="dispatch-title">{dispatch.title || dispatch.text.split("\n")[0]}</span>
        {said && <span className="dispatch-last truncate">{lastLine(said)}</span>}
      </button>
      {open && <DispatchDetail dispatch={dispatch} />}
      {going && (
        <div className="dispatch-actions">
          {/* A phone has no column of projects beside the chat: the card is the way into its project. */}
          <a className="btn small phone-only dispatch-open" href={home} onClick={(e) => go(e, home)}>{t("common.open")}</a>
          <button className="btn small ghost" disabled={busy} onClick={() => void cancel()}>{t("main.dispatch.cancel")}</button>
        </div>
      )}
    </article>
  );
}

function lastLine(message: DispatchMessage): string {
  return `${t(`main.author.${message.author}`)} · ${t(`main.kind.${["progress", "done", "blocked", "decision", "cancelled", "message"].includes(message.kind) ? message.kind : "message"}`)}: ${message.text.split("\n")[0]}`;
}

function DispatchDetail({ dispatch }: { dispatch: Dispatch }) {
  const { data } = useQuery<Dispatch & { messages: DispatchMessage[] }>(`/api/dispatches/${encodeURIComponent(dispatch.id)}`, { staleMs: 2000 });
  return (
    <div className="dispatch-detail">
      <div className="dispatch-asked">{dispatch.text}</div>
      {(data?.messages ?? []).map((m) => (
        <div key={m.id} className={`dispatch-message author-${m.author}`}>
          <span className="dispatch-message-head">{t(`main.author.${m.author}`)} · {relTime(m.at)}</span>
          <span className="dispatch-message-text">{m.text}</span>
        </div>
      ))}
      {!data && <div className="faint">{t("common.loading")}</div>}
    </div>
  );
}
