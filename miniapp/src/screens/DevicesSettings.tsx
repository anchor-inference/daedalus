// Settings → Devices: the computers paired with daedalus-cli, how to pair another, and the code a
// terminal shows typed in by hand.
//
// A device is a token the server issued to one computer after the operator approved it; revoking it
// ends that token and closes the computer's open link at once, while the conversations it started
// stay, readable, and pause their next tool call until the computer is paired again and takes them
// back. Revoking asks first, in the row itself: the viewer the app runs in inside Telegram refuses
// `confirm()`. Requests still waiting for an answer are listed above the devices too, for an operator
// who closed the notice.

import { useEffect, useRef, useState } from "react";
import { api, type CliDevice, type PairRequest } from "../api";
import { useEvent } from "../events";
import { absDate, relTimeLong } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { CLI_DEVICES, PAIRS, normCode, openPairing, osLabel, showCode } from "../remote";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { copyText } from "../ui/components";

/** How daedalus-cli is installed, as its guide (`cli/README.md`) says: built from a checkout by its
 *  install script, which needs Go and puts `daedalus-cli` and `dae` in `~/.local/bin`. There is no
 *  published binary yet; when there is, this becomes its download. Empty would make the card point at
 *  the guide instead. */
export const INSTALL_COMMAND = "git clone https://github.com/anchor-inference/daedalus && daedalus/cli/scripts/install.sh";

/** The command that pairs a computer with this server, at the address this page was opened from. */
export function loginCommand(origin = window.location.origin): string {
  return `daedalus-cli login ${origin}`;
}

/** A token's age in whole days, the unit a token's life is thought about in; today is "today". */
export function tokenAge(issued: string, now = Date.now()): string {
  const days = Math.floor((now - Date.parse(issued)) / 86_400_000);
  if (!Number.isFinite(days) || days < 1) return t("devices.token.today");
  return plural("devices.token.days", days);
}

export function DevicesPanel({ toast }: { toast: (text: string) => void }) {
  return (
    <>
      <PairCard toast={toast} />
      <Waiting />
      <DeviceList toast={toast} />
    </>
  );
}

