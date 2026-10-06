// Connected calendars: what is connected and how its sync is going, and how to connect another. Each
// provider is a card; picking one shows the few steps it takes, in order, beside the form they fill.
// A conflict is offered as the two versions to keep, read from the account's conflict fields.

import { useState, type CSSProperties } from "react";
import { api } from "../api";
import { relTime } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { Sheet } from "../ui/dialogs";
import { confirmAsync, errorText } from "../ui";
import { useQuery, invalidate } from "../store";
import { ACCOUNTS, CALENDARS, PALETTE, refreshPlanner } from "./data";
import type { Account, Provider } from "./types";

const PROVIDERS: Provider[] = ["google", "outlook", "yandex", "icloud", "caldav", "ics"];
/** Where a CalDAV preset's server is; the reader types only the account and its app password. */
const SERVERS: Partial<Record<Provider, string>> = { yandex: "https://caldav.yandex.ru", icloud: "https://caldav.icloud.com" };
const CONSOLES: Partial<Record<Provider, string>> = {
  google: "https://console.cloud.google.com/apis/credentials",
  outlook: "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
  yandex: "https://id.yandex.ru/security/app-passwords",
  icloud: "https://account.apple.com/account/manage",
};

function providerOf(account: Account): Provider {
  if (account.provider === "caldav" && (account.preset === "yandex" || account.preset === "icloud")) return account.preset;
  return account.provider;
}

export function ProviderGlyph({ provider }: { provider: Provider }) {
  return (
    <span className={`cal-provider cal-provider-${provider}`} aria-hidden="true">
      <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
        {provider === "google" ? <path d="M20 12.2c0 4.6-3.2 7.8-8 7.8a8 8 0 1 1 5.4-13.9M20 12h-8" />
          : provider === "outlook" ? <><rect x="3" y="6" width="11" height="12" rx="2" /><circle cx="8.5" cy="12" r="2.5" /><path d="M14 8h7v8h-7M14 9l3.5 3L21 9" /></>
          : provider === "yandex" ? <path d="M14 20V4h-1.6C9.6 4 8 5.6 8 8.2s1.4 4 4 4.6M12 12.8L7.5 20" />
          : provider === "icloud" ? <path d="M7 18h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.4 1.4A3.3 3.3 0 0 0 7 18z" />
          : provider === "ics" ? <><path d="M5 11a8 8 0 0 1 8 8M5 5a14 14 0 0 1 14 14" /><circle cx="6" cy="18" r="1.5" /></>
          : <><rect x="4" y="5" width="16" height="15" rx="2" /><path d="M4 10h16M9 3v4M15 3v4M9 15l2 2 4-4" /></>}
      </svg>
    </span>
  );
}

export function statusOf(account: Account): "ok" | "error" | "syncing" | "never" {
  if (account.status) return account.status;
  if (account.sync_error) return "error";
  return account.last_sync_at ? "ok" : "never";
}

