// Settings → Browser → "Your Chrome": the operator's own Chrome as a browser the agent may be given.
//
// The connection is checked from both sides, so the card says what Daedalus sees — whether the
// connector is registered (on this computer), which Chromes are connected, why none is — and "Check
// connection" makes that Chrome's extension flash at the same moment the round trip comes back here.
// On a server there is no connector: a Chrome is paired with a one-time code instead, and the paired
// ones are listed with a way to revoke each. Any number stay connected at once.
//
// What the agent may do there is the host's `[browser.user]`; the per-conversation switch is in the
// browser panel's header, never here.

import { useEffect, useState } from "react";
import type { BrowserChrome, BrowserUserChoice, BrowserUserDevice, BrowserUserSettings, BrowserUserStatus, ChromeLauncherReport } from "../api";
import { api } from "../api";
import { t } from "../i18n";
import { invalidate, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { timeAgo, useCopied } from "../ui/components";
import { Button, confirmDialog, Dot, Segmented, Switch } from "../ui/primitives";
import { Row, SetBlock, SetFoot, SetGroup } from "../settingsrow";
import { useEvent } from "../events";

export const USER_STATUS = "/api/browser/user";

export const DEFAULT_USER: BrowserUserSettings = { enabled: true, granted_sites: [], monitor: true, watch: true, bodies: false, read_only: true, close_tabs: false };

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

export function YourChrome({ user, setUser, toast }: { user: BrowserUserSettings; setUser: (patch: Partial<BrowserUserSettings>) => void; toast: (text: string) => void }) {
  const query = useQuery<BrowserUserStatus>(USER_STATUS, { pollMs: 10000 });
  const status = query.data;
  useEvent(["browser.user"], () => invalidate(USER_STATUS), []);
  const state = chromeState(status);
  if (!status || state === "none") return null;
  return (
    <SetGroup title={t("uc.title")} help={t("uc.sub")} className="bs-chrome" data-setting="browser.user" data-state={state}>
      {status.native ? <Connector status={status} toast={toast} /> : null}
      <Connection status={status} state={state} toast={toast} />
      {!status.native && <Pairing status={status} toast={toast} />}
      <SetBlock className="bs-chrome-consent"><span className="set-hint">{status.consent}</span></SetBlock>
      <Defaults user={user} setUser={setUser} />
    </SetGroup>
  );
}

function Connector({ status, toast }: { status: BrowserUserStatus; toast: (text: string) => void }) {
  const [copied, copy] = useCopied();
  const report = status.launcher;
  const connector = connectorState(report);
  const [busy, setBusy] = useState(false);
  async function register() {
    setBusy(true);
    try {
      const done = await api.post<{ launcher: ChromeLauncherReport; error: string }>(`${USER_STATUS}/register`, {});
      if (done.error) toast(done.error);
      else if (done.launcher.connector?.restart_chrome) toast(t("uc.connector.restart"));
      invalidate(USER_STATUS);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const folder = report?.extension?.path ?? "";
  return (
    <>
      <Row
        className="bs-chrome-step"
        data-step="connector"
        data-connector={connector}
        title={<span className="bs-env-head"><Dot status={connector === "registered" ? "ok" : connector === "restart" ? "warn" : "error"} />{t("uc.connector.title")}</span>}
        desc={report ? t(`uc.connector.${connector}`) : status.launcher_error || t("uc.connector.unknown")}
      >
        {connector !== "registered" && report && <Button size="sm" variant="primary" busy={busy} onClick={() => void register()}>{t("uc.connector.register")}</Button>}
      </Row>
      <Row
        className="bs-chrome-step"
        data-step="extension"
        title={<span className="bs-env-head"><Dot status={report?.extension?.ready ? "ok" : "error"} />{t("uc.ext.title")}</span>}
        desc={report?.extension?.ready ? undefined : report?.extension?.error || t("uc.ext.missing")}
      />
      {report?.extension?.ready && (
        <SetBlock>
          <ol className="bs-chrome-howto set-hint">
            <li>
              {t("uc.ext.step1")} <code className="bs-code">chrome://extensions</code>{" "}
              <Button size="sm" icon={copied ? "check" : undefined} onClick={() => void copy("chrome://extensions").then((ok) => { if (!ok) toast(t("uc.copy.failed")); })}>{t("uc.copy")}</Button>
            </li>
            <li>{t("uc.ext.step2")}</li>
            <li>{t("uc.ext.step3")} <code className="bs-code mono">{folder}</code></li>
          </ol>
        </SetBlock>
      )}
    </>
  );
}

function Connection({ status, state, toast }: { status: BrowserUserStatus; state: ChromeState; toast: (text: string) => void }) {
  if (state === "down") {
    return <Row className="bs-chrome-conn" data-conn="down" title={<span className="bs-env-head"><Dot status="error" />{t("uc.down")}</span>} desc={status.detail || status.reason} />;
  }
  const connected = (status.relay?.chromes ?? []).filter((c) => c.connected || c.reconnecting);
  if (state !== "connected" || connected.length === 0) {
    return <Row className="bs-chrome-conn" data-conn="waiting" title={<span className="bs-env-head"><Dot status="error" />{t("uc.waiting")}</span>} desc={notConnectedReason(status)} />;
  }
  return <>{connected.map((c) => <ChromeRow key={c.chrome} chrome={c} toast={toast} />)}</>;
}

/** One connected Chrome: its name and versions, what agents have open there, and a check of its own,
 *  since a round trip through one says nothing of the other. */
function ChromeRow({ chrome, toast }: { chrome: BrowserChrome; toast: (text: string) => void }) {
  const [rtt, setRtt] = useState<number | null>(null);
  const [checking, setChecking] = useState(false);
  async function check() {
    setChecking(true);
    try {
      const done = await api.post<{ rtt_ms: number }>(`${USER_STATUS}/check`, { chrome: chrome.chrome });
      setRtt(done.rtt_ms);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setChecking(false);
    }
  }
  return (
    <div className="bs-chrome-conn" data-conn="connected" data-chrome={chrome.chrome}>
      <Row
        title={<span className="bs-env-head"><Dot status={chrome.connected ? "ok" : "waiting"} />{t(chrome.connected ? "uc.connected" : "uc.reconnecting", { name: chrome.profile_name || "Chrome" })}</span>}
        desc={rtt !== null ? <span className="bs-chrome-rtt" data-rtt={rtt}>{t("uc.check.done", { ms: rtt })}</span> : undefined}
      >
        <Button size="sm" busy={checking} disabled={!chrome.connected} onClick={() => void check()}>{t("uc.check")}</Button>
      </Row>
      <SetBlock>
        <div className="kv"><span>{t("uc.ext.version")}</span><b className="mono">{chrome.ext_version || "?"}</b></div>
        <div className="kv"><span>{t("uc.chrome.version")}</span><b className="mono">{chrome.chrome_version || "?"}</b></div>
        <div className="kv"><span>{t("uc.transport")}</span><b>{t(chrome.transport === "server" ? "uc.transport.server" : "uc.transport.native")}</b></div>
        <div className="kv"><span>{t("uc.since")}</span><b>{timeAgo(chrome.since ?? null)}</b></div>
        <div className="kv"><span>{t("uc.seen")}</span><b>{timeAgo(chrome.last_seen ?? null)}</b></div>
        <div className="kv"><span>{t("uc.agent.tabs")}</span><b>{chrome.tabs ? t("uc.agent.tabs.open", { count: chrome.tabs }) : t("uc.agent.tabs.none")}</b></div>
      </SetBlock>
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
    if (!(await confirmDialog({ title: t("uc.revoke.title", { name: d.name }), body: t("uc.revoke.body"), action: t("uc.revoke"), danger: true }))) return;
    try {
      await api.delete(`${USER_STATUS}/devices/${encodeURIComponent(d.id)}`);
      invalidate(USER_STATUS);
    } catch (e) {
      toast(errorText(e));
    }
  }
  const address = code?.server_url || status.server_url;
  return (
    <>
      <SetFoot><Button size="sm" variant="primary" onClick={() => void pair()}>{t("uc.pair")}</Button></SetFoot>
      {code && (
        <SetBlock className="bs-chrome-code" data-code={code.code}>
          <div className="kv"><span>{t("uc.pair.code")}</span><b className="mono bs-chrome-code-value">{code.code}</b></div>
          <div className="kv"><span>{t("uc.pair.server")}</span><b className="mono">{address || t("uc.pair.noaddress")}</b></div>
          <span className="set-hint">{t("uc.pair.hint")}</span>
        </SetBlock>
      )}
      <SetBlock><span className="field-label">{t("uc.devices")}</span></SetBlock>
      {status.devices.length === 0 ? (
        <SetBlock><span className="set-hint">{t("uc.devices.none")}</span></SetBlock>
      ) : status.devices.map((d) => (
        <Row key={d.id} className="bs-row" data-device={d.id} title={d.name} desc={[d.connected ? t("uc.devices.connected") : t("uc.devices.seen", { when: timeAgo(d.last_seen) }), d.chrome_version].filter(Boolean).join(" · ")}>
          <Button size="sm" variant="danger" onClick={() => void revoke(d)}>{t("uc.revoke")}</Button>
        </Row>
      ))}
    </>
  );
}

function Defaults({ user, setUser }: { user: BrowserUserSettings; setUser: (patch: Partial<BrowserUserSettings>) => void }) {
  const [text, setText] = useState(user.granted_sites.join("\n"));
  useEffect(() => setText(user.granted_sites.join("\n")), [user.granted_sites]);
  return (
    <>
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
      <Row title={t("uc.read_only")} desc={t("uc.read_only.sub")} data-setting="browser.user.read_only">
        <Switch checked={user.read_only} onChange={(read_only) => setUser({ read_only })} label={t("uc.read_only")} />
      </Row>
      <Row title={t("uc.close_tabs")} desc={t("uc.close_tabs.sub")} data-setting="browser.user.close_tabs">
        <Switch checked={user.close_tabs} onChange={(close_tabs) => setUser({ close_tabs })} label={t("uc.close_tabs")} />
      </Row>
      <SetBlock data-setting="browser.user.granted_sites">
        <label className="field-label" htmlFor="browser-user-sites">{t("uc.sites")}</label>
        <textarea id="browser-user-sites" className="field bs-list" rows={3} value={text} spellCheck={false} placeholder="example.com" onChange={(e) => setText(e.target.value)} onBlur={() => {
          const next = [...new Set(text.split(/[\n,]/).map((x) => x.trim().toLowerCase()).filter(Boolean))];
          if (next.join("\n") !== user.granted_sites.join("\n")) setUser({ granted_sites: next });
        }} />
        <span className="set-hint">{t("uc.sites.hint")}</span>
      </SetBlock>
    </>
  );
}

/** The panel header's switch: which browser this conversation's agent uses. Only the operator moves
 *  it into their Chrome, and the first move there in a conversation asks once, with what the agent can
 *  do there; the agent may move it back to its own browser itself, and the switch redraws on that too. */
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
