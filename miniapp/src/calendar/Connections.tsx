// Connected calendars: what is connected and how its sync is going, and how to connect another. Each
// provider is a card; picking one shows the few steps it takes, in order, beside the form they fill.
// A conflict is offered as the two versions to keep, read from the account's conflict fields.

import { useState, type CSSProperties } from "react";
import { useCopied } from "../ui/components";
import { api } from "../api";
import { relTime } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { Button, Field, IconButton, Sheet } from "../ui/primitives";
import { FamilyGlyph } from "../ui/provider-mark";
import { confirmAsync, errorText } from "../ui";
import { useQuery, invalidate } from "../store";
import { ACCOUNTS, CALENDARS, PALETTE, refreshPlanner } from "./data";
import type { Account, Occurrence, Provider } from "./types";

const PROVIDERS: Provider[] = ["google", "outlook", "yandex", "icloud", "caldav", "ics"];
/** Where a CalDAV preset's server is; the reader types only the account and its app password. */
const SERVERS: Partial<Record<Provider, string>> = { yandex: "https://caldav.yandex.ru", icloud: "https://caldav.icloud.com" };
const CONSOLES: Partial<Record<Provider, string>> = {
  google: "https://console.cloud.google.com/apis/credentials",
  outlook: "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
  yandex: "https://id.yandex.ru/security/app-passwords",
  icloud: "https://account.apple.com/account/manage",
};

/** The service an account was set up as. The host stores Yandex and iCloud as CalDAV with their
 *  server filled in, so the server says which it is. */
function providerOf(account: Account): Provider {
  if (account.provider !== "caldav") return account.provider;
  const server = account.server_url ?? "";
  if (server.startsWith(SERVERS.yandex!)) return "yandex";
  if (server.startsWith(SERVERS.icloud!)) return "icloud";
  return "caldav";
}

