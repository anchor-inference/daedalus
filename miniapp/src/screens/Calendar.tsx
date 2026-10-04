import { useMemo, useState } from "react";
import { api } from "../api";
import { t, useLang } from "../i18n";
import { Icon } from "../icons";
import { useQuery, invalidate } from "../store";
import { Sheet } from "../ui/dialogs";
import { confirmAsync, errorText } from "../ui";
import { PageHeader } from "../ui/index";
import { wallInput, wallInstant } from "./calendarTime";
import "./calendar.css";

type Event = { id: string; account_id: string | null; title: string; description: string; start_at: string; end_at: string; timezone: string; all_day: number; location: string; recurrence: string; dirty: string; version: number };
type Account = { id: string; provider: "google" | "outlook" | "yandex"; name: string; remote_calendar_id: string; last_sync_at: string | null; sync_error: string };
type Draft = { id?: string; version?: number; dirty?: string; title: string; description: string; start_at: string; end_at: string; timezone: string; all_day: boolean; location: string; account_id: string | null };
type Provider = Account["provider"];

function blank(day: Date): Draft {
  const start = new Date(day.getFullYear(), day.getMonth(), day.getDate(), 9);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  return { title: "", description: "", start_at: wallInput(start.toISOString(), timezone), end_at: wallInput(new Date(start.getTime() + 3600000).toISOString(), timezone), timezone, all_day: false, location: "", account_id: null };
}

