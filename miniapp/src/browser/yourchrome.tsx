// Settings → Environments → "Your Chrome": the operator's own Chrome as a browser the agent may be given.
//
// The connection is checked from both sides, so the card says what Daedalus sees — whether the
// connector is registered (on this computer), whether the extension is connected and as which
// Chrome, why it is not — and "Check connection" makes the extension flash at the same moment the
// round trip comes back here. On a server there is no connector: a Chrome is paired with a one-time
// code instead, and the paired ones are listed with a way to revoke each.
//
// What the agent may do there is the host's `[browser.user]`; the per-conversation switch is in the
// browser panel's header, never here.

import { useEffect, useState } from "react";
import type { BrowserUserChoice, BrowserUserDevice, BrowserUserSettings, BrowserUserStatus, ChromeLauncherReport } from "../api";
import { api } from "../api";
import { t } from "../i18n";
import { Icon } from "../icons";
import { invalidate, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { copyText, Segmented, Switch, timeAgo } from "../ui/components";
import { Row } from "../settingsrow";
import { useEvent } from "../events";
import { toast as statusToast } from "../ui/dialogs";

export const USER_STATUS = "/api/browser/user";

export const DEFAULT_USER: BrowserUserSettings = { enabled: true, granted_sites: [], monitor: true, watch: true, bodies: false };

/** Where the card stands: no daemon for it here, its daemon down, a Chrome connected, or waiting for one. */
export type ChromeState = "none" | "down" | "connected" | "waiting";

export function chromeState(status: BrowserUserStatus | undefined): ChromeState {
  if (!status || !status.configured) return "none";
  if (!status.daemon) return "down";
  return status.relay?.connected ? "connected" : "waiting";
}

/** The connector (Chrome's native-messaging host) as the launcher reports it, natively. */
export type ConnectorState = "registered" | "restart" | "stale" | "absent" | "unknown";

export function connectorState(report: ChromeLauncherReport | null | undefined): ConnectorState {
  const c = report?.connector;
  if (!c) return "unknown";
  if (c.registered && c.restart_chrome) return "restart";
  if (c.registered) return "registered";
  if (c.stale) return "stale";
  return "absent";
}

/** Why no Chrome is connected, in the words the daemon last had, or the plain first-time reason. */
export function notConnectedReason(status: BrowserUserStatus): string {
  return status.relay?.last_error || t("uc.waiting.never");
}

/** The toast for the extension popup's "Check connection", on whatever page is open: the popup says
 *  "Daedalus confirmed" at the same moment, so both sides confirm the same check. */
export function useChromeChecks(): void {
  useEvent(["browser.user"], (event) => {
    const payload = (event.payload ?? {}) as { kind?: string; profile_name?: string };
    if (payload.kind === "check") statusToast(t("uc.popup.checked", { name: payload.profile_name || "Chrome" }));
  }, []);
}

export function YourChrome({ user, setUser, toast }: { user: BrowserUserSettings; setUser: (patch: Partial<BrowserUserSettings>) => void; toast: (text: string) => void }) {
  const query = useQuery<BrowserUserStatus>(USER_STATUS, { pollMs: 10000 });
  const status = query.data;
  useEvent(["browser.user"], () => invalidate(USER_STATUS), []);
  const state = chromeState(status);
  if (!status || state === "none") return null;
  return (
    <div className="card bs-chrome" data-setting="browser.user" data-state={state}>
      <div className="section-title" style={{ marginTop: 0 }}>{t("uc.title")}</div>
      <div className="sub">{t("uc.sub")}</div>
      {status.native ? <Connector status={status} toast={toast} /> : null}
      <Connection status={status} state={state} toast={toast} />
      {!status.native && <Pairing status={status} toast={toast} />}
      <div className="bs-chrome-consent sub">{status.consent}</div>
      <Defaults user={user} setUser={setUser} />
    </div>
  );
}

function Connector({ status, toast }: { status: BrowserUserStatus; toast: (text: string) => void }) {
  const report = status.launcher;
  const connector = connectorState(report);
  const [busy, setBusy] = useState(false);
  async function register() {
    setBusy(true);
    try {
      const done = await api.post<{ launcher: ChromeLauncherReport; error: string }>(`${USER_STATUS}/register`, {});
      if (done.error) toast(done.error);
      else toast(t(done.launcher.connector?.restart_chrome ? "uc.connector.restart" : "uc.connector.done"));
      invalidate(USER_STATUS);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const folder = report?.extension?.path ?? "";
  return (
    <div className="bs-chrome-steps">
      <div className="bs-chrome-step" data-step="connector" data-connector={connector}>
        <span className={`dot ${connector === "registered" ? "ok" : connector === "restart" ? "warn" : "error"}`} aria-hidden="true" />
        <span className="bs-row-main">
          <b>{t("uc.connector.title")}</b>
          <span className="sub">{report ? t(`uc.connector.${connector}`) : status.launcher_error || t("uc.connector.unknown")}</span>
        </span>
        {connector !== "registered" && report && (
          <button type="button" className="btn small primary" disabled={busy} onClick={() => void register()}>{t("uc.connector.register")}</button>
        )}
      </div>
      <div className="bs-chrome-step" data-step="extension">
        <span className={`dot ${report?.extension?.ready ? "ok" : "error"}`} aria-hidden="true" />
        <span className="bs-row-main">
          <b>{t("uc.ext.title")}</b>
          {report?.extension?.ready ? (
            <>
              <ol className="bs-chrome-howto sub">
                <li>
                  {t("uc.ext.step1")} <code className="bs-code">chrome://extensions</code>{" "}
                  <button type="button" className="btn small" onClick={() => void copyText("chrome://extensions").then((ok) => toast(t(ok ? "uc.copied" : "uc.copy.failed")))}>{t("uc.copy")}</button>
                </li>
                <li>{t("uc.ext.step2")}</li>
                <li>{t("uc.ext.step3")} <code className="bs-code mono">{folder}</code></li>
              </ol>
            </>
          ) : (
            <span className="sub">{report?.extension?.error || t("uc.ext.missing")}</span>
          )}
        </span>
      </div>
    </div>
  );
}

function Connection({ status, state, toast }: { status: BrowserUserStatus; state: ChromeState; toast: (text: string) => void }) {
  const relay = status.relay;
  const [rtt, setRtt] = useState<number | null>(null);
  const [checking, setChecking] = useState(false);
  useEffect(() => setRtt(null), [state]);
  async function check() {
    setChecking(true);
    try {
      const done = await api.post<{ rtt_ms: number }>(`${USER_STATUS}/check`, {});
      setRtt(done.rtt_ms);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setChecking(false);
    }
  }
  if (state === "down") {
    return (
      <div className="bs-chrome-conn" data-conn="down">
        <span className="dot error" aria-hidden="true" />
        <span className="bs-row-main"><b>{t("uc.down")}</b><span className="sub">{status.detail || status.reason}</span></span>
      </div>
    );
  }
  if (state !== "connected" || !relay) {
    return (
      <div className="bs-chrome-conn" data-conn="waiting">
        <span className="dot error" aria-hidden="true" />
        <span className="bs-row-main"><b>{t("uc.waiting")}</b><span className="sub">{notConnectedReason(status)}</span></span>
      </div>
    );
  }
  return (
    <div className="bs-chrome-conn" data-conn="connected">
      <div className="bs-env-head">
        <span className="dot ok" aria-hidden="true" />
        <b>{t("uc.connected", { name: relay.profile_name || "Chrome" })}</b>
      </div>
      <div className="kv"><span>{t("uc.ext.version")}</span><b className="mono">{relay.ext_version || "?"}</b></div>
      <div className="kv"><span>{t("uc.chrome.version")}</span><b className="mono">{relay.chrome_version || "?"}</b></div>
      <div className="kv"><span>{t("uc.transport")}</span><b>{t(relay.transport === "server" ? "uc.transport.server" : "uc.transport.native")}</b></div>
      <div className="kv"><span>{t("uc.since")}</span><b>{timeAgo(relay.since ?? null)}</b></div>
      <div className="kv"><span>{t("uc.seen")}</span><b>{timeAgo(relay.last_seen ?? null)}</b></div>
      <div className="btnrow">
        <button type="button" className="btn small" disabled={checking} onClick={() => void check()}>{t("uc.check")}</button>
        {rtt !== null && <span className="sub bs-chrome-rtt" data-rtt={rtt}>{t("uc.check.done", { ms: rtt })}</span>}
      </div>
    </div>
  );
}

function Pairing({ status, toast }: { status: BrowserUserStatus; toast: (text: string) => void }) {
  const [code, setCode] = useState<{ code: string; expires_at: string; server_url: string } | null>(null);
  async function pair() {
    try {
      setCode(await api.post(`${USER_STATUS}/pairing`, {}));
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function revoke(d: BrowserUserDevice) {
    if (!(await confirmAsync(t("uc.revoke.title", { name: d.name }), { body: t("uc.revoke.body"), action: t("uc.revoke"), danger: true }))) return;
    try {
      await api.delete(`${USER_STATUS}/devices/${encodeURIComponent(d.id)}`);
      invalidate(USER_STATUS);
    } catch (e) {
      toast(errorText(e));
    }
  }
  const address = code?.server_url || status.server_url;
  return (
    <div className="bs-chrome-pair">
      <div className="btnrow">
        <button type="button" className="btn small primary" onClick={() => void pair()}>{t("uc.pair")}</button>
      </div>
      {code && (
        <div className="bs-chrome-code" data-code={code.code}>
          <div className="kv"><span>{t("uc.pair.code")}</span><b className="mono bs-chrome-code-value">{code.code}</b></div>
          <div className="kv"><span>{t("uc.pair.server")}</span><b className="mono">{address || t("uc.pair.noaddress")}</b></div>
          <div className="sub">{t("uc.pair.hint")}</div>
        </div>
      )}
      <div className="section-title">{t("uc.devices")}</div>
      {status.devices.length === 0 ? (
        <div className="sub">{t("uc.devices.none")}</div>
      ) : (
        <ul className="bs-rows">
          {status.devices.map((d) => (
            <li key={d.id} className="bs-row" data-device={d.id}>
              <Icon name="globe" size={16} />
              <span className="bs-row-main">
                <b className="truncate">{d.name}</b>
                <span className="sub truncate">{[d.connected ? t("uc.devices.connected") : t("uc.devices.seen", { when: timeAgo(d.last_seen) }), d.chrome_version].filter(Boolean).join(" · ")}</span>
              </span>
              <button type="button" className="btn small danger" onClick={() => void revoke(d)}>{t("uc.revoke")}</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Defaults({ user, setUser }: { user: BrowserUserSettings; setUser: (patch: Partial<BrowserUserSettings>) => void }) {
  const [text, setText] = useState(user.granted_sites.join("\n"));
  useEffect(() => setText(user.granted_sites.join("\n")), [user.granted_sites]);
  return (
    <div className="bs-chrome-defaults">
      <Row title={t("uc.enabled")} desc={t("uc.enabled.sub")} data-setting="browser.user.enabled">
        <Switch checked={user.enabled} onChange={(enabled) => setUser({ enabled })} label={t("uc.enabled")} />
      </Row>
      <Row title={t("uc.monitor")} data-setting="browser.user.monitor">
        <Switch checked={user.monitor} onChange={(monitor) => setUser({ monitor })} label={t("uc.monitor")} />
      </Row>
      <Row title={t("uc.watch")} data-setting="browser.user.watch">
        <Switch checked={user.watch} onChange={(watch) => setUser({ watch })} label={t("uc.watch")} />
      </Row>
      <Row title={t("uc.bodies")} desc={t("uc.bodies.sub")} data-setting="browser.user.bodies">
        <Switch checked={user.bodies} onChange={(bodies) => setUser({ bodies })} label={t("uc.bodies")} />
      </Row>
      <label className="field" htmlFor="browser-user-sites">{t("uc.sites")}</label>
      <textarea id="browser-user-sites" data-setting="browser.user.granted_sites" className="field bs-list" rows={3} value={text} spellCheck={false} placeholder="example.com" onChange={(e) => setText(e.target.value)} onBlur={() => {
        const next = [...new Set(text.split(/[\n,]/).map((x) => x.trim().toLowerCase()).filter(Boolean))];
        if (next.join("\n") !== user.granted_sites.join("\n")) setUser({ granted_sites: next });
      }} />
      <div className="sub">{t("uc.sites.hint")}</div>
    </div>
  );
}

/** The panel header's switch: which browser this conversation's agent uses. Only the operator moves
 *  it; the first move to their Chrome in a conversation asks once, with what the agent can do there. */
export function BrowserSwitch({ sessionId, user, consent, toast }: { sessionId: string; user: BrowserUserChoice; consent?: string; toast: (text: string) => void }) {
  const [busy, setBusy] = useState(false);
  async function choose(env: "agent" | "user") {
    if (env === user.env || busy) return;
    let agreed = false;
    if (env === "user" && !user.consented) {
      agreed = await confirmAsync(t("uc.switch.consent.title"), { body: consent || t("uc.switch.consent.body"), action: t("uc.switch.consent.action"), danger: false });
      if (!agreed) return;
    }
    setBusy(true);
    try {
      await api.put(`${USER_STATUS}/sessions/${encodeURIComponent(sessionId)}`, { env, consent: agreed });
      invalidate("/api/browsers");
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const chrome = user.name ? t("uc.switch.user.named", { name: user.name }) : t("uc.switch.user");
  return (
    <div className="bp-switch" data-env={user.env} data-connected={user.connected}>
      <Segmented value={user.env} label={t("uc.switch.label")} onChange={(env) => void choose(env)} options={[
        { id: "agent", label: t("uc.switch.agent") },
        { id: "user", label: chrome },
      ]} />
      {user.env === "user" && !user.connected && <span className="sub bp-switch-note">{t("uc.switch.offline")}</span>}
    </div>
  );
}
