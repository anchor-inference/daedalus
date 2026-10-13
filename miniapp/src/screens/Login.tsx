import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { twoFactor } from "../twofactor";
import { ApiError } from "../api";
import { Icon } from "../icons";
import { errorText } from "../ui";
import { t, useLang } from "../i18n";
import { LangPicker } from "../ui/index";

declare global {
  interface Window {
    onTelegramAuth?: (user: Record<string, unknown>) => void;
  }
}

export type AuthConfig = {
  /** The bot whose Login Widget vouches for the operator; null on an installation without Telegram. */
  telegram: { bot_username: string } | null;
  /** Whether a pairing link the server printed is still open. */
  pairing: boolean;
  /** This browser is past the first way in and owes the two-factor code. */
  second_factor: boolean;
};

/** When the window last asked its launcher for a link, so a link that does not sign in cannot loop. */
const DESKTOP_SIGNIN_KEY = "daedalus.login.desktop-asked";
const DESKTOP_SIGNIN_PAUSE_MS = 60_000;

function askedRecently(): boolean {
  try {
    return Date.now() - Number(sessionStorage.getItem(DESKTOP_SIGNIN_KEY) || 0) < DESKTOP_SIGNIN_PAUSE_MS;
  } catch {
    return false;
  }
}

function noteAsked() {
  try {
    sessionStorage.setItem(DESKTOP_SIGNIN_KEY, String(Date.now()));
  } catch {
    /* nothing to remember it in: the button still works */
  }
}

/** The site outside Telegram: Telegram's Login Widget or the link the server printed, then the
 * two-factor code when that is on.
 *
 * Inside the desktop application the launcher beside the window mints that link itself, so the
 * window asks it for one instead of asking the operator for a code it shows nowhere. */
