// The operator's side of a live link: making one, deciding what its holder may do, seeing who uses it,
// and cutting it off.
//
// A live link is not the read-only page above it in the share dialog. Whoever opens it drives this
// conversation's agent with the operator's own powers, and the dialog says that before anything else.
// Whether the link can be opened from outside at all is said just as plainly: a link on this machine's
// loopback address works for nobody else, and handing one out by mistake is the failure to prevent.
//
// What a link allows (its password, models, spending and terminals) is set in a window of its own, as wide
// as the secrets manager: the operator turned down narrow dialogs for this twice, and a role's set of
// presets with dozens of rows grouped by provider does not fit a 560 px column.

import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { useQuery, invalidate } from "./store";
import { t, plural } from "./i18n";
import { relTime, absTime } from "./format";
import { useCopied } from "./ui/components";
import { errorText } from "./ui";
import { Icon } from "./icons";
import { Button, Select, Segmented, Sheet } from "./ui/primitives";
import { useMedia } from "./shell";
import {
  amount, billingsInUse, buildPolicy, fieldSection, formatPct, formatUsd, formOf, freshForm, generatePassword, lockAll, summarize,
  toggleAllowed, toggleIn, type AllowedEntry, type LiveForm, type LiveOptions, type LivePolicy, type RoleForm, type RolePreset, type ShareRole, type Terminals,
} from "./livepolicy";
import "./share.css";

export type LiveLinkVisitor = { name: string; first_seen_at: string; last_seen_at: string };
export type LiveLink = {
  id: string;
  url: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  active: boolean;
  last_seen_at: string | null;
  connections: number;
  visitors: LiveLinkVisitor[];
  label?: string;
  has_password?: boolean;
  policy?: LivePolicy;
  spent?: { usd: number; weekly_pct: number };
};
/** `reach` is who can open a link at all: anyone on the internet, the home network, this machine, or nobody. */
export type LiveLinks = { links: LiveLink[]; reach: "outside" | "network" | "machine" | "none"; base: string };

const EXPIRIES: (number | null)[] = [null, 1, 24, 24 * 7];
/** The Select speaks in words; the host in hours, or null for a link that never expires. */
const expiryId = (hours: number | null) => (hours === null ? "never" : String(hours));
const EXPIRY_HOURS: Record<string, number | null> = { never: null, "1": 1, "24": 24, "168": 168 };
const expiryLabel = (hours: number | null) => t(hours === null ? "live.expiry.never" : hours === 1 ? "live.expiry.hour" : hours === 24 ? "live.expiry.day" : "live.expiry.week");

