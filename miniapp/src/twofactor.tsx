// Two-factor sign-in in Settings → Security: enrolment by QR code or typed key, confirmation with a
// first code, the recovery codes shown once, and turning it off or replacing the codes with a code.
// Its own card and its own file, so the other cards of the section stay independent of it.
import { FormEvent, useEffect, useState } from "react";
import { api } from "./api";
import { errorText } from "./ui";
import { copyText } from "./ui/components";
import { shortDateTime } from "./format";
import { t } from "./i18n";
import { Row } from "./settingsrow";
import { useMedia } from "./shell";

export type TwoFactorStatus = { enabled: boolean; enrolling: boolean; recovery_left: number; enabled_at: string | null };
export type TwoFactorSetup = { secret: string; uri: string; qr: string[]; label: string };
type WithCodes = TwoFactorStatus & { recovery_codes: string[] };

export const twoFactor = {
  status: () => api.get<TwoFactorStatus>("/api/auth/totp"),
  setup: () => api.post<TwoFactorSetup>("/api/auth/totp/setup"),
  enable: (code: string) => api.post<WithCodes>("/api/auth/totp/enable", { code }),
  disable: (code: string) => api.post<TwoFactorStatus>("/api/auth/totp/disable", { code }),
  regenerate: (code: string) => api.post<WithCodes>("/api/auth/totp/recovery", { code }),
  /** The login page's second step: the pending sign-in plus a code make a session. */
  verify: (code: string) => api.post<{ ok: boolean; with: "totp" | "recovery"; recovery_left: number }>("/api/auth/totp/verify", { code }),
};

/** Whether the app runs in the desktop application's window, which signs in through its launcher. */
export function inDesktopWindow(): boolean {
  return !!window.daedalus?.window;
}

/** The QR code as squares on a light ground, whatever the theme: a scanner reads dark on light. */
export function QrCode({ rows, label }: { rows: string[]; label: string }) {
  const size = rows.length;
  const quiet = 4; // the margin the standard asks for around the symbol
  const cells: string[] = [];
  rows.forEach((row, y) => {
    for (let x = 0; x < row.length; x++) if (row[x] === "1") cells.push(`M${x + quiet} ${y + quiet}h1v1h-1z`);
  });
  const side = size + quiet * 2;
  return (
    <svg className="totp-qr" viewBox={`0 0 ${side} ${side}`} role="img" aria-label={label} shapeRendering="crispEdges">
      <rect width={side} height={side} fill="#fff" />
      <path d={cells.join("")} fill="#000" />
    </svg>
  );
}

/** The secret in groups of four, as authenticator apps print it: easier to type without losing the place. */
function grouped(secret: string): string {
  return secret.replace(/(.{4})(?=.)/g, "$1 ");
}

function RecoveryCodes({ codes, toast, onDone }: { codes: string[]; toast: (text: string) => void; onDone: () => void }) {
  return (
    <div className="totp-codes-box">
      <div className="settings-row-title">{t("settings.totp.codes.title")}</div>
      <div className="sub">{t("settings.totp.codes.sub")}</div>
      <ol className="totp-codes mono">
        {codes.map((code) => <li key={code}>{code}</li>)}
      </ol>
      <div className="totp-actions">
        <button className="btn small" onClick={() => void copyText(codes.join("\n")).then((ok) => ok && toast(t("settings.totp.codes.copied")))}>{t("settings.totp.codes.copy")}</button>
        <button className="btn primary small" onClick={onDone}>{t("settings.totp.codes.done")}</button>
      </div>
    </div>
  );
}

function CodeField({ id, value, onChange, label, digitsOnly }: { id: string; value: string; onChange: (v: string) => void; label: string; digitsOnly?: boolean }) {
  return (
    <input
      id={id}
      className="field mono totp-code"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
      placeholder={digitsOnly ? "123456" : label}
      inputMode={digitsOnly ? "numeric" : "text"}
      autoComplete="one-time-code"
      maxLength={digitsOnly ? 6 : 20}
      spellCheck={false}
    />
  );
}

type Ask = "disable" | "regenerate";