function monthStart(value: Date) { return new Date(value.getFullYear(), value.getMonth(), 1); }
function dayKey(value: Date) { return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`; }
function nextDate(value: string) { const date = new Date(`${value}T00:00:00Z`); date.setUTCDate(date.getUTCDate() + 1); return date.toISOString().slice(0, 10); }
function previousDate(value: string) { const date = new Date(`${value}T00:00:00Z`); date.setUTCDate(date.getUTCDate() - 1); return date.toISOString().slice(0, 10); }

export function CalendarScreen({ toast }: { toast: (message: string) => void }) {
  const [lang] = useLang();
  const [month, setMonth] = useState(monthStart(new Date()));
  const [draft, setDraft] = useState<Draft | null>(null);
  const [connections, setConnections] = useState(false);
  const [provider, setProvider] = useState<Provider>("google");
  const [name, setName] = useState("");
  const [remoteId, setRemoteId] = useState("primary");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [username, setUsername] = useState("");
  const [appPassword, setAppPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const first = new Date(month.getFullYear(), month.getMonth(), 1 - ((new Date(month.getFullYear(), month.getMonth(), 1).getDay() + 6) % 7));
  const last = new Date(first.getFullYear(), first.getMonth(), first.getDate() + 42);
  const key = `/api/calendar/events?start=${encodeURIComponent(first.toISOString())}&end=${encodeURIComponent(last.toISOString())}`;
  const { data: events, loading, error, refresh } = useQuery<Event[]>(key, { pollMs: 30000, staleMs: 5000 });
  const { data: accounts, refresh: refreshAccounts } = useQuery<Account[]>("/api/calendar/accounts", { pollMs: 30000, staleMs: 5000 });
  const days = useMemo(() => Array.from({ length: 42 }, (_, index) => new Date(first.getFullYear(), first.getMonth(), first.getDate() + index)), [month.getTime()]);
  const byDay = useMemo(() => {
    const result = new Map<string, Event[]>();
    for (const event of events ?? []) {
      if (event.all_day) {
        for (let date = event.start_at.slice(0, 10); date < event.end_at.slice(0, 10); date = nextDate(date)) result.set(date, [...(result.get(date) ?? []), event]);
        continue;
      }
      const start = new Date(event.start_at);
      const end = new Date(event.end_at);
      for (let day = new Date(start.getFullYear(), start.getMonth(), start.getDate()); day < end; day.setDate(day.getDate() + 1)) {
        const key = dayKey(day);
        result.set(key, [...(result.get(key) ?? []), event]);
      }
    }
    return result;
  }, [events]);
  const today = dayKey(new Date());
  const label = new Intl.DateTimeFormat(lang === "ru" ? "ru-RU" : "en-US", { month: "long", year: "numeric" }).format(month);
  const weekdays = Array.from({ length: 7 }, (_, i) => new Intl.DateTimeFormat(lang === "ru" ? "ru-RU" : "en-US", { weekday: "short" }).format(new Date(2024, 0, 1 + i)));

  async function save(event: Draft) {
    setBusy(true);
    try {
      const startAt = event.all_day ? `${event.start_at}T00:00:00Z` : wallInstant(event.start_at, event.timezone);
      const endAt = event.all_day ? `${nextDate(event.end_at)}T00:00:00Z` : wallInstant(event.end_at, event.timezone);
      if (!startAt || !endAt) { toast(t("calendar.invalid.time")); return; }
      const body = { ...event, start_at: startAt, end_at: endAt };
      if (event.id) await api.put(`/api/calendar/events/${event.id}`, body);
      else await api.post("/api/calendar/events", body);
      setDraft(null);
      refresh();
      invalidate("/api/calendar/events");
      toast(t("calendar.saved"));
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  async function remove(event: Draft) {
    if (!event.id || !(await confirmAsync(t("calendar.delete"), { action: t("common.delete") }))) return;
    try {
      await api.delete(`/api/calendar/events/${event.id}?version=${event.version}`);
      setDraft(null); refresh(); invalidate("/api/calendar/events"); toast(t("calendar.deleted"));
    } catch (exc) { toast(errorText(exc)); }
  }

  async function connect() {
    setBusy(true);
    try {
      if (provider !== "yandex") {
        const started = await api.post<{ url: string }>("/api/calendar/oauth/start", { provider, name, client_id: clientId, client_secret: clientSecret, remote_calendar_id: remoteId });
        window.location.assign(started.url);
        return;
      }
      await api.post("/api/calendar/accounts", { provider, name, remote_calendar_id: remoteId, credentials: { username, app_password: appPassword } });
      setClientSecret(""); setAppPassword(""); setConnections(false);
      refreshAccounts(); toast(t("calendar.connected"));
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  async function sync(id: string) {
    setBusy(true);
    try { await api.post(`/api/calendar/accounts/${id}/sync`); refresh(); refreshAccounts(); toast(t("calendar.synced")); }
    catch (exc) { toast(errorText(exc)); refreshAccounts(); }
    finally { setBusy(false); }
  }

  async function disconnect(account: Account) {
    if (!(await confirmAsync(t("calendar.disconnect"), { body: t("calendar.disconnect.body"), action: t("common.delete") }))) return;
    try { await api.delete(`/api/calendar/accounts/${account.id}`); refresh(); refreshAccounts(); }
    catch (exc) { toast(errorText(exc)); }
  }

  async function resolve(eventId: string, choice: "local" | "remote") {
    setBusy(true);
    try {
      await api.post(`/api/calendar/events/${eventId}/resolve`, { choice });
      setDraft(null); refresh(); refreshAccounts(); toast(t("calendar.synced"));
    } catch (exc) { toast(errorText(exc)); }
    finally { setBusy(false); }
  }

  return <div className="screen calendar-screen">
    <PageHeader title={t("nav.calendar")} actions={<>
      <button className="btn" onClick={() => setConnections(true)}><Icon name="plug" size={16} /> {t("calendar.accounts")}</button>
      <button className="btn primary" onClick={() => setDraft(blank(new Date()))}><Icon name="plus" size={16} /> {t("calendar.new")}</button>
    </>} />
    <div className="calendar-toolbar">
      <div className="calendar-month"><button className="iconbtn" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))} aria-label={t("calendar.previous")}><Icon name="back" size={18} /></button><h2>{label}</h2><button className="iconbtn" onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))} aria-label={t("calendar.next")}><Icon name="forward" size={18} /></button></div>
      <button className="btn" onClick={() => setMonth(monthStart(new Date()))}>{t("calendar.today")}</button>
    </div>
    {error && <div className="empty">{error}</div>}
    {loading && !events && <div className="empty">{t("common.loading")}</div>}
    <div className="calendar-grid" role="grid" aria-label={label}>
      {weekdays.map((word) => <div className="calendar-weekday" key={word}>{word}</div>)}
      {days.map((day) => <div className={`calendar-day ${day.getMonth() !== month.getMonth() ? "outside" : ""} ${dayKey(day) === today ? "today" : ""}`} key={dayKey(day)} role="gridcell">
        <button className="calendar-date" onClick={() => setDraft(blank(day))} aria-label={`${t("calendar.new")}: ${day.toLocaleDateString(lang === "ru" ? "ru-RU" : "en-US")}`}>{day.getDate()}</button>
        {(byDay.get(dayKey(day)) ?? []).slice(0, 4).map((event) => <button key={event.id} className={`calendar-event ${event.account_id ? "linked" : ""}`} onClick={() => setDraft({ ...event, start_at: event.all_day ? event.start_at.slice(0, 10) : wallInput(event.start_at, event.timezone), end_at: event.all_day ? previousDate(event.end_at.slice(0, 10)) : wallInput(event.end_at, event.timezone), all_day: !!event.all_day })} title={event.title}>{event.all_day ? "" : new Intl.DateTimeFormat(lang === "ru" ? "ru-RU" : "en-US", { hour: "2-digit", minute: "2-digit" }).format(new Date(event.start_at)) + " "}{event.title}</button>)}
        {(byDay.get(dayKey(day)) ?? []).length > 4 && <span className="calendar-more">+{(byDay.get(dayKey(day)) ?? []).length - 4}</span>}
      </div>)}
    </div>
    {draft && <Sheet title={draft.id ? t("calendar.edit") : t("calendar.new")} onClose={() => setDraft(null)} size="narrow"><form className="calendar-form" onSubmit={(e) => { e.preventDefault(); void save(draft); }}>
      <label>{t("calendar.title")}<input required maxLength={240} value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })} /></label>
      <label>{t("calendar.start")}<input type={draft.all_day ? "date" : "datetime-local"} required value={draft.start_at} onChange={(e) => setDraft({ ...draft, start_at: e.target.value })} /></label>
      <label>{t(draft.all_day ? "calendar.last.day" : "calendar.end")}<input type={draft.all_day ? "date" : "datetime-local"} required value={draft.end_at} min={draft.all_day ? draft.start_at : undefined} onChange={(e) => setDraft({ ...draft, end_at: e.target.value })} /></label>
      <label>{t("calendar.timezone")}<input value={draft.timezone} onChange={(e) => setDraft({ ...draft, timezone: e.target.value })} /></label>
      <label className="calendar-check"><input type="checkbox" checked={draft.all_day} onChange={(e) => setDraft({ ...draft, all_day: e.target.checked, start_at: e.target.checked ? draft.start_at.slice(0, 10) : `${draft.start_at}T09:00`, end_at: e.target.checked ? draft.start_at.slice(0, 10) : `${draft.start_at}T10:00` })} />{t("calendar.all.day")}</label>
      <label>{t("calendar.location")}<input value={draft.location} onChange={(e) => setDraft({ ...draft, location: e.target.value })} /></label>
      <label>{t("calendar.description")}<textarea value={draft.description} onChange={(e) => setDraft({ ...draft, description: e.target.value })} /></label>
      <label>{t("calendar.account")}<select value={draft.account_id ?? ""} disabled={!!draft.id} onChange={(e) => setDraft({ ...draft, account_id: e.target.value || null })}><option value="">{t("calendar.local")}</option>{(accounts ?? []).map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}</select></label>
      {draft.id && draft.dirty && (accounts ?? []).some((account) => account.id === draft.account_id && account.sync_error.includes(draft.id ?? "")) && <div className="calendar-conflict"><strong>{t("calendar.conflict")}</strong><div><button type="button" className="btn" disabled={busy} onClick={() => void resolve(draft.id!, "remote")}>{t("calendar.use.provider")}</button><button type="button" className="btn" disabled={busy} onClick={() => void resolve(draft.id!, "local")}>{t("calendar.use.local")}</button></div></div>}
      <div className="calendar-actions">{draft.id && <button type="button" className="btn danger" onClick={() => void remove(draft)}>{t("common.delete")}</button>}<button className="btn primary" disabled={busy}>{t("common.save")}</button></div>
    </form></Sheet>}
    {connections && <Sheet title={t("calendar.accounts")} onClose={() => setConnections(false)} size="narrow"><div className="calendar-connections">
      {(accounts ?? []).map((account) => <div className="calendar-account" key={account.id}><div><strong>{account.name}</strong><small>{account.provider} · {account.last_sync_at ? new Date(account.last_sync_at).toLocaleString() : t("calendar.never.synced")}</small>{account.sync_error && <small className="error">{account.sync_error}</small>}{/^event ([a-f0-9]+) changed/.test(account.sync_error) && <span className="calendar-conflict-actions"><button className="btn" disabled={busy} onClick={() => void resolve(account.sync_error.split(" ")[1], "remote")}>{t("calendar.use.provider")}</button><button className="btn" disabled={busy} onClick={() => void resolve(account.sync_error.split(" ")[1], "local")}>{t("calendar.use.local")}</button></span>}</div><button className="iconbtn" onClick={() => void sync(account.id)} disabled={busy} aria-label={t("calendar.sync")}><Icon name="reload" size={16} /></button><button className="iconbtn" onClick={() => void disconnect(account)} aria-label={t("calendar.disconnect")}><Icon name="trash" size={16} /></button></div>)}
      <form className="calendar-form" onSubmit={(e) => { e.preventDefault(); void connect(); }}><h3>{t("calendar.connect")}</h3>
        <label>{t("calendar.provider")}<select value={provider} onChange={(e) => setProvider(e.target.value as Provider)}><option value="google">{t("calendar.google")}</option><option value="outlook">{t("calendar.outlook")}</option><option value="yandex">{t("calendar.yandex")}</option></select></label>
        <label>{t("calendar.name")}<input required value={name} onChange={(e) => setName(e.target.value)} /></label>
        {provider === "yandex" ? <><label>{t("calendar.username")}<input required value={username} onChange={(e) => setUsername(e.target.value)} /></label><label>{t("calendar.app.password")}<input type="password" required value={appPassword} onChange={(e) => setAppPassword(e.target.value)} /></label></> : <><label>{t("calendar.client.id")}<input required value={clientId} onChange={(e) => setClientId(e.target.value)} /></label><label>{t("calendar.client.secret")}<input type="password" required value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} /></label><p className="calendar-hint">{t("calendar.redirect")}: <code>{window.location.origin}/api/calendar/oauth/callback</code></p></>}
        <label>{t("calendar.remote.id")}<input value={remoteId} onChange={(e) => setRemoteId(e.target.value)} /></label><p className="calendar-hint">{t(`calendar.hint.${provider}`)}</p><button className="btn primary" disabled={busy}>{t("calendar.connect")}</button>
      </form>
    </div></Sheet>}
  </div>;
}
