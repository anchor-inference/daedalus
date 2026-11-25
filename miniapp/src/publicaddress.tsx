// Settings → Public address: a tunnel the operator opens from the native application, for agent links
// and shared services. The launcher holds the tunnel and the host relays it (`GET /api/public-address`);
// this page says what the operator wants and tells the truth about what came of it, so `up` is not
// called "answering" until a request has really come back through the address.
//
// A server has an address of its own and answers `native: false`: nothing of this is drawn there, not
// even the entry in the index. Off by default is the launcher's, and no step here asks twice.

import { FormEvent, useState } from "react";
import { api } from "./api";
import { errorText } from "./ui";
import { useCopied } from "./ui/components";
import { Button, Select, Switch } from "./ui/primitives";
import { t } from "./i18n";
import { pathFor } from "./router";
import { Row, SetBlock, SetGroup, TextBlock } from "./settingsrow";
import { go, useMedia } from "./shell";
import { prime, useQuery } from "./store";
import { QrCode } from "./twofactor";
import type { Capabilities } from "./capabilities";
import "./publicaddress.css";

export const ADDRESS = "/api/public-address";
/** Long enough not to crowd the launcher, short enough that a confirmed address shows while it is looked at. */
const POLL_MS = 3000;
const NGROK_DOWNLOAD = "https://ngrok.com/download";
const FRP_DOCS = "https://github.com/anchor-inference/daedalus/blob/main/desktop/README.md#public-address";

export type AddressProvider = "ngrok" | "tailscale" | "cloudflare" | "frp";
export const PROVIDERS: AddressProvider[] = ["ngrok", "tailscale", "cloudflare", "frp"];
type AddressState = "off" | "installing" | "starting" | "sign_in" | "up" | "reconnecting" | "failed";

export type Tunnel = {
  tunneld: boolean;
  enabled: boolean;
  provider: AddressProvider;
  state: AddressState;
  address: string;
  confirmed: boolean;
  sign_in?: string;
  code?: string;
  detail?: string;
  wakes: number;
  /** Whether a command the agent runs is kept from the file a token is in: true only in the Linux sandbox. */
  tokens_hidden: boolean;
  exposed: { links: number; services: number };
  qr: string[];
  providers: {
    ngrok: { client: boolean; token: boolean; found?: string };
    tailscale: { client: boolean; signed_in?: boolean };
    cloudflare: { client: boolean; token: boolean };
    frp: { client: boolean; token: boolean; server?: string; domain?: string };
  };
};

export type PublicAddress =
  | { native: false }
  | { native: true; available: false; reason?: string }
  | ({ native: true; available: true; reason?: undefined } & Tunnel);

type AddressChange = Partial<{ enabled: boolean; provider: AddressProvider; trust_ngrok: boolean; ngrok_token: string; frp_token: string; frp_server: string; frp_domain: string; sign_out: boolean }>;

/** The failures the launcher and its program can name. Anything else is worded as `other`, never shown raw. */
const CODES = ["no_client", "client_unconfirmed", "client_changed", "token_rejected", "blocked", "https_off", "funnel_off", "server_refused", "not_configured", "no_tunneld", "download_failed", "other"];

/** The state in the operator's words. `detail` is English for support and is never this line. */
export function addressStatus(view: Pick<Tunnel, "state" | "confirmed" | "code">): { text: string; tone: "" | "ok" | "attn" | "bad" } {
  switch (view.state) {
    case "up":
      return view.confirmed ? { text: t("address.state.answering"), tone: "ok" } : { text: t("address.state.checking"), tone: "attn" };
    case "failed":
      return { text: t(`address.code.${CODES.includes(view.code ?? "") ? view.code : "other"}`), tone: "bad" };
    case "off":
      return { text: t("address.state.off"), tone: "" };
    case "reconnecting":
    case "sign_in":
      return { text: t(`address.state.${view.state}`), tone: "attn" };
    default:
      return { text: t(`address.state.${view.state}`), tone: "" };
  }
}

/** Whether a link made now would work away from this machine. */
export function addressIsUp(view: PublicAddress | undefined): boolean {
  return !!view && view.native && view.available && view.state === "up";
}

/** The way to the section from wherever it is needed. */
export function AddressLink() {
  const path = pathFor("settings", "address");
  return <a href={path} onClick={(e) => go(e, path)}>{t("address.where")}</a>;
}

/** A native installation with no public address up, said once where it matters: nothing on a server. */
export function AddressNote({ text }: { text?: string }) {
  const { data } = useQuery<PublicAddress>(ADDRESS, { staleMs: 5000 });
  if (!data?.native || addressIsUp(data)) return null;
  return <div className="sub" data-address-note="">{text && `${text} `}<AddressLink /></div>;
}