export function TwoFactorCard({ toast }: { toast: (text: string) => void }) {
  const [status, setStatus] = useState<TwoFactorStatus | null>(null);
  const [setup, setSetup] = useState<TwoFactorSetup | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [ask, setAsk] = useState<Ask | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const desktop = inDesktopWindow();
  // Two buttons beside a two-line status leave the words a third of a phone's width: under it, they fit.
  const narrow = useMedia("(max-width: 640px)");

  useEffect(() => {
    twoFactor.status().then(setStatus).catch((e) => toast(errorText(e)));
  }, [toast]);

  async function run<T>(work: () => Promise<T>, after: (value: T) => void) {
    setBusy(true);
    setError(null);
    try {
      after(await work());
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const begin = () => run(twoFactor.setup, (s) => (setSetup(s), setCode("")));
  const cancel = () => (setSetup(null), setAsk(null), setCode(""), setError(null));
  function enable(e: FormEvent) {
    e.preventDefault();
    void run(() => twoFactor.enable(code.trim()), (r) => {
      setSetup(null);
      setCode("");
      setStatus(r);
      setCodes(r.recovery_codes);
      toast(t("settings.totp.enabled"));
    });
  }
  function confirm(e: FormEvent) {
    e.preventDefault();
    if (ask === "disable") {
      void run(() => twoFactor.disable(code.trim()), (r) => (setStatus(r), cancel(), toast(t("settings.totp.disabled"))));
    } else {
      void run(() => twoFactor.regenerate(code.trim()), (r) => (setStatus(r), cancel(), setCodes(r.recovery_codes)));
    }
  }

  return (
    <div className="card totp-card" data-totp={status ? (status.enabled ? "on" : "off") : "loading"}>
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.totp.title")}</div>
      {desktop ? (
        <div className="sub">{status?.enabled ? t("settings.totp.on", { t: shortDateTime(status.enabled_at) }) : t("settings.totp.off")} · {t("settings.totp.desktop")}</div>
      ) : (
        <>
          <div className="sub">{t("settings.totp.sub")}</div>
          {status === null && <div className="empty">{t("common.loading")}</div>}
          {codes && <RecoveryCodes codes={codes} toast={toast} onDone={() => setCodes(null)} />}
          {status && !status.enabled && !setup && (
            <Row title={t("settings.totp.off")}>
              <button className="btn primary small" disabled={busy} onClick={() => void begin()}>{t("settings.totp.setup")}</button>
            </Row>
          )}
          {setup && (
            <form className="totp-setup" onSubmit={enable}>
              <div className="sub">{t("settings.totp.scan")}</div>
              <QrCode rows={setup.qr} label={t("settings.totp.qr")} />
              <div className="sub">{t("settings.totp.manual")}</div>
              <code className="totp-secret mono" data-secret={setup.secret}>{grouped(setup.secret)}</code>
              <label className="sub" htmlFor="totp-enable-code">{t("settings.totp.confirm")}</label>
              <div className="totp-actions">
                <CodeField id="totp-enable-code" value={code} onChange={(v) => setCode(v.replace(/\D/g, ""))} label={t("settings.totp.code")} digitsOnly />
                <button className="btn primary small" type="submit" disabled={busy || code.length !== 6}>{t("settings.totp.enable")}</button>
                <button className="btn small" type="button" onClick={cancel}>{t("settings.totp.cancel")}</button>
              </div>
            </form>
          )}
          {status?.enabled && !ask && (
            <Row title={t("settings.totp.on", { t: shortDateTime(status.enabled_at) })} desc={t("settings.totp.recoveryleft", { n: status.recovery_left })} stack={narrow}>
              <button className="btn small" onClick={() => (setAsk("regenerate"), setCode(""), setError(null))}>{t("settings.totp.regenerate")}</button>
              <button className="btn small" onClick={() => (setAsk("disable"), setCode(""), setError(null))}>{t("settings.totp.disable")}</button>
            </Row>
          )}
          {status?.enabled && ask && (
            <form className="totp-setup" onSubmit={confirm}>
              <label className="sub" htmlFor="totp-ask-code">{t(ask === "disable" ? "settings.totp.ask.disable" : "settings.totp.ask.regenerate")}</label>
              <div className="totp-actions">
                <CodeField id="totp-ask-code" value={code} onChange={setCode} label={t("settings.totp.ask.code")} />
                <button className="btn primary small" type="submit" disabled={busy || !code.trim()}>{t("settings.totp.ask.confirm")}</button>
                <button className="btn small" type="button" onClick={cancel}>{t("settings.totp.cancel")}</button>
              </div>
            </form>
          )}
          {error && <div className="login-error totp-error" role="alert">{error}</div>}
        </>
      )}
    </div>
  );
}