export function ConnectionsSheet({ onClose, toast }: { onClose: () => void; toast: (text: string) => void }) {
  const { data: accounts, error, refresh } = useQuery<Account[]>(ACCOUNTS, { pollMs: 15000, staleMs: 3000 });
  const [adding, setAdding] = useState<Provider | null>(null);
  const [busy, setBusy] = useState("");

  const after = () => { refresh(); invalidate(CALENDARS); refreshPlanner(); };

  async function sync(account: Account) {
    setBusy(account.id);
    try { await api.post(`/api/calendar/accounts/${encodeURIComponent(account.id)}/sync`); toast(t("cal.synced")); }
    catch (exc) { toast(errorText(exc)); }
    finally { setBusy(""); after(); }
  }

  async function disconnect(account: Account) {
    if (!(await confirmAsync(t("cal.disconnect.title", { name: account.name }), { body: t(account.provider === "ics" ? "cal.disconnect.body.ics" : "cal.disconnect.body"), action: t("cal.disconnect") }))) return;
    try { await api.delete(`/api/calendar/accounts/${encodeURIComponent(account.id)}`); toast(t("cal.disconnected")); }
    catch (exc) { toast(errorText(exc)); }
    after();
  }

  async function resolve(eventId: string, choice: "local" | "remote") {
    setBusy(eventId);
    try { await api.post(`/api/calendar/events/${encodeURIComponent(eventId)}/resolve`, { choice }); toast(t("cal.conflict.resolved")); }
    catch (exc) { toast(errorText(exc)); }
    finally { setBusy(""); after(); }
  }

  return (
    <Sheet title={adding ? t(`cal.provider.${adding}`) : t("cal.connections")} onClose={onClose} className="cal-connections-sheet"
      head={adding ? <button type="button" className="btn small ghost cal-back" onClick={() => setAdding(null)}><Icon name="back" size={14} /> {t("cal.connect.back")}</button> : undefined}>
      {adding ? <ConnectForm provider={adding} toast={toast} onDone={() => { setAdding(null); after(); }} /> : (
        <div className="cal-connections">
          {error && !accounts && <div className="cal-error">{error}</div>}
          {accounts && accounts.length > 0 && (
            <section>
              <h4 className="cal-section-title">{t("cal.connections.yours")}</h4>
              <ul className="cal-accounts">
                {accounts.map((account) => {
                  const status = statusOf(account);
                  return (
                    <li key={account.id} className={`cal-account ${status}`}>
                      <ProviderGlyph provider={providerOf(account)} />
                      <div className="cal-account-main">
                        <b>{account.name}</b>
                        <span className="cal-account-meta">
                          {t(`cal.provider.${providerOf(account)}`)} · <span className={`cal-status ${status}`}>{status === "ok" ? t("cal.sync.ok", { when: relTime(account.last_sync_at) }) : t(`cal.sync.${status}`)}</span>
                        </span>
                        {account.sync_error && <span className="cal-account-error">{account.sync_error}</span>}
                        {(account.conflicts ?? []).map((conflict) => (
                          <div key={conflict.event_id} className="cal-account-conflict">
                            <span><Icon name="alert" size={13} /> {t("cal.conflict.event", { title: conflict.title })}</span>
                            <div className="cal-conflict-actions">
                              <button type="button" className="btn small" disabled={busy === conflict.event_id} onClick={() => void resolve(conflict.event_id, "local")}>{t("cal.conflict.local")}</button>
                              <button type="button" className="btn small" disabled={busy === conflict.event_id} onClick={() => void resolve(conflict.event_id, "remote")}>{t("cal.conflict.remote.use")}</button>
                            </div>
                          </div>
                        ))}
                      </div>
                      <button type="button" className="iconbtn" disabled={busy === account.id} onClick={() => void sync(account)} aria-label={t("cal.sync.now.named", { name: account.name })} title={t("cal.sync.now")}><Icon name="reload" size={16} /></button>
                      <button type="button" className="iconbtn" onClick={() => void disconnect(account)} aria-label={t("cal.disconnect.named", { name: account.name })} title={t("cal.disconnect")}><Icon name="unlink" size={16} /></button>
                    </li>
                  );
                })}
              </ul>
            </section>
          )}
          <section>
            <h4 className="cal-section-title">{t("cal.connect.add")}</h4>
            <div className="cal-provider-grid">
              {PROVIDERS.map((provider) => (
                <button key={provider} type="button" className="cal-provider-card" onClick={() => setAdding(provider)}>
                  <ProviderGlyph provider={provider} />
                  <span><b>{t(`cal.provider.${provider}`)}</b><small>{t(`cal.provider.${provider}.what`)}</small></span>
                </button>
              ))}
            </div>
          </section>
        </div>
      )}
    </Sheet>
  );
}

