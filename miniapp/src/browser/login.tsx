// The private sign-in form: the agent asked to be signed in (BrowserLogin), and the operator types the
// username, password and any one-time code here. The values go on their own request straight to the
// agent's browser, which types them into the page; the model never sees them, and the app keeps none of
// them once sent (the fields are emptied whatever the answer). A site with a saved sign-in is offered it
// first: once, or for the rest of the session.
//
// The card is drawn wherever the request is seen: under the conversation of the session that asked, on
// the Browser tab, and in the Inbox entry. Telegram never draws it; its entry links here.
import { useEffect, useState, type FormEvent } from "react";
import type { BrowserGroup, BrowserLoginAsk, Notification } from "../api";
import { t } from "../i18n";
import { Icon } from "../icons";
import { errorText } from "../ui";
import { declineLogin, submitLogin, useBrowserGroup } from "./data";
import "./login.css";

type Toast = (text: string) => void;

/** The request a group carries, or null: only an open group asks. */
export function loginAsk(group: Pick<BrowserGroup, "needs_you" | "status"> | null | undefined): BrowserLoginAsk | null {
  const ask = group?.needs_you?.login;
  return group?.status === "running" && ask && ask.id && ask.site ? ask : null;
}

/** What the operator is told after sending: where the page ended and whether the sign-in took. */
export function loginOutcome(result: { site: string; url: string; signed_in: string; waiting: string }): string {
  if (result.waiting === "code") return t("browser.login.done.code", { site: result.site });
  const key = result.signed_in === "yes" ? "browser.login.done.yes" : result.signed_in === "no" ? "browser.login.done.no" : "browser.login.done.unknown";
  return t(key, { site: result.site, url: result.url });
}

export function BrowserLoginCard({ group, toast, compact = false }: { group: BrowserGroup; toast: Toast; compact?: boolean }) {
  const ask = loginAsk(group);
  if (!ask) return null;
  // Keyed on the request: a new one (the code after the password) starts from empty fields.
  return <LoginForm key={ask.id} group={group.id} ask={ask} toast={toast} compact={compact} />;
}

function LoginForm({ group, ask, toast, compact }: { group: string; ask: BrowserLoginAsk; toast: Toast; compact: boolean }) {
  const [typing, setTyping] = useState(!ask.saved);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [save, setSave] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  // Nothing typed outlives the card: leaving the page or the request ending empties it.
  useEffect(() => () => { setPassword(""); setOtp(""); }, []);
  const ready = !busy && (typing ? !!(username || password || otp) : true);

  async function send(useSaved?: "once" | "always") {
    if (busy) return;
    setBusy(true);
    setProblem("");
    try {
      const body = useSaved
        ? { login_id: ask.id, use_saved: useSaved, ...(otp ? { otp } : {}) }
        : { login_id: ask.id, ...(username ? { username } : {}), ...(password ? { password } : {}), ...(otp ? { otp } : {}), save: save && !!password };
      const result = await submitLogin(group, body);
      toast(loginOutcome(result));
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setPassword("");
      setOtp("");
      setBusy(false);
    }
  }

  async function decline() {
    setBusy(true);
    try {
      await declineLogin(group, ask.id);
      toast(t("browser.login.declined", { site: ask.site }));
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (ready) void send();
  }

  const id = (name: string) => `login-${ask.id}-${name}`;
  return (
    <form className={`login-card ${compact ? "compact" : ""}`} data-login={ask.id} autoComplete="off" onSubmit={onSubmit} aria-label={t("browser.login.title", { site: ask.site })}>
      <div className="login-head">
        <Icon name="lock" size={16} />
        <span className="login-title">{t("browser.login.title", { site: ask.site })}</span>
      </div>
      {ask.reason && <div className="login-reason"><span className="login-label">{t("browser.login.reason")}</span> {ask.reason}</div>}
      <div className="sub login-private">{t("browser.login.private")}</div>
      {!typing ? (
        <>
          <div className="login-saved">{t("browser.login.saved", { site: ask.site })}</div>
          <label className="login-label" htmlFor={id("otp")}>{t("browser.login.otp")}</label>
          <input id={id("otp")} className="field" value={otp} onChange={(e) => setOtp(e.target.value)} inputMode="numeric" autoComplete="off"
            autoCapitalize="none" autoCorrect="off" spellCheck={false} data-1p-ignore="" data-lpignore="true" placeholder={t("browser.login.otp.optional")} />
          <div className="login-actions">
            <button type="button" className="btn primary" disabled={busy} data-use="once" onClick={() => void send("once")}>{t("browser.login.saved.once")}</button>
            <button type="button" className="btn" disabled={busy} data-use="always" onClick={() => void send("always")}>{t("browser.login.saved.always")}</button>
            <button type="button" className="btn ghost" disabled={busy} onClick={() => setTyping(true)}>{t("browser.login.saved.other")}</button>
          </div>
        </>
      ) : (
        <>
          <label className="login-label" htmlFor={id("user")}>{t("browser.login.username")}</label>
          <input id={id("user")} className="field" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" autoCapitalize="none"
            autoCorrect="off" spellCheck={false} data-1p-ignore="" data-lpignore="true" />
          <label className="login-label" htmlFor={id("password")}>{t("browser.login.password")}</label>
          <input id={id("password")} className="field" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="off"
            data-1p-ignore="" data-lpignore="true" />
          <label className="login-label" htmlFor={id("otp")}>{t("browser.login.otp")}</label>
          <input id={id("otp")} className="field" value={otp} onChange={(e) => setOtp(e.target.value)} inputMode="numeric" autoComplete="off"
            autoCapitalize="none" autoCorrect="off" spellCheck={false} data-1p-ignore="" data-lpignore="true" placeholder={t("browser.login.otp.optional")} />
          <label className="login-save">
            <input type="checkbox" checked={save} onChange={(e) => setSave(e.target.checked)} /> {t("browser.login.save", { site: ask.site })}
          </label>
          <div className="login-actions">
            <button type="submit" className="btn primary" disabled={!ready}>{t("browser.login.submit")}</button>
            {ask.saved && <button type="button" className="btn ghost" disabled={busy} onClick={() => setTyping(false)}>{t("browser.login.saved.back")}</button>}
            <span className="grow" />
            <button type="button" className="btn ghost" disabled={busy} data-decline="" onClick={() => void decline()}>{t("browser.login.decline")}</button>
          </div>
        </>
      )}
      {!typing && <div className="login-actions end"><button type="button" className="btn ghost" disabled={busy} data-decline="" onClick={() => void decline()}>{t("browser.login.decline")}</button></div>}
      {busy && <div className="sub login-busy" role="status">{t("browser.login.busy")}</div>}
      {problem && <div className="sub login-problem" role="alert">{problem}</div>}
    </form>
  );
}

/** The group a "needs you" entry names (`browser:<group>`), when it is a sign-in. */
export function loginGroupOf(entry: Pick<Notification, "kind" | "request_ref" | "needs_you">): string | null {
  if (entry.kind !== "browser_login" || !entry.needs_you || !entry.request_ref?.startsWith("browser:")) return null;
  return entry.request_ref.slice("browser:".length) || null;
}

/** The form inside an Inbox entry: the group read on its own, so the entry is the place to answer. */
export function NoticeLoginCard({ entry, toast }: { entry: Notification; toast: Toast }) {
  const id = loginGroupOf(entry);
  const group = useBrowserGroup(id);
  if (!id || !group) return null;
  return <div className="notice-login" onClick={(e) => e.stopPropagation()}><BrowserLoginCard group={group} toast={toast} compact /></div>;
}