export function liveLinksKey(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}/live-shares`;
}

export function useLiveLinks(sessionId: string | null): LiveLinks | undefined {
  const data = useQuery<LiveLinks>(sessionId ? liveLinksKey(sessionId) : null, { pollMs: 15000, staleMs: 5000 }).data;
  // A host from before live links answers this route with something else, or not at all: no links then.
  return data && Array.isArray(data.links) ? data : undefined;
}

/** A link's name in a list: the operator's label, else the end of its address, which is what tells two apart. */
function linkName(link: LiveLink): string {
  return link.label?.trim() || t("live.unnamed", { tail: link.id.slice(-4) });
}

/** In the conversation's header while a live link is open: that it is, how many have used it, and when. */
export function LiveLinkChip({ sessionId, onOpen }: { sessionId: string; onOpen: () => void }) {
  const data = useLiveLinks(sessionId);
  const active = data?.links.filter((link) => link.active) ?? [];
  if (!active.length) return null;
  const visitors = active.reduce((sum, link) => sum + link.visitors.length, 0);
  const last = active.map((link) => link.last_seen_at).filter(Boolean).sort().at(-1) ?? null;
  const label = [t("live.chip"), plural("live.visitors", visitors), last ? relTime(last) : ""].filter(Boolean).join(" · ");
  return (
    <button type="button" className="chip bad live-chip" onClick={onOpen} title={t("live.chip.title")} data-live-link>
      <Icon name="share" size={12} /> {label}
    </button>
  );
}

/** The live link's part of the share dialog: the warning, where it reaches, the open links in short, and
 *  the way into the window where a link is made and set. */
export function LiveLinkSection({ sessionId, toast }: { sessionId: string; toast: (text: string) => void }) {
  const data = useLiveLinks(sessionId);
  const [open, setOpen] = useState<string | null>(null);
  const [copied, copyValue] = useCopied();
  const active = data?.links.filter((link) => link.active) ?? [];
  const ended = data?.links.filter((link) => !link.active).slice(0, 3) ?? [];

  return (
    <section className="live-links" aria-labelledby="live-links-title">
      <h3 id="live-links-title" className="live-title">{t("live.title")}</h3>
      <div className="live-warning" role="note"><Icon name="alert" size={14} /> <span>{t("live.warning")}</span></div>
      {data && <Reach reach={data.reach} />}
      {active.map((link) => (
        <div key={link.id} className="live-link" data-live-active>
          <div className="live-link-name">
            <b>{linkName(link)}</b>
            <LockMark on={!!link.has_password} />
          </div>
          <div className="share-field">
            <input className="field mono" readOnly value={link.url} onFocus={(e) => e.target.select()} aria-label={t("live.url")} />
            <Button icon={copied === link.id ? "check" : "copy"} disabled={!link.url} onClick={async () => { if (!(await copyValue(link.url, link.id))) toast(t("svc.copyfail")); }}>{t("common.copy")}</Button>
            <Button icon="sliders" className="live-open-settings" onClick={() => setOpen(link.id)}>{t("live.settings")}</Button>
          </div>
          <div className="sub live-facts">
            <span>{plural("live.visitors", link.visitors.length)}</span>
            <span>{link.last_seen_at ? t("live.lastseen", { when: relTime(link.last_seen_at) }) : t("live.unused")}</span>
            <span>{link.expires_at ? t("live.expires", { when: absTime(link.expires_at) }) : t("live.noexpiry")}</span>
            {link.connections > 0 && <span>{plural("live.connections", link.connections)}</span>}
          </div>
        </div>
      ))}
      <div className="live-create">
        <Button variant="primary" icon="plus" onClick={() => setOpen("new")}>{t(active.length ? "live.create.another" : "live.create")}</Button>
      </div>
      {ended.length > 0 && (
        <div className="sub live-ended">
          {ended.map((link) => (
            <div key={link.id}>{t(link.revoked_at ? "live.ended.revoked" : "live.ended.expired", { when: absTime(link.revoked_at ?? link.expires_at) })} · {plural("live.visitors", link.visitors.length)}</div>
          ))}
        </div>
      )}
      {open && <LiveLinkWindow sessionId={sessionId} start={open} onClose={() => setOpen(null)} toast={toast} />}
    </section>
  );
}

function LockMark({ on }: { on: boolean }) {
  return (
    <span className={`live-lock ${on ? "on" : "off"}`} title={t(on ? "live.pw.has" : "live.pw.none.short")}>
      <Icon name={on ? "lock" : "alert"} size={12} /> {t(on ? "live.pw.has.short" : "live.pw.none.short")}
    </span>
  );
}

function Reach({ reach }: { reach: LiveLinks["reach"] }) {
  const good = reach === "outside";
  return (
    <div className={`live-reach ${good ? "ok" : "warn"}`} data-reach={reach} role="status">
      <Icon name={good ? "check" : "alert"} size={14} />
      <span><b>{t(`live.reach.${reach}`)}</b> {t(`live.reach.${reach}.sub`)}</span>
    </div>
  );
}

/** The window: the session's links on the left, the chosen one's settings on the right. `start` is a
 *  link's id or "new". */
export function LiveLinkWindow({ sessionId, start, onClose, toast }: { sessionId: string; start: string; onClose: () => void; toast: (text: string) => void }) {
  const phone = !useMedia("(min-width: 1024px)");
  const data = useLiveLinks(sessionId);
  const key = liveLinksKey(sessionId);
  const options = useQuery<LiveOptions>(`${key}/options`, { staleMs: 30000 }).data;
  const [chosen, setChosen] = useState(start);
  const links = data?.links.filter((link) => link.active) ?? [];
  const link = chosen === "new" ? null : links.find((item) => item.id === chosen) ?? null;
  const [revoking, setRevoking] = useState(false);
  const [copied, copyValue] = useCopied();

  async function revoke(id: string) {
    setRevoking(true);
    try {
      await api.delete(`${key}/${encodeURIComponent(id)}`);
      invalidate(key);
      toast(t("live.revoked"));
      if (chosen === id) setChosen("new");
    } catch (e) {
      toast(e instanceof ApiError && e.status === 404 ? t("live.gone") : errorText(e));
    } finally {
      setRevoking(false);
    }
  }

  const list = (
    <nav className="live-win-list" aria-label={t("live.win.links")}>
      <div className="live-win-listhead">
        <span className="grow">{t("live.win.links")}</span>
        <Button size="sm" icon="plus" variant={chosen === "new" ? "primary" : undefined} className="live-win-new" onClick={() => setChosen("new")}>{t("live.win.new")}</Button>
      </div>
      {links.length === 0 && <div className="sub live-win-empty">{t("live.win.none")}</div>}
      {links.map((item) => {
        const spentLine = spendLine(item);
        return (
          <div key={item.id} className={`live-win-row ${item.id === chosen ? "on" : ""}`} data-link={item.id}>
            <button type="button" className="live-win-rowmain" aria-current={item.id === chosen ? "true" : undefined} onClick={() => setChosen(item.id)}>
              <span className="live-win-rowname"><span className="truncate">{linkName(item)}</span><LockMark on={!!item.has_password} /></span>
              <span className="sub live-win-rowfacts">
                {plural("live.visitors", item.visitors.length)} · {item.expires_at ? t("live.expires", { when: relTime(item.expires_at) }) : t("live.noexpiry")}
              </span>
              {spentLine && <span className="sub live-win-rowfacts">{spentLine}</span>}
            </button>
            <div className="live-win-rowtools">
              <Button size="sm" variant="quiet" icon={copied === item.id ? "check" : "copy"} disabled={!item.url} onClick={async () => { if (!(await copyValue(item.url, item.id))) toast(t("svc.copyfail")); }}>{t("common.copy")}</Button>
              <Button size="sm" variant="quiet" className="live-win-revoke" disabled={revoking} onClick={() => void revoke(item.id)}>{t("live.revoke")}</Button>
            </div>
          </div>
        );
      })}
      {data && <Reach reach={data.reach} />}
    </nav>
  );

  return (
    <Sheet title={t("live.win.title")} onClose={onClose} size={phone ? "full" : "xl"} className={`live-sheet ${phone ? "ph-sheet" : ""}`}>
      <div className={`live-win ${phone ? "phone" : ""}`} data-live-window>
        {list}
        {options
          ? <LinkPane key={link?.id ?? "new"} sessionId={sessionId} link={link} options={options} phone={phone} toast={toast} onCreated={(id) => setChosen(id)} />
          : <div className="live-win-pane"><div className="sub live-win-loading">{t("common.loading")}</div></div>}
      </div>
    </Sheet>
  );
}

function spendLine(link: LiveLink): string {
  const usd = link.policy?.budget_usd;
  const pct = link.policy?.budget_weekly_pct;
  const parts = [
    usd != null ? t("live.spent.usd.short", { spent: formatUsd(link.spent?.usd ?? 0), limit: formatUsd(usd) }) : "",
    pct != null ? t("live.spent.pct.short", { spent: formatPct(link.spent?.weekly_pct ?? 0), limit: formatPct(pct) }) : "",
  ].filter(Boolean);
  return parts.join(" · ");
}

type Password = { mode: "keep" | "set" | "clear"; value: string; shown: boolean };

/** The chosen link's settings, or a new link's. Keyed by the link, so switching rows starts afresh. */
function LinkPane({ sessionId, link, options, phone, toast, onCreated }: { sessionId: string; link: LiveLink | null; options: LiveOptions; phone: boolean; toast: (text: string) => void; onCreated: (id: string) => void }) {
  const key = liveLinksKey(sessionId);
  const [label, setLabel] = useState(link?.label ?? "");
  const [expiry, setExpiry] = useState<string>(link ? "keep" : expiryId(24));
  const [password, setPassword] = useState<Password>({ mode: link?.has_password ? "keep" : "set", value: "", shown: false });
  const [form, setForm] = useState<LiveForm>(() => (link ? formOf(link.policy, options) : freshForm(options)));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  // Said beside the button until the next change, rather than in a toast that would cover the composer.
  const [saved, setSaved] = useState(false);
  const [copied, copyValue] = useCopied();
  const body = useRef<HTMLDivElement>(null);
  useEffect(() => setSaved(false), [label, expiry, password, form]);

  const willHavePassword = password.mode === "keep" ? !!link?.has_password : password.mode === "set" && !!password.value;
  const sentence = useMemo(() => summarize(form, options, willHavePassword), [form, options, willHavePassword]);
  const usd = amount(form.budgetUsd);
  const pct = amount(form.budgetPct);
  const badAmount = Number.isNaN(usd) || Number.isNaN(pct);

  const setRole = (id: string, next: RoleForm) => setForm((was) => ({ ...was, roles: { ...was.roles, [id]: next } }));

  async function save() {
    setErrors({});
    setBusy(true);
    const policy = buildPolicy(form, options);
    try {
      if (!link) {
        const sent: Record<string, unknown> = { expires_hours: EXPIRY_HOURS[expiry] ?? null, label: label.trim(), policy };
        if (password.mode === "set" && password.value) sent.password = password.value;
        const made = await api.post<LiveLink>(key, sent);
        invalidate(key);
        // No toast: the link appears chosen in the list with its address on top, which is the news.
        if (made?.id) onCreated(made.id);
      } else {
        const sent: Record<string, unknown> = { label: label.trim(), policy };
        if (password.mode === "set" && password.value) sent.password = password.value;
        if (password.mode === "clear") sent.clear_password = true;
        if (expiry !== "keep") sent.expires_hours = EXPIRY_HOURS[expiry] ?? null;
        await api.patch(`${key}/${encodeURIComponent(link.id)}`, sent);
        invalidate(key);
        setPassword({ mode: willHavePassword ? "keep" : "set", value: "", shown: false });
        setSaved(true);
      }
    } catch (e) {
      // The host names the field it refused (`password`, `policy.roles.main`, `policy.budget_usd`):
      // the message goes under that field, and the window scrolls to its section.
      const field = e instanceof ApiError && typeof e.data.field === "string" ? e.data.field : "";
      if (field) {
        setErrors({ [field]: errorText(e) });
        const section = fieldSection(field);
        body.current?.querySelector(`[data-section="${section}"]`)?.scrollIntoView({ block: "start", behavior: "smooth" });
      } else toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const errorAt = (prefix: string) => Object.entries(errors).find(([field]) => field === prefix || field.startsWith(`${prefix}.`))?.[1];

  return (
    <div className="live-win-pane">
      <div ref={body} className="live-win-body">
        {!link && <div className="live-warning" role="note"><Icon name="alert" size={14} /> <span>{t("live.warning")}</span></div>}
        {link?.url && (
          <div className="live-win-url">
            <input className="field mono" readOnly value={link.url} onFocus={(e) => e.target.select()} aria-label={t("live.url")} />
            <Button icon={copied === "url" ? "check" : "copy"} onClick={async () => { if (!(await copyValue(link.url, "url"))) toast(t("svc.copyfail")); }}>{t("common.copy")}</Button>
          </div>
        )}

        <section className="live-sec" data-section="access" aria-labelledby="live-sec-access">
          <h4 id="live-sec-access" className="live-sec-title">{t("live.sec.access")}</h4>
          <div className="live-grid2">
            <div className="field-box">
              <label className="field-label" htmlFor="live-label">{t("live.label")}</label>
              <input id="live-label" className="field" value={label} maxLength={80} placeholder={t("live.label.placeholder")} onChange={(e) => setLabel(e.target.value)} aria-invalid={errorAt("label") ? true : undefined} />
              {errorAt("label") ? <div className="field-error">{errorAt("label")}</div> : <div className="sub field-hint">{t("live.label.hint")}</div>}
            </div>
            <div className="field-box">
              <label className="field-label" htmlFor="live-expiry">{t("live.expiry")}</label>
              <Select id="live-expiry" size="md" label={t("live.expiry")} value={expiry} onChange={setExpiry} invalid={!!errorAt("expires_hours")}
                options={[...(link ? [{ id: "keep", label: link.expires_at ? t("live.expiry.keep", { when: absTime(link.expires_at) }) : t("live.expiry.keep.never") }] : []),
                  ...EXPIRIES.map((hours) => ({ id: expiryId(hours), label: link ? t("live.expiry.from.now", { when: expiryLabel(hours) }) : expiryLabel(hours) }))]} />
              {errorAt("expires_hours") && <div className="field-error">{errorAt("expires_hours")}</div>}
            </div>
          </div>
          <PasswordBlock link={link} value={password} onChange={setPassword} error={errorAt("password") ?? errorAt("clear_password")} />
        </section>

        <section className="live-sec" data-section="models" aria-labelledby="live-sec-models">
          <div className="live-sec-head">
            <h4 id="live-sec-models" className="live-sec-title grow">{t("live.sec.models")}</h4>
            <Button size="sm" icon="lock" className="live-lockall" onClick={() => setForm((was) => lockAll(was, options))}>{t("live.lockall")}</Button>
          </div>
          <p className="sub live-sec-sub">{t("live.models.sub")}</p>
          {errorAt("policy.roles") && <div className="field-error" role="alert">{errorAt("policy.roles")}</div>}
          <div className="live-roles">
            {options.roles.map((role) => form.roles[role.id] && (
              <RoleRow key={role.id} role={role} value={form.roles[role.id]} onChange={(next) => setRole(role.id, next)} error={errorAt(`policy.roles.${role.id}`)} phone={phone} />
            ))}
          </div>
        </section>

        <LimitsBlock form={form} options={options} link={link} onChange={(patch) => setForm((was) => ({ ...was, ...patch }))}
          errorUsd={errorAt("policy.budget_usd")} errorPct={errorAt("policy.budget_weekly_pct")} />

        <section className="live-sec" data-section="terminals" aria-labelledby="live-sec-term">
          <h4 id="live-sec-term" className="live-sec-title">{t("live.sec.terminals")}</h4>
          <p className="sub live-sec-sub">{t("live.term.sub")}</p>
          <div className="access-options live-term" role="radiogroup" aria-label={t("live.sec.terminals")}>
            {options.terminals.map((item) => (
              <label key={item} className={`access-option ${form.terminals === item ? "on" : ""}`} data-terminals={item}>
                <input type="radio" name="live-terminals" checked={form.terminals === item} onChange={() => setForm((was) => ({ ...was, terminals: item as Terminals }))} />
                <span>
                  <b>{t(`live.term.${item}`)}</b>
                  <span className="sub">{t(`live.term.${item}.sub`, { place: t(`live.place.${options.place}`) })}</span>
                </span>
              </label>
            ))}
          </div>
          {errorAt("policy.terminals") && <div className="field-error">{errorAt("policy.terminals")}</div>}
        </section>

        <WithheldBlock items={options.withheld ?? []} />
      </div>
      <footer className="live-win-foot">
        <div className={`live-summary ${willHavePassword ? "" : "open"}`} data-live-summary role="status">
          <Icon name={willHavePassword ? "shield" : "alert"} size={16} />
          <span>{sentence}</span>
        </div>
        {saved && <span className="sub live-saved" role="status"><Icon name="check" size={14} /> {t("live.saved")}</span>}
        <Button variant="primary" className="live-save" busy={busy} disabled={busy || badAmount || (password.mode === "set" && !!link?.has_password && !password.value)} onClick={() => void save()}>
          {t(link ? "common.save" : "live.create")}
        </Button>
      </footer>
    </div>
  );
}

function PasswordBlock({ link, value, onChange, error }: { link: LiveLink | null; value: Password; onChange: (next: Password) => void; error?: string }) {
  const kept = !!link?.has_password;
  const editing = value.mode === "set";
  const none = value.mode === "clear" || (!kept && !value.value);
  return (
    <div className={`live-pw ${none ? "none" : "set"}`} data-password={none ? "none" : "set"}>
      <div className="live-pw-head">
        <Icon name={none ? "alert" : "lock"} size={16} />
        <b className="grow">{t(none ? "live.pw.none" : kept && value.mode === "keep" ? "live.pw.has" : "live.pw.will")}</b>
        {kept && value.mode === "keep" && (
          <>
            <Button size="sm" className="live-pw-change" onClick={() => onChange({ mode: "set", value: "", shown: false })}>{t("live.pw.change")}</Button>
            <Button size="sm" variant="quiet" className="live-pw-remove" onClick={() => onChange({ mode: "clear", value: "", shown: false })}>{t("live.pw.remove")}</Button>
          </>
        )}
        {kept && value.mode !== "keep" && <Button size="sm" variant="quiet" onClick={() => onChange({ mode: "keep", value: "", shown: false })}>{t("live.pw.keep")}</Button>}
      </div>
      <p className="sub live-pw-sub">{t(none ? "live.pw.none.sub" : "live.pw.apart")}</p>
      {editing && (
        <div className="live-pw-field">
          <input id="live-password" className="field mono" type={value.shown ? "text" : "password"} autoComplete="new-password" value={value.value} maxLength={128}
            placeholder={t(kept ? "live.pw.new.placeholder" : "live.pw.placeholder")} aria-label={t("live.pw.label")} aria-invalid={error ? true : undefined}
            onChange={(e) => onChange({ ...value, value: e.target.value })} />
          <Button icon={value.shown ? "eyeoff" : "eye"} className="live-pw-show" aria-label={t(value.shown ? "live.pw.hide" : "live.pw.show")} title={t(value.shown ? "live.pw.hide" : "live.pw.show")}
            onClick={() => onChange({ ...value, shown: !value.shown })} />
          <Button icon="key" className="live-pw-gen" onClick={() => onChange({ ...value, value: generatePassword(), shown: true })}>{t("live.pw.generate")}</Button>
        </div>
      )}
      {error && <div className="field-error">{error}</div>}
    </div>
  );
}

const billingWord = (preset: RolePreset | undefined) => (preset ? t(`live.bill.${preset.billing}`) : "");

function presetOption(preset: RolePreset) {
  return { id: preset.id, label: `${preset.provider_name} · ${preset.label}`, hint: `${preset.model} · ${billingWord(preset)}` };
}

/** One role: what it is for, the value its turns use, and what the visitor may switch to. */
function RoleRow({ role, value, onChange, error, phone }: { role: ShareRole; value: RoleForm; onChange: (next: RoleForm) => void; error?: string; phone: boolean }) {
  const preset = role.presets.find((item) => item.id === value.preset);
  const sorted = useMemo(() => [...role.presets].sort((a, b) => a.provider_name.localeCompare(b.provider_name) || a.label.localeCompare(b.label)), [role.presets]);
  const known = t(`live.role.${role.id}`);
  const name = known.startsWith("[") ? role.label : known;
  const what = t(`live.role.${role.id}.sub`);
  const isCurrent = value.preset === role.current.preset && value.effort === role.current.effort && value.account === role.current.account;
  return (
    <div className={`live-role ${error ? "bad" : ""}`} data-role={role.id}>
      <div className="live-role-text">
        <b>{name}</b>
        {!what.startsWith("[") && <span className="sub">{what}</span>}
      </div>
      <div className="live-role-value">
        <Select size="md" className="live-role-preset" label={t("live.role.model", { role: name })} value={value.preset} options={sorted.map(presetOption)}
          onChange={(id) => { const next = role.presets.find((item) => item.id === id); onChange({ ...value, preset: id, effort: next?.efforts.includes(value.effort) ? value.effort : "", account: "" }); }} />
        {preset && preset.efforts.length > 0 && (
          <Select size="md" className="live-role-effort" label={t("live.role.effort", { role: name })} value={value.effort || "_"}
            options={[{ id: "_", label: t("live.effort.default") }, ...preset.efforts.map((id) => ({ id, label: effortLabel(id) }))]}
            onChange={(id) => onChange({ ...value, effort: id === "_" ? "" : id })} />
        )}
        {preset && preset.accounts.length > 1 && (
          <Select size="md" className="live-role-account" label={t("live.role.account", { role: name })} value={value.account || "_"}
            options={[{ id: "_", label: t("live.account.default") }, ...preset.accounts.map((a) => ({ id: a.id, label: a.name }))]}
            onChange={(id) => onChange({ ...value, account: id === "_" ? "" : id })} />
        )}
        <span className={`live-bill ${preset?.billing ?? ""}`}>{billingWord(preset)}</span>
        {isCurrent && <span className="sub live-role-current">{t("live.role.iscurrent")}</span>}
      </div>
      <div className="live-role-choice">
        {role.pickable ? (
          <>
            <span className="sub live-role-choice-label">{t("live.choose.label")}</span>
            <Segmented size="sm" label={t("live.choose.label")} value={value.mode} className="live-role-mode"
              options={[{ id: "locked", label: t("live.choose.locked") }, { id: "set", label: value.mode === "set" && value.allowed.length ? t("live.choose.set.n", { n: value.allowed.length }) : t("live.choose.set") }]}
              onChange={(mode) => onChange({ ...value, mode })} />
          </>
        ) : <span className="sub live-role-fixed">{t("live.choose.none")}</span>}
      </div>
      {role.pickable && value.mode === "set" && (
        <AllowedPicker role={role} value={value.preset} allowed={value.allowed} onChange={(allowed) => onChange({ ...value, allowed })} phone={phone} />
      )}
      {error && <div className="field-error live-role-error">{error}</div>}
    </div>
  );
}

function effortLabel(id: string): string {
  const known = t(`add.effort.${id}`);
  return known.startsWith("[") ? id : known;
}

/** A role's set, in the model picker's shape: the providers by name in a column with how many of each
 *  are chosen and a box that takes a whole provider, the chosen provider's presets beside it, and a
 *  search over all of them that lists matches from every provider at once. A chosen preset opens the
 *  efforts and accounts it may use. */
function AllowedPicker({ role, value, allowed, onChange, phone }: { role: ShareRole; value: string; allowed: AllowedEntry[]; onChange: (next: AllowedEntry[]) => void; phone: boolean }) {
  const [query, setQuery] = useState("");
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const providers = useMemo(() => {
    const byProvider = new Map<string, { name: string; presets: RolePreset[] }>();
    for (const preset of role.presets) {
      const group = byProvider.get(preset.provider) ?? { name: preset.provider_name, presets: [] };
      group.presets.push(preset);
      byProvider.set(preset.provider, group);
    }
    return [...byProvider.entries()].sort((a, b) => a[1].name.localeCompare(b[1].name));
  }, [role.presets]);
  const [shown, setShown] = useState(() => role.presets.find((item) => item.id === value)?.provider ?? providers[0]?.[0] ?? "");
  const chosen = new Set(allowed.map((entry) => entry.preset));
  const matches = (preset: RolePreset) => words.every((word) => `${preset.label} ${preset.model} ${preset.provider_name}`.toLowerCase().includes(word));
  const searching = words.length > 0;
  const rows = searching ? role.presets.filter(matches) : providers.find(([id]) => id === shown)?.[1].presets ?? [];
  const entry = (id: string) => allowed.find((item) => item.preset === id);
  const setEntry = (id: string, patch: Partial<AllowedEntry>) => onChange(allowed.map((item) => (item.preset === id ? { ...item, ...patch } : item)));
  const setMany = (presets: RolePreset[], on: boolean) => {
    let next = allowed;
    for (const preset of presets) if (preset.id !== value) next = toggleAllowed(next, preset.id, on);
    onChange(next);
  };
  const shownGroup = providers.find(([id]) => id === shown)?.[1];
  const shownAll = !!shownGroup && shownGroup.presets.every((preset) => chosen.has(preset.id) || preset.id === value);

  return (
    <div className="live-allowed" data-allowed={role.id}>
      <div className="live-allowed-tools">
        <label className="live-allowed-search">
          <Icon name="search" size={14} />
          <input type="search" className="field" value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("live.allowed.search")} aria-label={t("live.allowed.search")} />
        </label>
        <span className="sub live-allowed-count">{t("live.allowed.count", { n: allowed.length, total: role.presets.length })}</span>
        {allowed.length > 0 && <Button size="sm" variant="quiet" onClick={() => onChange([])}>{t("live.allowed.clear")}</Button>}
      </div>
      <div className={`live-allowed-split ${phone ? "phone" : ""}`}>
        <div className="live-allowed-provs" role="tablist" aria-label={t("composer.model.providers")}>
          {providers.map(([id, group]) => {
            const count = group.presets.filter((preset) => chosen.has(preset.id)).length;
            const hits = searching ? group.presets.filter(matches).length : group.presets.length;
            return (
              <button key={id} type="button" role="tab" aria-selected={!searching && id === shown} data-prov={id}
                className={`live-allowed-prov ${!searching && id === shown ? "on" : ""} ${searching && !hits ? "none" : ""}`} onClick={() => { setQuery(""); setShown(id); }}>
                <span className="truncate grow">{group.name}</span>
                <span className={`live-allowed-provcount ${count ? "some" : ""}`}>{count ? `${count}/${group.presets.length}` : group.presets.length}</span>
              </button>
            );
          })}
        </div>
        <div className="live-allowed-models">
          <div className="live-allowed-mhead">
            <b className="grow truncate">{searching ? t("live.allowed.found", { n: rows.length }) : shownGroup?.name}</b>
            {!searching && shownGroup && (
              <label className="live-allowed-provall">
                <input type="checkbox" checked={shownAll} onChange={(e) => setMany(shownGroup.presets, e.target.checked)} />
                <span>{t("live.allowed.all", { provider: shownGroup.name })}</span>
              </label>
            )}
          </div>
          {rows.length === 0 && <div className="sub live-allowed-empty">{t("live.allowed.nothing")}</div>}
          {rows.map((preset) => {
            const own = preset.id === value;
            const on = own || chosen.has(preset.id);
            const kept = entry(preset.id);
            return (
              <div key={preset.id} className={`live-allowed-row ${on ? "on" : ""}`} data-preset={preset.id}>
                <label className="live-allowed-main">
                  <input type="checkbox" checked={on} disabled={own} onChange={(e) => onChange(toggleAllowed(allowed, preset.id, e.target.checked))} aria-label={preset.label} />
                  <span className="live-allowed-name">
                    <span className="truncate">{preset.label}</span>
                    <span className="sub mono truncate">{searching ? `${preset.provider_name} · ${preset.model}` : preset.model}</span>
                  </span>
                  {own && <span className="sub live-allowed-own">{t("live.allowed.own")}</span>}
                  <span className={`live-bill ${preset.billing}`}>{billingWord(preset)}</span>
                </label>
                {kept && (preset.efforts.length > 1 || preset.accounts.length > 1) && (
                  <div className="live-allowed-subs">
                    {preset.efforts.length > 1 && (
                      <Chips label={t("live.allowed.efforts")} anyLabel={t("live.allowed.any")} values={preset.efforts.map((id) => ({ id, label: effortLabel(id) }))} chosen={kept.efforts}
                        onToggle={(id, next) => setEntry(preset.id, { efforts: id === "" ? [] : toggleIn(kept.efforts, id, next) })} />
                    )}
                    {preset.accounts.length > 1 && (
                      <Chips label={t("live.allowed.accounts")} anyLabel={t("live.allowed.any")} values={preset.accounts.map((a) => ({ id: a.id, label: a.name }))} chosen={kept.accounts}
                        onToggle={(id, next) => setEntry(preset.id, { accounts: id === "" ? [] : toggleIn(kept.accounts, id, next) })} />
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/** A row of toggles where nothing pressed means "any": the host reads an empty list that way. */
function Chips({ label, anyLabel, values, chosen, onToggle }: { label: string; anyLabel: string; values: { id: string; label: string }[]; chosen: string[]; onToggle: (id: string, on: boolean) => void }) {
  return (
    <div className="live-chips" role="group" aria-label={label}>
      <span className="sub live-chips-label">{label}</span>
      <button type="button" className={`live-chipbtn ${chosen.length === 0 ? "on" : ""}`} aria-pressed={chosen.length === 0} onClick={() => onToggle("", true)}>{anyLabel}</button>
      {values.map((item) => {
        const on = chosen.includes(item.id);
        return <button key={item.id} type="button" className={`live-chipbtn ${on ? "on" : ""}`} aria-pressed={on} onClick={() => onToggle(item.id, !on)}>{item.label}</button>;
      })}
    </div>
  );
}

/** The two limits. The one that applies to the models the visitor can reach comes first and in full;
 *  the other stays reachable but says that nothing chosen is billed that way. */
function LimitsBlock({ form, options, link, onChange, errorUsd, errorPct }: { form: LiveForm; options: LiveOptions; link: LiveLink | null; onChange: (patch: Partial<LiveForm>) => void; errorUsd?: string; errorPct?: string }) {
  const used = billingsInUse(form, options);
  const order: ("usd" | "pct")[] = used.has("subscription") && !used.has("metered") ? ["pct", "usd"] : ["usd", "pct"];
  const card = (kind: "usd" | "pct") => {
    const relevant = used.has(kind === "usd" ? "metered" : "subscription");
    const text = kind === "usd" ? form.budgetUsd : form.budgetPct;
    const parsed = amount(text);
    const bad = Number.isNaN(parsed);
    const limit = parsed && !bad ? parsed : link ? (kind === "usd" ? link.policy?.budget_usd : link.policy?.budget_weekly_pct) ?? null : null;
    const spent = link ? (kind === "usd" ? link.spent?.usd : link.spent?.weekly_pct) ?? 0 : null;
    const error = (kind === "usd" ? errorUsd : errorPct) ?? (bad ? t("live.limit.bad") : undefined);
    const id = `live-limit-${kind}`;
    return (
      <div key={kind} className={`live-limit ${relevant ? "" : "quiet"}`} data-limit={kind}>
        <label className="field-label" htmlFor={id}>{t(`live.limit.${kind}`)}</label>
        <div className="live-limit-input">
          {kind === "usd" && <span className="live-limit-unit">$</span>}
          <input id={id} className="field" inputMode="decimal" value={text} placeholder={t("live.limit.none")} aria-invalid={error ? true : undefined}
            onChange={(e) => onChange(kind === "usd" ? { budgetUsd: e.target.value } : { budgetPct: e.target.value })} />
          {kind === "pct" && <span className="live-limit-unit">%</span>}
        </div>
        <div className="sub live-limit-sub">{t(relevant ? `live.limit.${kind}.sub` : `live.limit.${kind}.unused`)}</div>
        {spent !== null && (
          <div className="live-spent">
            <div className="live-spent-line sub">{limit ? t("live.spent.of", { spent: kind === "usd" ? formatUsd(spent) : formatPct(spent), limit: kind === "usd" ? formatUsd(limit) : formatPct(limit) }) : t("live.spent.only", { spent: kind === "usd" ? formatUsd(spent) : formatPct(spent) })}</div>
            {limit ? <div className="live-bar" role="progressbar" aria-valuemin={0} aria-valuemax={limit} aria-valuenow={spent}><span style={{ width: `${Math.min(100, (spent / limit) * 100)}%` }} className={spent >= limit ? "full" : spent / limit > 0.8 ? "near" : ""} /></div> : null}
          </div>
        )}
        {error && <div className="field-error">{error}</div>}
      </div>
    );
  };
  return (
    <section className="live-sec" data-section="limits" aria-labelledby="live-sec-limits">
      <h4 id="live-sec-limits" className="live-sec-title">{t("live.sec.limits")}</h4>
      <p className="sub live-sec-sub">{t("live.limit.what")}</p>
      <div className="live-grid2">{order.map(card)}</div>
    </section>
  );
}

/** What the agent cannot reach while any link of the session is live. Read-only on purpose: the host
 *  takes these away for every turn of the conversation, the operator's own included, and a switch here
 *  would suggest the operator could hand them to a visitor. */
function WithheldBlock({ items }: { items: { id: string; label: string; why: string }[] }) {
  if (!items.length) return null;
  return (
    <section className="live-sec" data-section="withheld" aria-labelledby="live-sec-withheld">
      <h4 id="live-sec-withheld" className="live-sec-title">{t("live.withheld.title")}</h4>
      <p className="sub live-sec-sub">{t("live.withheld.sub")}</p>
      <ul className="live-withheld">
        {items.map((item) => {
          const label = t(`live.withheld.${item.id}`);
          const why = t(`live.withheld.${item.id}.why`);
          return (
            <li key={item.id} data-withheld={item.id}>
              <Icon name="lock" size={14} />
              <span><b>{label.startsWith("[") ? item.label : label}</b><span className="sub">{why.startsWith("[") ? item.why : why}</span></span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
