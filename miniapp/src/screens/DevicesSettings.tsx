// Settings → Devices: the computers paired with daedalus-cli, how to pair another, and the code a
// terminal shows typed in by hand.
//
// A device is a token the server issued to one computer after the operator approved it; revoking it
// ends that token and closes the computer's open link at once, while the conversations it started
// stay, readable, and pause their next tool call until the computer is paired again and takes them
// back. Revoking asks first, in the app's own confirm dialog: the viewer the app runs in inside
// Telegram refuses the browser's `confirm()`. Requests still waiting for an answer are listed above the devices too, for an operator
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
import { useCopied } from "../ui/components";
import { Button, confirmDialog } from "../ui/primitives";
import { Row, SetBlock, SetGroup } from "../settingsrow";

/** How daedalus-cli is installed, as its guide (`cli/README.md`) says: the install script downloads
 *  the archive for the machine from the newest desktop release, checks it against the release's
 *  signed SHA256SUMS, and puts `daedalus-cli` and `dae` on PATH. PowerShell on Windows, the shell
 *  script everywhere else; the page is usually opened on the computer being paired, so its own
 *  platform picks the one shown. */
export const INSTALL_COMMAND = "curl -fsSL https://raw.githubusercontent.com/anchor-inference/daedalus/main/cli/scripts/install.sh | sh";
export const INSTALL_COMMAND_WINDOWS = "irm https://raw.githubusercontent.com/anchor-inference/daedalus/main/cli/scripts/install.ps1 | iex";

/** The install command for the platform this page runs on. */
export function installCommand(platform = (navigator as { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform ?? ""): string {
  return /^win/i.test(platform) ? INSTALL_COMMAND_WINDOWS : INSTALL_COMMAND;
}

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
    <SetGroup title={t("devices.waiting.title")} data-setting="devices.waiting">
      {waiting.map((r) => (
        <Row
          key={r.id}
          className="device-row"
          data-pair-request={r.user_code}
          title={<span className="device-name"><Icon name="laptop" size={15} /><span className="truncate">{r.name}</span></span>}
          desc={[osLabel(r.os, r.arch), showCode(r.user_code), t("pair.sheet.asked", { t: relTimeLong(r.created_at) })].filter(Boolean).join(" · ")}
        >
          <Button size="sm" variant="primary" onClick={() => openPairing(r.user_code)}>{t("pair.notice.review")}</Button>
        </Row>
      ))}
    </SetGroup>
  );
}

function Command({ text, toast }: { text: string; toast: (text: string) => void }) {
  const [copied, copy] = useCopied();
  return (
    <div className="devices-cmd">
      <code className="truncate">{text}</code>
      <button type="button" className="iconbtn small quiet" aria-label={t("common.copy")} title={t("common.copy")}
        onClick={() => void copy(text).then((ok) => { if (!ok) toast(t("devices.copy.failed")); })}><Icon name={copied ? "check" : "copy"} size={14} /></button>
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
    <SetGroup title={t("devices.pair.title")} data-setting="devices.pair">
      <SetBlock>
        <ol className="devices-steps">
          <li>
            <span>{t("devices.install")}</span>
            <Command text={installCommand()} toast={toast} />
          </li>
          <li>
            <span>{t("devices.login")}</span>
            <Command text={loginCommand()} toast={toast} />
          </li>
          <li><span>{t("devices.approve")}</span></li>
        </ol>
      </SetBlock>
      <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <Row title={t("devices.code.label")} htmlFor="pair-code" desc={t("devices.code.sub")}>
          <span className="devices-code-row">
            <input id="pair-code" className="field mono" value={code} autoComplete="off" spellCheck={false} autoCapitalize="characters"
              placeholder="ABCD-EFGH" maxLength={16} onChange={(e) => setCode(e.target.value)} />
            <Button type="submit" variant="primary" disabled={!ready}>{t("devices.code.go")}</Button>
          </span>
        </Row>
      </form>
    </SetGroup>
  );
}

function DeviceList({ toast }: { toast: (text: string) => void }) {
  const read = useQuery<{ devices: CliDevice[] }>(CLI_DEVICES, { staleMs: 15000 });
  // A computer that connects, drops or is paired elsewhere changes its row here at once.
  useEvent(["device.changed", "device.connection", "device.pairing"], () => invalidate(CLI_DEVICES));
  const devices = read.data?.devices ?? [];
  return (
    <SetGroup title={t("devices.list.title")} data-setting="devices.list">
      {read.error && <SetBlock><span className="set-hint push-error">{read.error}</span></SetBlock>}
      {read.data && devices.length === 0 && <SetBlock data-devices-empty=""><span className="set-hint">{t("devices.empty")}</span></SetBlock>}
      {!read.data && !read.error && <SetBlock><span className="set-hint">{t("common.loading")}</span></SetBlock>}
      {devices.map((d) => <DeviceRow key={d.id} device={d} toast={toast} />)}
    </SetGroup>
  );
}

function DeviceRow({ device: d, toast }: { device: CliDevice; toast: (text: string) => void }) {
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState(d.name);
  const [busy, setBusy] = useState(false);
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (renaming) field.current?.select();
  }, [renaming]);
  async function rename() {
    const next = name.trim();
    if (!next || next === d.name) return setRenaming(false);
    setBusy(true);
    try {
      await api.patch(`${CLI_DEVICES}/${encodeURIComponent(d.id)}`, { name: next });
      invalidate(CLI_DEVICES);
      // A remote chat's rows and header name its computer: they are read again with the new name.
      invalidate("/api/sessions");
      setRenaming(false);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function revoke() {
    if (!(await confirmDialog({ title: t("devices.revoke.title", { name: d.name }), body: t("devices.revoke.body"), action: t("devices.revoke.confirm"), cancel: t("devices.revoke.keep"), danger: true }))) return;
    setBusy(true);
    try {
      await api.delete(`${CLI_DEVICES}/${encodeURIComponent(d.id)}`);
      invalidate(CLI_DEVICES);
      invalidate("/api/sessions");
    } catch (e) {
      toast(errorText(e));
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
    <Row
      className="device-row"
      data-device={d.id}
      title={renaming ? (
        <form className="device-rename" onSubmit={(e) => { e.preventDefault(); void rename(); }}>
          <input ref={field} className="field" value={name} maxLength={64} aria-label={t("devices.rename.label")} disabled={busy}
            onChange={(e) => setName(e.target.value)} onKeyDown={(e) => { if (e.key === "Escape") { setName(d.name); setRenaming(false); } }} />
          <Button type="submit" size="sm" variant="primary" disabled={busy || !name.trim()}>{t("common.save")}</Button>
          <Button size="sm" disabled={busy} onClick={() => { setName(d.name); setRenaming(false); }}>{t("common.cancel")}</Button>
        </form>
      ) : (
        <span className="device-name">
          <Icon name="laptop" size={15} />
          <span className="truncate">{d.name}</span>
          <span className={`device-link ${d.connected ? "on" : ""}`} data-connected={d.connected ? "" : undefined}>
            <span className="remote-dot" aria-hidden />{t(d.connected ? "devices.connected" : "devices.disconnected")}
          </span>
        </span>
      )}
      desc={<span title={about}>{meta}</span>}
    >
      {!renaming && (
        <>
          <Button size="sm" data-action="rename" onClick={() => setRenaming(true)}>{t("devices.rename")}</Button>
          <Button size="sm" variant="danger" data-action="revoke" disabled={busy} onClick={() => void revoke()}>{t("devices.revoke")}</Button>
        </>
      )}
    </Row>
  );
}
