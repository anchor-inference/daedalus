// A live link's page: the session's own screen and nothing around it. No rail, no list of chats, no
// settings: the link opens one conversation, and the host's door refuses everything else whatever this
// page asks. What the visitor sends is marked as theirs in the chat, under the name they give here.

import { Suspense, useEffect, useState, type FormEvent } from "react";
import { api, ApiError } from "./api";
import { SessionScreen } from "./chunks";
import { startEvents } from "./events";
import { t, useLang } from "./i18n";
import { applyAppearance } from "./appearance";
import { ConfirmHost, ToastHost, toast as showToast } from "./ui/dialogs";
import { VISIT_ENDED, VISIT_LOCKED, VISIT_SPENT, setVisitLimits, setVisitPick, setVisitorName, visitorName, type VisitLimits, type VisitPick } from "./visit";
import { Icon } from "./icons";
import { Button } from "./ui/primitives";
import "./visit.css";

/** What the host says about the link the page was opened with. */
export type VisitLink = { session_id: string; title: string; expires_at: string | null; limits?: VisitLimits; pick?: VisitPick | null };

type Ended = "ended" | "unknown";

export function VisitApp() {
  useLang();
  const [link, setLink] = useState<VisitLink | null>(null);
  const [ended, setEnded] = useState<Ended | null>(null);
  const [failed, setFailed] = useState("");
  const [locked, setLocked] = useState(false);
  const [spent, setSpent] = useState(false);
  const [asked, setAsked] = useState(0);

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
    // Any request answering 401 means the pass is missing or stale (the operator changed the password):
    // the conversation goes away and the password screen takes its place.
    const lock = () => { setLocked(true); setLink(null); };
    const spend = () => setSpent(true);
    window.addEventListener(VISIT_LOCKED, lock);
    window.addEventListener(VISIT_SPENT, spend);
    return () => { window.removeEventListener(VISIT_LOCKED, lock); window.removeEventListener(VISIT_SPENT, spend); };
  }, []);

  useEffect(() => {
    if (locked) return;
    api.get<VisitLink>("/api/share").then((got) => {
      setVisitLimits(got.limits ?? null); setVisitPick(got.pick ?? null);
      const left = got.limits?.budget;
      if (left && ((left.usd && left.usd.spent >= left.usd.limit) || (left.weekly_pct && left.weekly_pct.spent >= left.weekly_pct.limit))) setSpent(true);
      setLink(got);
    }).catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 401) setLocked(true);
      else if (e instanceof ApiError && (e.status === 410 || e.status === 404)) setEnded(e.status === 410 ? "ended" : "unknown");
      else setFailed(e instanceof Error ? e.message : String(e));
    });
  }, [locked, asked]);

  // A spent budget is heard from a 402 on a send; the link's own reading says how much is left, so it is
  // asked again then and every minute, which keeps the indicator honest while the agent works.
  useEffect(() => {
    if (!link || locked) return;
    const again = () => api.get<VisitLink>("/api/share").then((got) => { setVisitLimits(got.limits ?? null); setVisitPick(got.pick ?? null); setLink(got); }).catch(() => undefined);
    const timer = window.setInterval(again, 60000);
    if (spent) void again();
    return () => window.clearInterval(timer);
  }, [!!link, locked, spent]);

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
  if (locked) return <UnlockScreen onOpen={() => { setLocked(false); setAsked((n) => n + 1); }} />;
  if (!link) {
    return (
      <div className="visit">
        <div className="visit-gone">{failed ? <><b>{t("visit.failed")}</b><span>{failed}</span></> : t("common.loading")}</div>
      </div>
    );
  }
  return (
    <div className={`visit ${spent ? "spent" : ""} ${link.limits?.terminals === "none" ? "no-terminals" : ""}`}>
      <VisitBar link={link} />
      {spent && <div className="visit-spent" role="alert"><Icon name="alert" size={14} /> <span>{t("visit.spent")}</span></div>}
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
      <BudgetLeft limits={link.limits} />
      {link.expires_at && <span className="visit-bar-until">{t("visit.until", { when: new Date(link.expires_at).toLocaleString() })}</span>}
      <span className="grow" />
      {editing ? (
        <form className="visit-name" onSubmit={(e) => { e.preventDefault(); save(String(new FormData(e.currentTarget).get("name") ?? "")); }}>
          <input className="field" name="name" autoFocus defaultValue={name} maxLength={60} placeholder={t("visit.name.placeholder")} aria-label={t("visit.name.label")} />
          <Button size="sm" variant="primary" type="submit">{t("common.save")}</Button>
        </form>
      ) : (
        <Button size="sm" variant="quiet" className="visit-name-btn" onClick={() => setEditing(true)} title={t("visit.name.label")}>
          {name ? t("visit.as", { name }) : t("visit.name.set")}
        </Button>
      )}
    </div>
  );
}