/** A token is written and never read back: the field stays empty, and "saved" is all that is ever shown. */
function TokenField({ id, label, saved, busy, setting, onSave }: { id: string; label: string; saved: boolean; busy: boolean; setting: string; onSave: (token: string) => Promise<boolean> }) {
  const [draft, setDraft] = useState("");
  const [replacing, setReplacing] = useState(false);
  // Two buttons beside the words leave them a third of a phone's width, as on the two-factor card.
  const narrow = useMedia("(max-width: 640px)");

  async function save(e: FormEvent) {
    e.preventDefault();
    if (await onSave(draft.trim())) {
      setDraft("");
      setReplacing(false);
    }
  }

  if (saved && !replacing) {
    return (
      <Row title={label} desc={t("address.token.saved")} stack={narrow} data-setting={setting} data-token="saved">
        <Button size="sm" onClick={() => setReplacing(true)}>{t("address.token.replace")}</Button>
        <Button size="sm" disabled={busy} onClick={() => void onSave("")}>{t("address.token.forget")}</Button>
      </Row>
    );
  }
  return (
    <form className="settings-block" data-setting={setting} data-token="empty" onSubmit={save}>
      <label className="field-label" htmlFor={id}>{label}</label>
      <div className="share-field">
        <input id={id} className="field mono" type="password" value={draft} onChange={(e) => setDraft(e.target.value)} autoComplete="new-password" spellCheck={false} maxLength={512} />
        <Button type="submit" variant="primary" size="sm" disabled={busy || !draft.trim()}>{t("common.save")}</Button>
        {saved && <Button size="sm" onClick={() => (setDraft(""), setReplacing(false))}>{t("common.cancel")}</Button>}
      </div>
    </form>
  );
}

function ExternalLink({ href, text }: { href: string; text: string }) {
  return <a href={href} target="_blank" rel="noopener">{text}</a>;
}

/** What the chosen provider needs besides the choice: a client found, a token, a server. Rows of the
 *  provider's card; the sentences that explain them are blocks of it. */
function Setup({ view, busy, change, windows }: { view: Tunnel; busy: boolean; change: (body: AddressChange) => Promise<boolean>; windows: boolean }) {
  const ngrok = view.providers.ngrok;
  const frp = view.providers.frp;
  const readable = !view.tokens_hidden && <SetBlock data-address-readable=""><span className="set-hint">{t("address.token.readable")}</span></SetBlock>;
  switch (view.provider) {
    case "ngrok":
      return (
        <>
          {readable}
          {ngrok.found && !ngrok.client && (
            <Row title={t("address.ngrok.found")} desc={<span className="mono">{ngrok.found}</span>} data-setting="address.ngrok.trust">
              <Button size="sm" disabled={busy} onClick={() => void change({ trust_ngrok: true })}>{t("address.ngrok.trust")}</Button>
            </Row>
          )}
          {!ngrok.found && !ngrok.client && <SetBlock data-address-get="ngrok"><span className="set-hint">{t("address.ngrok.missing")} <ExternalLink href={NGROK_DOWNLOAD} text={t("address.ngrok.get")} /></span></SetBlock>}
          <TokenField id="address-ngrok-token" label={t("address.ngrok.token")} saved={ngrok.token} busy={busy} setting="address.ngrok.token" onSave={(token) => change({ ngrok_token: token })} />
        </>
      );
    case "tailscale":
      return view.providers.tailscale.signed_in && view.state !== "sign_in" ? (
        <Row title={t("address.tailscale.signedin")} data-setting="address.tailscale.signout">
          <Button size="sm" disabled={busy} onClick={() => void change({ sign_out: true })}>{t("address.tailscale.signout")}</Button>
        </Row>
      ) : null;
    case "frp":
      return (
        <>
          {/* Keyed by the kept value: these fields hold their own text, so one changed from another window would not show. */}
          <TextBlock key={`server-${frp.server ?? ""}`} id="address-frp-server" label={t("address.frp.server")} value={frp.server ?? ""} placeholder="vps.example.com:7000" setting="address.frp.server" onSave={(value) => void change({ frp_server: value })} />
          <TextBlock key={`domain-${frp.domain ?? ""}`} id="address-frp-domain" label={t("address.frp.domain")} value={frp.domain ?? ""} placeholder="tunnel.example.com" setting="address.frp.domain" onSave={(value) => void change({ frp_domain: value })} />
          <TokenField id="address-frp-token" label={t("address.frp.token")} saved={frp.token} busy={busy} setting="address.frp.token" onSave={(token) => change({ frp_token: token })} />
          <SetBlock>
            <span className="set-hint"><ExternalLink href={FRP_DOCS} text={t("address.frp.docs")} /></span>
            <span className="set-hint" data-address-frp-tls="">{t("address.frp.tls")}</span>
            {windows && <span className="set-hint" data-address-defender="">{t("address.frp.defender")}</span>}
          </SetBlock>
          {readable}
        </>
      );
    default:
      return null;
  }
}

