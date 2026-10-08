// A staff member's open request answered on a phone, away from the place it was asked: the Terminals
// screen's card and the team's member page. The answers are the ones the project's banner gives
// (AskAnswers in project/phone.tsx) and go to the same route, but weighted for being out of context:
// no answer is the white primary, a refusal is red and one tap, and "Always" — a standing grant —
// sits beside "Allow once" as an equal, never as the default.

import { useState, type FormEvent } from "react";
import { api, ApiError, type Ask } from "../api";
import { t } from "../i18n";
import { Icon } from "../icons";
import { invalidate, useOffline } from "../store";
import { errorText } from "../ui";
import { answeredBy } from "./model";

const enc = encodeURIComponent;

export function PhoneAskAnswers({ ask, projectId, toast, always = false, server = "", onDone }: { ask: Ask; projectId: string; toast: (text: string) => void; always?: boolean; server?: string; onDone?: () => void }) {
  const offline = useOffline();
  const [writing, setWriting] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const options = Array.isArray(ask.detail?.options) ? ask.detail.options.filter((o): o is string => typeof o === "string") : [];
  const permission = ask.kind === "permission" || ask.kind === "folder";
  async function send(body: { selected?: string[]; text?: string; allow?: boolean; always?: boolean; server?: boolean }) {
    if (busy || offline) return;
    setBusy(true);
    try {
      await api.post(`/api/asks/${enc(ask.id)}/answer`, body);
      toast(t("focus.ask.sent"));
      onDone?.();
    } catch (e) {
      const who = e instanceof ApiError && e.status === 409 ? answeredBy(e.message) : "";
      toast(e instanceof ApiError && e.status === 409 ? (who ? t("perm.conflict.by", { who: t(`perm.by.${who}`) }) : t("focus.ask.conflict")) : errorText(e));
    } finally {
      setBusy(false);
      invalidate(`/api/asks?project=${enc(projectId)}`);
      invalidate(`/api/projects/${enc(projectId)}/board`);
      invalidate(`/api/projects/${enc(projectId)}/staff`);
      invalidate("/api/terminals");
      if (body.always && ask.staff_id) invalidate(`/api/staff/${enc(ask.staff_id)}/session`);
    }
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const words = text.trim();
    if (ask.kind === "permission") void send({ allow: false, text: words });
    else if (words) void send({ text: words });
  };
  if (writing) {
    return (
      <form className="ph-ask-own" onSubmit={submit}>
        <input className="ph-field" autoFocus value={text} maxLength={4000} enterKeyHint="send"
          placeholder={t(ask.kind === "permission" ? "phone.ask.why" : "focus.ask.placeholder")} aria-label={t(ask.kind === "permission" ? "phone.ask.why" : "focus.ask.placeholder")}
          onChange={(e) => setText(e.target.value)} />
        <div className="ph-foot-row even">
          <button type="button" className="ph-btn" onClick={() => setWriting(false)}>{t("common.cancel")}</button>
          <button type="submit" className={`ph-btn ${ask.kind === "permission" ? "danger" : ""}`} disabled={busy || offline || (ask.kind !== "permission" && !text.trim())}>{t("focus.ask.send")}</button>
        </div>
      </form>
    );
  }
  return (
    <div className="ph-ask">
      {offline && <div className="ph-ask-note" role="status">{t("focus.attention.offline")}</div>}
      <div className="ph-ask-row">
        {permission ? (
          <>
            <button type="button" className="ph-btn" data-answer="allow" disabled={busy || offline} onClick={() => void send({ allow: true })}>{t(ask.kind === "folder" ? "focus.ask.yes" : "ph.ask.once")}</button>
            {always && ask.kind === "permission" && <button type="button" className="ph-btn" data-answer="always" disabled={busy || offline} onClick={() => void send({ allow: true, always: true })}>{t("perm.always")}</button>}
            <button type="button" className="ph-btn danger" data-answer="deny" disabled={busy || offline} onClick={() => void send({ allow: false })}>{t(ask.kind === "folder" ? "focus.ask.no" : "ph.ask.reject")}</button>
          </>
        ) : (
          options.map((option) => <button key={option} type="button" className="ph-btn" disabled={busy || offline} onClick={() => void send({ selected: [option] })}>{option}</button>)
        )}
      </div>
      {always && server && ask.kind === "permission" && (
        <button type="button" className="ph-btn ghost ph-ask-wide" data-answer="always-server" disabled={busy || offline} onClick={() => void send({ allow: true, always: true, server: true })}>{t("perm.always.server", { server })}</button>
      )}
      {ask.kind !== "folder" && (
        <button type="button" className="ph-btn ghost ph-ask-wide" disabled={busy || offline} onClick={() => setWriting(true)}><Icon name="pen" size={16} />{t(ask.kind === "permission" ? "phone.ask.denyWhy" : "phone.ask.write")}</button>
      )}
    </div>
  );
}