/** What is left of the owner's limit, quietly: the money one, the weekly one, or nothing when neither is set. */
function BudgetLeft({ limits }: { limits?: VisitLimits }) {
  const usd = limits?.budget?.usd;
  const pct = limits?.budget?.weekly_pct;
  if (!usd && !pct) return null;
  const parts = [
    usd ? t("visit.left.usd", { n: `$${Math.max(0, usd.limit - usd.spent).toFixed(2)}`, limit: `$${usd.limit.toFixed(2)}` }) : "",
    pct ? t("visit.left.pct", { n: `${Math.max(0, pct.limit - pct.spent).toFixed(1)}%`, limit: `${pct.limit}%` }) : "",
  ].filter(Boolean);
  const low = (usd && usd.spent >= usd.limit * 0.8) || (pct && pct.spent >= pct.limit * 0.8);
  return <span className={`visit-left ${low ? "low" : ""}`} title={t("visit.left.title")} data-budget-left>{parts.join(" · ")}</span>;
}

/** The password screen: nothing of the conversation is known before it opens, not even its title, so it
 *  names nothing but the fact that the link has a password. A wrong one says so; too many tries make the
 *  door wait, and the button counts the wait down instead of letting the visitor press into a refusal. */
function UnlockScreen({ onOpen }: { onOpen: () => void }) {
  const [value, setValue] = useState("");
  const [shown, setShown] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [wait, setWait] = useState(0);

  useEffect(() => {
    if (wait <= 0) return;
    const timer = window.setTimeout(() => setWait((n) => n - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [wait]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!value || wait > 0) return;
    setBusy(true);
    setProblem("");
    try {
      await api.post("/api/share/unlock", { password: value });
      onOpen();
    } catch (err) {
      const code = err instanceof ApiError ? err.data.code : "";
      if (code === "wrong") setProblem(t("visit.lock.wrong"));
      else if (code === "slow" && err instanceof ApiError) {
        setWait(Math.max(1, Math.ceil(Number(err.data.retry_after) || 30)));
        setProblem(t("visit.lock.slow"));
      } else if (err instanceof ApiError && (err.status === 410 || err.status === 404)) setProblem(t("visit.ended.body"));
      else setProblem(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="visit visit-locked">
      <form className="visit-lock" onSubmit={(e) => void submit(e)} data-unlock>
        <span className="visit-lock-mark"><Icon name="lock" size={22} /></span>
        <h1>{t("visit.lock.title")}</h1>
        <p className="sub">{t("visit.lock.body")}</p>
        <label className="field-label" htmlFor="visit-password">{t("visit.lock.label")}</label>
        <div className="visit-lock-field">
          <input id="visit-password" className="field" type={shown ? "text" : "password"} autoComplete="current-password" autoFocus value={value}
            onChange={(e) => setValue(e.target.value)} aria-invalid={problem ? true : undefined} aria-describedby={problem ? "visit-lock-problem" : undefined} />
          <Button type="button" icon={shown ? "eyeoff" : "eye"} aria-label={t(shown ? "live.pw.hide" : "live.pw.show")} title={t(shown ? "live.pw.hide" : "live.pw.show")} onClick={() => setShown((s) => !s)} />
        </div>
        {problem && <div id="visit-lock-problem" className="field-error" role="alert">{problem}</div>}
        <Button type="submit" variant="primary" className="visit-lock-open" busy={busy} disabled={!value || wait > 0 || busy}>
          {wait > 0 ? t("visit.lock.wait", { n: wait }) : t("visit.lock.open")}
        </Button>
      </form>
    </div>
  );
}