/** The terminals waiting to be paired, each opening the approval sheet. Nothing while none waits. */
function Waiting() {
  const read = useQuery<{ requests: PairRequest[] }>(PAIRS, { staleMs: 15000 });
  const now = Date.now();
  const waiting = (read.data?.requests ?? []).filter((r) => r.state === "pending" && Date.parse(r.expires_at) > now);
  if (!waiting.length) return null;
  return (
    <div className="card" data-setting="devices.waiting">
      <div className="section-title" style={{ marginTop: 0 }}>{t("devices.waiting.title")}</div>
      <div className="mlist">
        {waiting.map((r) => (
          <div key={r.id} className="mrow device-row" data-pair-request={r.user_code}>
            <div className="mline noradio">
              <div className="mmain">
                <span className="mtitle"><Icon name="laptop" size={15} /><span className="truncate">{r.name}</span></span>
                <span className="mmeta device-meta">{[osLabel(r.os, r.arch), showCode(r.user_code), t("pair.sheet.asked", { t: relTimeLong(r.created_at) })].filter(Boolean).join(" · ")}</span>
              </div>
              <div className="mactions">
                <button type="button" className="btn small primary" onClick={() => openPairing(r.user_code)}>{t("pair.notice.review")}</button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function Command({ text, toast }: { text: string; toast: (text: string) => void }) {
  return (
    <div className="devices-cmd">
      <code className="truncate">{text}</code>
      <button type="button" className="iconbtn small quiet" aria-label={t("common.copy")} title={t("common.copy")}
        onClick={() => void copyText(text).then((ok) => toast(t(ok ? "common.copied" : "devices.copy.failed")))}><Icon name="copy" size={14} /></button>
    </div>
  );
}

function PairCard({ toast }: { toast: (text: string) => void }) {
  const [code, setCode] = useState("");
  const ready = normCode(code).length >= 4;
  const submit = () => {
    if (!ready) return;
    openPairing(code.trim());
    setCode("");
  };
  return (
    <div className="card" data-setting="devices.pair">
      <div className="section-title" style={{ marginTop: 0 }}>{t("devices.pair.title")}</div>
      <ol className="devices-steps">
        <li>
          <span>{t("devices.install")}</span>
          {INSTALL_COMMAND ? <Command text={INSTALL_COMMAND} toast={toast} /> : <span className="sub"> {t("devices.install.soon")}</span>}
        </li>
        <li>
          <span>{t("devices.login")}</span>
          <Command text={loginCommand()} toast={toast} />
        </li>
        <li><span>{t("devices.approve")}</span></li>
      </ol>
      <form className="devices-code" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <label className="settings-row-title" htmlFor="pair-code">{t("devices.code.label")}</label>
        <div className="devices-code-row">
          <input id="pair-code" className="field mono" value={code} autoComplete="off" spellCheck={false} autoCapitalize="characters"
            placeholder="ABCD-EFGH" maxLength={16} onChange={(e) => setCode(e.target.value)} />
          <button type="submit" className="btn primary" disabled={!ready}>{t("devices.code.go")}</button>
        </div>
        <div className="sub">{t("devices.code.sub")}</div>
      </form>
    </div>
  );
}

function DeviceList({ toast }: { toast: (text: string) => void }) {
  const read = useQuery<{ devices: CliDevice[] }>(CLI_DEVICES, { staleMs: 15000 });
  // A computer that connects, drops or is paired elsewhere changes its row here at once.
  useEvent(["device.changed", "device.connection", "device.pairing"], () => invalidate(CLI_DEVICES));
  const devices = read.data?.devices ?? [];
  return (
    <div className="card" data-setting="devices.list">
      <div className="section-title" style={{ marginTop: 0 }}>{t("devices.list.title")}</div>
      {read.error && <div className="sub push-error">{read.error}</div>}
      {read.data && devices.length === 0 && <div className="sub" data-devices-empty="">{t("devices.empty")}</div>}
      {!read.data && !read.error && <div className="sub">{t("common.loading")}</div>}
      <div className="mlist">
        {devices.map((d) => <DeviceRow key={d.id} device={d} toast={toast} />)}
      </div>
    </div>
  );
}

function DeviceRow({ device: d, toast }: { device: CliDevice; toast: (text: string) => void }) {
  const [mode, setMode] = useState<"view" | "rename" | "revoke">("view");
  const [name, setName] = useState(d.name);
  const [busy, setBusy] = useState(false);
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (mode === "rename") field.current?.select();
  }, [mode]);
  async function rename() {
    const next = name.trim();
    if (!next || next === d.name) return setMode("view");
    setBusy(true);
    try {
      await api.patch(`${CLI_DEVICES}/${encodeURIComponent(d.id)}`, { name: next });
      invalidate(CLI_DEVICES);
      // A remote chat's rows and header name its computer: they are read again with the new name.
      invalidate("/api/sessions");
      setMode("view");
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function revoke() {
    setBusy(true);
    try {
      await api.delete(`${CLI_DEVICES}/${encodeURIComponent(d.id)}`);
      toast(t("devices.revoked", { name: d.name }));
      invalidate(CLI_DEVICES);
      invalidate("/api/sessions");
    } catch (e) {
      toast(errorText(e));
      setMode("view");
    } finally {
      setBusy(false);
    }
  }
  const meta = [
    osLabel(d.os, d.arch),
    d.connected ? "" : d.last_seen_at ? t("devices.seen", { t: relTimeLong(d.last_seen_at) }) : t("devices.never"),
    plural("devices.sessions", d.sessions),
    // The token is issued at pairing; the server does not say when a client rotated it since.
    t("devices.token", { age: tokenAge(d.token_issued_at ?? d.paired_at) }),
  ].filter(Boolean).join(" · ");
  const about = [
    t(d.approved_via === "telegram" ? "devices.paired.telegram" : "devices.paired", { date: absDate(d.paired_at) }),
    d.client_version ? `daedalus-cli ${d.client_version}` : "",
    d.hostname && d.hostname !== d.name ? d.hostname : "",
    d.last_address,
    t("devices.lapses", { date: absDate(d.expires_at) }),
  ].filter(Boolean).join(" · ");
  return (
    <div className={`mrow device-row ${mode === "revoke" ? "open" : ""}`} data-device={d.id}>
      <div className="mline noradio">
        <div className="mmain">
          {mode === "rename" ? (
            <form className="device-rename" onSubmit={(e) => { e.preventDefault(); void rename(); }}>
              <input ref={field} className="field" value={name} maxLength={64} aria-label={t("devices.rename.label")} disabled={busy}
                onChange={(e) => setName(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setName(d.name); setMode("view"); } }} />
              <button type="submit" className="btn small primary" disabled={busy || !name.trim()}>{t("common.save")}</button>
              <button type="button" className="btn small" disabled={busy} onClick={() => { setName(d.name); setMode("view"); }}>{t("common.cancel")}</button>
            </form>
          ) : (
            <span className="mtitle">
              <Icon name="laptop" size={15} />
              <span className="truncate">{d.name}</span>
              <span className={`device-link ${d.connected ? "on" : ""}`} data-connected={d.connected ? "" : undefined}>
                <span className="remote-dot" aria-hidden />{t(d.connected ? "devices.connected" : "devices.disconnected")}
              </span>
            </span>
          )}
          <span className="mmeta device-meta" title={about}>{meta}</span>
        </div>
        {mode === "view" && (
          <div className="mactions">
            <button type="button" className="btn small" data-action="rename" onClick={() => setMode("rename")}>{t("devices.rename")}</button>
            <button type="button" className="btn small danger" data-action="revoke" onClick={() => setMode("revoke")}>{t("devices.revoke")}</button>
          </div>
        )}
      </div>
      {mode === "revoke" && (
        <div className="device-confirm" role="group" aria-label={t("devices.revoke.title", { name: d.name })}>
          <div><b>{t("devices.revoke.title", { name: d.name })}</b></div>
          <div className="sub">{t("devices.revoke.body")}</div>
          <div className="device-confirm-actions">
            <button type="button" className="btn small" disabled={busy} onClick={() => setMode("view")}>{t("devices.revoke.keep")}</button>
            <button type="button" className="btn small danger" disabled={busy} data-action="revoke-confirm" onClick={() => void revoke()}>{t("devices.revoke.confirm")}</button>
          </div>
        </div>
      )}
    </div>
  );
}