/** Where an account lives, under its name: the CalDAV server and login, or the feed's host. */
function whereOf(account: Account): string {
  if (account.provider === "ics") return account.host ?? "";
  if (account.provider === "caldav") return [account.username, providerOf(account) === "caldav" ? account.server_url?.replace(/^https:\/\//, "") : ""].filter(Boolean).join(" · ");
  return "";
}

export function ProviderGlyph({ provider }: { provider: Provider }) {
  return (
    <span className={`cal-provider cal-provider-${provider}`} aria-hidden="true">
      <FamilyGlyph family={`cal-${provider}`} size={14} />
    </span>
  );
}

export function statusOf(account: Account): "ok" | "error" | "syncing" | "never" {
  if (account.status) return account.status;
  if (account.sync_error) return "error";
  return account.last_sync_at ? "ok" : "never";
}

/** The events of a connected calendar that wait for a choice: the account says how many, and the
 *  occurrences carry the flag, so a year around today is read for them and each gets its buttons. */
function AccountConflicts({ calendarId, count, busy, onResolve }: { calendarId: string; count: number; busy: string; onResolve: (eventId: string, choice: "local" | "remote") => void }) {
  const [from] = useState(() => new Date(Date.now() - 30 * 86400000).toISOString());
  const [to] = useState(() => new Date(Date.now() + 365 * 86400000).toISOString());
  const { data } = useQuery<Occurrence[]>(`/api/calendar/events?start=${encodeURIComponent(from)}&end=${encodeURIComponent(to)}&calendars=${encodeURIComponent(calendarId)}`, { staleMs: 3000 });
  const seen = new Set<string>();
  const found = (data ?? []).filter((o) => o.conflict && !seen.has(o.event_id) && seen.add(o.event_id));
  return (
    <div className="cal-account-conflict">
      <span><Icon name="alert" size={13} /> {plural("cal.conflict.count", count)}</span>
      {found.map((o) => (
        <div key={o.event_id} className="cal-account-conflict-row">
          <span className="cal-account-conflict-title">{t("cal.conflict.event", { title: o.title || t("cal.untitled") })}</span>
          <div className="cal-conflict-actions">
            <Button size="sm" disabled={busy === o.event_id} onClick={() => onResolve(o.event_id, "local")}>{t("cal.conflict.local")}</Button>
            <Button size="sm" disabled={busy === o.event_id} onClick={() => onResolve(o.event_id, "remote")}>{t("cal.conflict.remote.use")}</Button>
          </div>
        </div>
      ))}
    </div>
  );
}

export function ConnectionsSheet({ onClose, toast }: { onClose: () => void; toast: (text: string) => void }) {
  const { data: accounts, error, refresh } = useQuery<Account[]>(ACCOUNTS, { pollMs: 15000, staleMs: 3000 });
  const [adding, setAdding] = useState<Provider | null>(null);
  const [busy, setBusy] = useState("");

  const after = () => { refresh(); invalidate(CALENDARS); refreshPlanner(); };

  async function sync(account: Account) {
    setBusy(account.id);
    try { await api.post(`/api/calendar/accounts/${encodeURIComponent(account.id)}/sync`); }
    catch (exc) { toast(errorText(exc)); }
    finally { setBusy(""); after(); }
  }

  async function disconnect(account: Account) {
    if (!(await confirmAsync(t("cal.disconnect.title", { name: account.name }), { body: t(account.provider === "ics" ? "cal.disconnect.body.ics" : "cal.disconnect.body"), action: t("cal.disconnect") }))) return;
    try { await api.delete(`/api/calendar/accounts/${encodeURIComponent(account.id)}`); }
    catch (exc) { toast(errorText(exc)); }
    after();
  }

  async function resolve(eventId: string, choice: "local" | "remote") {
    setBusy(eventId);
    try { await api.post(`/api/calendar/events/${encodeURIComponent(eventId)}/resolve`, { choice }); }
    catch (exc) { toast(errorText(exc)); }
    finally { setBusy(""); after(); }
  }

  return (
    <Sheet title={adding ? t(`cal.provider.${adding}`) : t("cal.connections")} onClose={onClose} size="md" className="cal-connections-sheet"
      head={adding ? <Button variant="quiet" size="sm" className="cal-back" icon="back" onClick={() => setAdding(null)}>{t("cal.connect.back")}</Button> : undefined}>
      {adding ? <ConnectForm provider={adding} toast={toast} onDone={() => { setAdding(null); after(); }} /> : (
        <div className="cal-set">
          {error && !accounts && <div className="cal-error">{error}</div>}
          {accounts && accounts.length > 0 && (
            <>
              <div className="cal-set-label">{t("cal.connections.yours")}</div>
              <ul className="cal-set-card cal-accounts">
                {accounts.map((account) => {
                  const status = statusOf(account);
                  return (
                    <li key={account.id} className={`cal-set-row cal-account ${status}`}>
                      <ProviderGlyph provider={providerOf(account)} />
                      <div className="cal-account-main">
                        <b>{account.name}</b>
                        <span className="cal-account-meta">
                          {t(`cal.provider.${providerOf(account)}`)} · <span className={`cal-status ${status}`}>{status === "ok" ? t("cal.sync.ok", { when: relTime(account.last_sync_at) }) : t(`cal.sync.${status}`)}</span>
                        </span>
                        {account.sync_error && <span className="cal-account-error">{account.sync_error}</span>}
                        {whereOf(account) && <span className="cal-account-where">{whereOf(account)}</span>}
                        {!!account.conflicts && account.calendar_id && <AccountConflicts calendarId={account.calendar_id} count={account.conflicts} busy={busy} onResolve={(id, choice) => void resolve(id, choice)} />}
                      </div>
                      <IconButton icon="reload" label={t("cal.sync.now.named", { name: account.name })} disabled={busy === account.id} onClick={() => void sync(account)} />
                      <IconButton icon="unlink" label={t("cal.disconnect.named", { name: account.name })} onClick={() => void disconnect(account)} />
                    </li>
                  );
                })}
              </ul>
            </>
          )}
          <div className="cal-set-label">{t("cal.connect.add")}</div>
          <div className="cal-set-card cal-provider-list">
            {PROVIDERS.map((provider) => (
              <button key={provider} type="button" className="cal-set-row cal-provider-card" onClick={() => setAdding(provider)}>
                <ProviderGlyph provider={provider} />
                <span className="cal-provider-text"><b>{t(`cal.provider.${provider}`)}</b><small>{t(`cal.provider.${provider}.what`)}</small></span>
                <Icon name="forward" size={14} />
              </button>
            ))}
          </div>
        </div>
      )}
    </Sheet>
  );
}

function ConnectForm({ provider, toast, onDone }: { provider: Provider; toast: (text: string) => void; onDone: () => void }) {
  const [copied, copy] = useCopied();
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
      // Yandex and iCloud are CalDAV with a known server, which the host fills in from the provider's name.
      else await api.post("/api/calendar/accounts", { provider, name, remote_calendar_id: remoteId || "primary", credentials: provider === "caldav" ? { server_url: server, username, password } : { username, password } });
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
          <IconButton icon={copied ? "check" : "copy"} size={28} label={t("cal.copy.redirect")} onClick={() => void copy(redirect)} />
        </div>
      )}
      <Field label={t("cal.connect.name")} required value={name} onChange={(e) => setName(e.target.value)} />
      {oauth && <>
        <Field label={t("cal.client.id")} required value={clientId} onChange={(e) => setClientId(e.target.value)} autoComplete="off" />
        <Field label={t("cal.client.secret")} type="password" required value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} autoComplete="off" />
      </>}
      {(provider === "caldav") && <Field label={t("cal.server")} type="url" required value={server} onChange={(e) => setServer(e.target.value)} placeholder="https://" />}
      {!oauth && provider !== "ics" && <>
        <Field label={t("cal.username")} required value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" />
        <Field label={t(provider === "caldav" ? "cal.password" : "cal.app.password")} type="password" required value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
      </>}
      {provider === "ics" && <>
        <Field label={t("cal.ics.url")} type="url" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://" />
        <div className="field-box">
          <span className="field-label" id="cal-ics-color">{t("cal.field.color")}</span>
          <div className="cal-swatches" role="group" aria-labelledby="cal-ics-color">{PALETTE.map((p) => <button key={p.id} type="button" className={`cal-swatch ${color === p.hex ? "on" : ""}`} style={{ "--c": p.hex } as CSSProperties} aria-pressed={color === p.hex} onClick={() => setColor(p.hex)} aria-label={t(`cal.color.${p.id}`)} title={t(`cal.color.${p.id}`)}>{color === p.hex && <Icon name="check" size={12} />}</button>)}</div>
        </div>
      </>}
      {provider !== "ics" && <Field label={t("cal.remote.id")} value={remoteId} onChange={(e) => setRemoteId(e.target.value)} hint={t("cal.remote.hint")} />}
      <div className="sheet-foot cal-foot">
        <span className="cal-spacer" />
        <Button variant="primary" type="submit" busy={busy}>{oauth ? t("cal.connect.signin") : provider === "ics" ? t("cal.subscribe") : t("cal.connect")}</Button>
      </div>
    </form>
  );
}