function ConnectForm({ provider, toast, onDone }: { provider: Provider; toast: (text: string) => void; onDone: () => void }) {
  const [name, setName] = useState(t(`cal.provider.${provider}`));
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [remoteId, setRemoteId] = useState("primary");
  const [server, setServer] = useState(SERVERS[provider] ?? "");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [url, setUrl] = useState("");
  const [color, setColor] = useState(PALETTE[provider === "ics" ? 2 : 0].hex);
  const [busy, setBusy] = useState(false);
  const oauth = provider === "google" || provider === "outlook";
  const redirect = `${window.location.origin}/api/calendar/oauth/callback`;
  const steps = [1, 2, 3, 4].map((n) => `cal.steps.${provider}.${n}`).filter((key) => t(key) !== `[${key}]`);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      if (oauth) {
        const started = await api.post<{ url: string }>("/api/calendar/oauth/start", { provider, name, client_id: clientId, client_secret: clientSecret, remote_calendar_id: remoteId || "primary" });
        window.location.assign(started.url);
        return;
      }
      if (provider === "ics") await api.post("/api/calendar/subscriptions", { name, url, color });
      else await api.post("/api/calendar/accounts", { provider: "caldav", preset: provider === "caldav" ? "" : provider, name, server_url: server, remote_calendar_id: remoteId === "primary" ? "" : remoteId, credentials: { username, password } });
      toast(t("cal.connected"));
      onDone();
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  return (
    <form className="cal-connect" onSubmit={submit}>
      <ol className="cal-steps">
        {steps.map((key, i) => <li key={key}><span className="cal-step-n">{i + 1}</span><span>{t(key)}</span></li>)}
      </ol>
      {CONSOLES[provider] && <a className="btn small cal-console" href={CONSOLES[provider]} target="_blank" rel="noreferrer"><Icon name="external" size={14} /> {t(`cal.console.${provider}`)}</a>}
      {oauth && (
        <div className="cal-redirect">
          <span>{t("cal.redirect")}</span>
          <code>{redirect}</code>
          <button type="button" className="iconbtn small" onClick={() => { void navigator.clipboard?.writeText(redirect).then(() => toast(t("cal.copied")), () => undefined); }} aria-label={t("cal.copy.redirect")} title={t("cal.copy.redirect")}><Icon name="copy" size={14} /></button>
        </div>
      )}
      <label className="field" htmlFor="cal-connect-name">{t("cal.connect.name")}</label>
      <input id="cal-connect-name" className="field" required value={name} onChange={(e) => setName(e.target.value)} />
      {oauth && <>
        <label className="field" htmlFor="cal-client-id">{t("cal.client.id")}</label>
        <input id="cal-client-id" className="field" required value={clientId} onChange={(e) => setClientId(e.target.value)} autoComplete="off" />
        <label className="field" htmlFor="cal-client-secret">{t("cal.client.secret")}</label>
        <input id="cal-client-secret" className="field" type="password" required value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} autoComplete="off" />
      </>}
      {(provider === "caldav") && <>
        <label className="field" htmlFor="cal-server">{t("cal.server")}</label>
        <input id="cal-server" className="field" type="url" required value={server} onChange={(e) => setServer(e.target.value)} placeholder="https://" />
      </>}
      {!oauth && provider !== "ics" && <>
        <label className="field" htmlFor="cal-user">{t("cal.username")}</label>
        <input id="cal-user" className="field" required value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        <label className="field" htmlFor="cal-pass">{t(provider === "caldav" ? "cal.password" : "cal.app.password")}</label>
        <input id="cal-pass" className="field" type="password" required value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
      </>}
      {provider === "ics" && <>
        <label className="field" htmlFor="cal-ics">{t("cal.ics.url")}</label>
        <input id="cal-ics" className="field" type="url" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" />
        <label className="field" id="cal-ics-color">{t("cal.field.color")}</label>
        <div className="cal-swatches" role="group" aria-labelledby="cal-ics-color">{PALETTE.map((p) => <button key={p.id} type="button" className={`cal-swatch ${color === p.hex ? "on" : ""}`} style={{ "--c": p.hex } as CSSProperties} aria-pressed={color === p.hex} onClick={() => setColor(p.hex)} aria-label={t(`cal.color.${p.id}`)} title={t(`cal.color.${p.id}`)}>{color === p.hex && <Icon name="check" size={12} />}</button>)}</div>
      </>}
      {provider !== "ics" && <>
        <label className="field" htmlFor="cal-remote">{t("cal.remote.id")}</label>
        <input id="cal-remote" className="field" value={remoteId} onChange={(e) => setRemoteId(e.target.value)} />
        <div className="cal-hint">{t("cal.remote.hint")}</div>
      </>}
      <div className="sheet-foot cal-foot">
        <span className="cal-spacer" />
        <button type="submit" className="btn primary" disabled={busy}>{oauth ? t("cal.connect.signin") : provider === "ics" ? t("cal.subscribe") : t("cal.connect")}</button>
      </div>
    </form>
  );
}