function Tunnelled({ view }: { view: Tunnel }) {
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const [copied, copy] = useCopied();
  // Whether the host runs on Windows, as the reveal button learns it: it is frpc on that machine that a
  // scanner flags, whatever the page is opened on.
  const windows = useQuery<Capabilities>("/api/capabilities", { staleMs: 20000 }).data?.reveal?.platform === "windows";
  const status = addressStatus(view);
  const link = view.state === "sign_in" && view.sign_in?.startsWith("https://") ? view.sign_in : "";
  const shown = view.enabled && !!view.address && (view.state === "up" || view.state === "reconnecting");

  /** True when the launcher took it. A refusal arrives in its own words, in English, which is all there is. */
  async function change(body: AddressChange): Promise<boolean> {
    setBusy(true);
    setProblem("");
    try {
      prime(ADDRESS, await api.put<PublicAddress>(ADDRESS, body));
      return true;
    } catch (e) {
      setProblem(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function copyAddress() {
    if (!(await copy(view.address))) setProblem(t("svc.copyfail"));
  }

  return (
    <>
      <SetGroup className="address-card" data-address={view.state} data-provider={view.provider}>
        <Row title={t("address.switch")} desc={<span className={`address-status ${status.tone}`} role={status.tone === "bad" ? "alert" : "status"} data-address-status="">{status.text}</span>} data-setting="address.enabled">
          <Switch checked={view.enabled} disabled={busy} onChange={(enabled) => void change({ enabled })} label={t("address.switch")} />
        </Row>
        {view.state === "failed" && view.detail && (
          <SetBlock>
            <details className="address-detail">
              <summary>{t("address.detail")}</summary>
              <code className="mono">{view.detail}</code>
            </details>
          </SetBlock>
        )}
        {link && (
          <Row title={t("address.signin.title")} data-setting="address.signin">
            <a className="btn primary small" href={link} target="_blank" rel="noopener">{t("address.signin")}</a>
          </Row>
        )}
        {problem && <SetBlock><span className="login-error address-problem" role="alert">{problem}</span></SetBlock>}
      </SetGroup>

      <SetGroup className="address-card" data-address-provider={view.provider}>
        {/* One choice of four with a sentence for each: the list says what each is, and the row says it
            again for the one in use, so the sentence is read where the choice was made. */}
        <Row title={t("address.provider")} htmlFor="address-provider" desc={t(`address.provider.${view.provider}.sub`)} data-setting="address.provider">
          <Select
            id="address-provider"
            label={t("address.provider")}
            value={view.provider}
            disabled={busy}
            onChange={(provider) => void change({ provider })}
            options={PROVIDERS.map((provider) => ({ id: provider, label: t(`address.provider.${provider}`), hint: t(`address.provider.${provider}.sub`) }))}
          />
        </Row>
        <Setup view={view} busy={busy} change={change} windows={windows} />
      </SetGroup>

      {shown && (
        <SetGroup title={t("address.current")} className="address-card" data-address-shown="">
          <SetBlock>
            <div className="share-field">
              <input className="field mono" readOnly value={view.address} title={view.address} onFocus={(e) => e.target.select()} aria-label={t("address.current")} />
              <Button size="sm" icon={copied ? "check" : "copy"} onClick={() => void copyAddress()}>{t("common.copy")}</Button>
            </div>
            {view.qr.length > 0 && <QrCode rows={view.qr} label={t("address.qr")} />}
          </SetBlock>
        </SetGroup>
      )}

      <SetGroup title={t("address.exposed")} className="address-card" data-address-exposed="">
        <Row title={t("address.exposed.links")}><span className="settings-value" data-exposed="links">{view.exposed.links}</span></Row>
        <Row title={t("address.exposed.services")}><span className="settings-value" data-exposed="services">{view.exposed.services}</span></Row>
        <SetBlock>
          <span className="set-hint">{t("address.exposed.notapp")}</span>
          <span className="set-hint">{t("address.down")}</span>
        </SetBlock>
      </SetGroup>
    </>
  );
}

export function PublicAddressPanel() {
  // Polled here and nowhere else: the query stops with the section, and the index asks only once.
  const { data, error } = useQuery<PublicAddress>(ADDRESS, { pollMs: POLL_MS, staleMs: 1000 });
  if (!data) return error ? <SetGroup><SetBlock><span className="set-hint" role="alert">{error}</span></SetBlock></SetGroup> : <div className="empty">{t("common.loading")}</div>;
  if (!data.native) return null;
  if (!data.available || !data.tunneld) {
    const reason = data.available ? "no_tunneld" : data.reason === "old_launcher" ? "old_launcher" : "no_launcher";
    return <SetGroup data-address-unavailable={reason}><SetBlock><span className="set-hint">{t(`address.unavailable.${reason}`)}</span></SetBlock></SetGroup>;
  }
  return <Tunnelled view={data} />;
}