export function LoginScreen({ onDone }: { onDone: () => void }) {
  useLang();
  const slot = useRef<HTMLDivElement>(null);
  const [conf, setConf] = useState<AuthConfig | null>(null);
  const [error, setError] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get("pairing") === "spent" ? t("login.pairing.spent") : null,
  );
  const [busy, setBusy] = useState(false);
  const desktop = typeof window.daedalus?.signIn === "function";

  function viaLauncher() {
    noteAsked();
    setBusy(true);
    setError(null);
    window.daedalus?.signIn?.();
  }

  useEffect(() => {
    if (!desktop) return;
    const off = window.daedalus?.onSignInFailed?.((message) => {
      setBusy(false);
      setError(message ? `${t("login.desktop.failed")} ${message}` : t("login.desktop.failed"));
    });
    // Once a minute at most on its own: a link that comes back here without signing in is shown
    // as an error and a button, not followed round again.
    if (!askedRecently()) viaLauncher();
    return off;
  }, [desktop]);

  const [code, setCode] = useState("");
  const [owesCode, setOwesCode] = useState(false);
  function loadConfig() {
    api
      .get<AuthConfig>("/api/auth/config")
      .then((c) => (setConf(c), setOwesCode(c.second_factor)))
      .catch((e) => setError(errorText(e)));
  }
  useEffect(loadConfig, []);

  const bot = conf?.telegram?.bot_username ?? null;
  useEffect(() => {
    if (!bot || !slot.current) return;
    window.onTelegramAuth = async (user) => {
      setBusy(true);
      setError(null);
      try {
        const answer = await api.post<{ second_factor?: boolean }>("/api/auth/telegram", user);
        if (answer.second_factor) {
          setOwesCode(true);
          setBusy(false);
          return;
        }
        onDone();
      } catch (e) {
        setError(errorText(e));
        setBusy(false);
      }
    };
    // The widget script renders its button as an iframe right where the script tag sits.
    const script = document.createElement("script");
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.async = true;
    script.setAttribute("data-telegram-login", bot);
    script.setAttribute("data-size", "large");
    script.setAttribute("data-radius", "12");
    script.setAttribute("data-userpic", "false");
    script.setAttribute("data-onauth", "onTelegramAuth(user)");
    slot.current.replaceChildren(script);
    return () => {
      window.onTelegramAuth = undefined;
    };
  }, [bot, onDone, owesCode]);

  async function withCode(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await twoFactor.verify(code.trim());
      onDone();
    } catch (err) {
      setError(errorText(err));
      setBusy(false);
      setCode("");
      // A sign-in that used up its guesses, or waited past its five minutes, starts again from the first step.
      if (err instanceof ApiError && err.status === 401) setOwesCode(false);
    }
  }

  async function startOver() {
    setCode("");
    setError(null);
    await api.post("/api/auth/logout").catch(() => undefined);
    setOwesCode(false);
  }
  const [pairing, setPairing] = useState("");
  function withPairing(e: FormEvent) {
    e.preventDefault();
    // A whole link or just its code: both end up at the same address, which signs this browser in and comes back here.
    const raw = pairing.trim();
    const code = (raw.includes("code=") ? new URL(raw, window.location.origin).searchParams.get("code") : raw) ?? "";
    if (!code) return;
    window.location.assign(`/api/auth/pair?code=${encodeURIComponent(code)}`);
  }
  const methods = bot ? 1 : 0;
  return (
    <div className="gate">
      <div className="login" role="main" aria-labelledby="login-title">
        <LangPicker />
        <img className="login-logo" src="/app/icons/icon-192.png" alt="" width={72} height={72} />
        <h1 id="login-title">{t("login.title")}</h1>
        <p className="sub">{t("login.sub")}</p>
        {conf === null && !error && <div className="sub">{t("common.loading")}</div>}
        {desktop && (
          <div className="login-methods">
            <button className="btn primary big" disabled={busy} onClick={viaLauncher}>
              <Icon name="key" size={16} /> {t("login.desktop")}
            </button>
            <p className="sub small">{t("login.desktop.hint")}</p>
          </div>
        )}
        {conf !== null && !desktop && owesCode && (
          <form className="login-pair login-code" onSubmit={(e) => void withCode(e)}>
            <div className="settings-row-title">{t("login.code.title")}</div>
            <p className="sub small">{t("login.code.sub")}</p>
            <label className="field" htmlFor="login-code">{t("login.code.label")}</label>
            <div className="share-field">
              <input id="login-code" className="field mono" value={code} onChange={(e) => setCode(e.target.value)} placeholder={t("login.code.placeholder")}
                autoComplete="one-time-code" inputMode="text" maxLength={20} spellCheck={false} autoFocus />
              <button className="btn primary" type="submit" disabled={!code.trim() || busy}>{t("login.code.submit")}</button>
            </div>
            <button className="linkbtn" type="button" onClick={() => void startOver()}>{t("login.code.restart")}</button>
          </form>
        )}
        {conf !== null && !desktop && !owesCode && (
          <div className="login-methods">
            {bot && <div ref={slot} className="login-widget" />}
            {methods > 0 && <div className="login-or">{t("login.or")}</div>}
            <form className="login-pair" onSubmit={withPairing}>
              <label className="field" htmlFor="pairing">{t("login.pairing.label")}</label>
              <div className="share-field">
                <input id="pairing" className="field mono" value={pairing} onChange={(e) => setPairing(e.target.value)} placeholder={t("login.pairing.placeholder")} autoComplete="off" spellCheck={false} />
                <button className="btn" type="submit" disabled={!pairing.trim() || busy}>{t("common.continue")}</button>
              </div>
              <p className="sub small">{t("login.pairing.hint")}</p>
            </form>
          </div>
        )}
        {busy && <div className="sub">{t("login.busy")}</div>}
        {error && <div className="login-error">{error}</div>}
        {!desktop && <p className="sub small login-foot">{t("login.install")}</p>}
      </div>
    </div>
  );
}
