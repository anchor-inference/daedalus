import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "../api";
import * as passkeys from "../passkeys";
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
  /** How many passkeys are enrolled: one is enough to sign in with nothing else. */
  passkeys: number;
  /** Whether a pairing link the server printed is still open. */
  pairing: boolean;
};

/** The site outside Telegram: a passkey, Telegram's Login Widget, or the link the server printed. */
export function LoginScreen({ onDone }: { onDone: () => void }) {
  useLang();
  const slot = useRef<HTMLDivElement>(null);
  const [conf, setConf] = useState<AuthConfig | null>(null);
  const [error, setError] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get("pairing") === "spent" ? t("login.pairing.spent") : null,
  );
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .get<AuthConfig>("/api/auth/config")
      .then(setConf)
      .catch((e) => setError(errorText(e)));
  }, []);

  const bot = conf?.telegram?.bot_username ?? null;
  useEffect(() => {
    if (!bot || !slot.current) return;
    window.onTelegramAuth = async (user) => {
      setBusy(true);
      setError(null);
      try {
        await api.post("/api/auth/telegram", user);
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
  }, [bot, onDone]);

  async function withPasskey() {
    setBusy(true);
    setError(null);
    try {
      await passkeys.signIn();
      onDone();
    } catch (e) {
      setError(errorText(e));
      setBusy(false);
    }
  }

  const canPasskey = passkeys.supported();
  const hasPasskeys = (conf?.passkeys ?? 0) > 0 && canPasskey;
  const [pairing, setPairing] = useState("");
  function withPairing(e: FormEvent) {
    e.preventDefault();
    // A whole link or just its code: both end up at the same address, which signs this browser in and comes back here.
    const raw = pairing.trim();
    const code = (raw.includes("code=") ? new URL(raw, window.location.origin).searchParams.get("code") : raw) ?? "";
    if (!code) return;
    window.location.assign(`/api/auth/pair?code=${encodeURIComponent(code)}`);
  }
  const methods = (hasPasskeys ? 1 : 0) + (bot ? 1 : 0);
  return (
    <div className="gate">
      <div className="login" role="main" aria-labelledby="login-title">
        <LangPicker />
        <img className="login-logo" src="/app/icons/icon-192.png" alt="" width={72} height={72} />
        <h1 id="login-title">{t("login.title")}</h1>
        <p className="sub">{t("login.sub")}</p>
        {conf === null && !error && <div className="sub">{t("common.loading")}</div>}
        {conf !== null && (
          <div className="login-methods">
            {hasPasskeys && (
              <button className="btn primary big" disabled={busy} onClick={() => void withPasskey()}>
                <Icon name="key" size={16} /> {t("login.passkey")}
              </button>
            )}
            {bot && (
              <>
                {hasPasskeys && <div className="login-or">{t("login.or")}</div>}
                <div ref={slot} className="login-widget" />
              </>
            )}
            {methods > 0 && <div className="login-or">{t("login.or")}</div>}
            <form className="login-pair" onSubmit={withPairing}>
              <label className="field" htmlFor="pairing">{t("login.pairing.label")}</label>
              <div className="share-field">
                <input id="pairing" className="field mono" value={pairing} onChange={(e) => setPairing(e.target.value)} placeholder={t("login.pairing.placeholder")} autoComplete="off" spellCheck={false} />
                <button className="btn" type="submit" disabled={!pairing.trim() || busy}>{t("common.continue")}</button>
              </div>
              <p className="sub small">{t("login.pairing.hint")}</p>
            </form>
            {canPasskey && !hasPasskeys && (
              <p className="sub small">{t("login.nopasskey")}</p>
            )}
          </div>
        )}
        {busy && <div className="sub">{t("login.busy")}</div>}
        {error && <div className="login-error">{error}</div>}
        <p className="sub small login-foot">{t("login.install")}</p>
      </div>
    </div>
  );
}
