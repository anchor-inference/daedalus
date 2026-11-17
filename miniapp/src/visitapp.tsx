// A live link's page: the session's own screen and nothing around it. No rail, no list of chats, no
// settings: the link opens one conversation, and the host's door refuses everything else whatever this
// page asks. What the visitor sends is marked as theirs in the chat, under the name they give here.

import { Suspense, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import { SessionScreen } from "./chunks";
import { startEvents } from "./events";
import { t, useLang } from "./i18n";
import { applyAppearance } from "./appearance";
import { ConfirmHost, ToastHost, toast as showToast } from "./ui/dialogs";
import { VISIT_ENDED, setVisitorName, visitorName } from "./visit";
import { Icon } from "./icons";
import "./visit.css";

/** What the host says about the link the page was opened with. */
export type VisitLink = { session_id: string; title: string; expires_at: string | null };

type Ended = "ended" | "unknown";

export function VisitApp() {
  useLang();
  const [link, setLink] = useState<VisitLink | null>(null);
  const [ended, setEnded] = useState<Ended | null>(null);
  const [failed, setFailed] = useState("");

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    const apply = () => applyAppearance();
    apply();
    mq?.addEventListener("change", apply);
    return () => mq?.removeEventListener("change", apply);
  }, []);

  useEffect(() => {
    const on = () => setEnded((was) => was ?? "ended");
    window.addEventListener(VISIT_ENDED, on);
    return () => window.removeEventListener(VISIT_ENDED, on);
  }, []);

  useEffect(() => {
    api.get<VisitLink>("/api/share").then(setLink).catch((e: unknown) => {
      if (e instanceof ApiError && (e.status === 410 || e.status === 404)) setEnded(e.status === 410 ? "ended" : "unknown");
      else setFailed(e instanceof Error ? e.message : String(e));
    });
  }, []);

  // The session's events, which the host's door narrows to this session whatever the page asks for.
  useEffect(() => (link && !ended ? startEvents() : undefined), [link, ended]);

  useEffect(() => {
    document.title = link?.title ? `${link.title} · Daedalus` : "Daedalus";
  }, [link?.title]);

  if (ended) {
    return (
      <div className="visit">
        <div className="visit-gone" role="status">
          <Icon name="unlink" size={28} />
          <b>{t(ended === "ended" ? "visit.ended.title" : "visit.unknown.title")}</b>
          <span>{t(ended === "ended" ? "visit.ended.body" : "visit.unknown.body")}</span>
        </div>
      </div>
    );
  }
  if (!link) {
    return (
      <div className="visit">
        <div className="visit-gone">{failed ? <><b>{t("visit.failed")}</b><span>{failed}</span></> : t("common.loading")}</div>
      </div>
    );
  }
  return (
    <div className="visit">
      <VisitBar link={link} />
      <div className="main chat-open">
        <Suspense fallback={<div className="empty">{t("common.loading")}</div>}>
          <SessionScreen id={link.session_id} onBack={() => undefined} toast={showToast} visit />
        </Suspense>
      </div>
      <ToastHost />
      <ConfirmHost />
    </div>
  );
}

/** The strip over the conversation: whose conversation this is, and the name the visitor writes under. */
function VisitBar({ link }: { link: VisitLink }) {
  const [name, setName] = useState(visitorName);
  const [editing, setEditing] = useState(false);
  const save = (value: string) => {
    setVisitorName(value);
    setName(visitorName());
    setEditing(false);
  };
  return (
    <div className="visit-bar" role="note">
      <Icon name="share" size={14} />
      <span className="visit-bar-text">{t("visit.bar")}</span>
      {link.expires_at && <span className="visit-bar-until">{t("visit.until", { when: new Date(link.expires_at).toLocaleString() })}</span>}
      <span className="grow" />
      {editing ? (
        <form className="visit-name" onSubmit={(e) => { e.preventDefault(); save(String(new FormData(e.currentTarget).get("name") ?? "")); }}>
          <input className="field" name="name" autoFocus defaultValue={name} maxLength={60} placeholder={t("visit.name.placeholder")} aria-label={t("visit.name.label")} />
          <button className="btn small primary" type="submit">{t("common.save")}</button>
        </form>
      ) : (
        <button type="button" className="btn small ghost visit-name-btn" onClick={() => setEditing(true)} title={t("visit.name.label")}>
          {name ? t("visit.as", { name }) : t("visit.name.set")}
        </button>
      )}
    </div>
  );
}
