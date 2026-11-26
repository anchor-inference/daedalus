// Several accounts per subscription (Claude, Codex, Grok, the prepaid plans), on the models page.
//
// An account is a name and a place in the order here; its login or key stays with the key proxy and
// never comes back to the app. A CLI login signs in through the CLI itself, in a terminal on the
// operator's machine pointed at the account's own folder; a plan's account is a second key. Each
// subscription has a default account, the one a model uses unless it names its own, and a switch for
// carrying a call over to the next account when one is refused for its limit.
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { relTimeLong } from "./format";
import { t } from "./i18n";
import { Icon } from "./icons";
import { navigate, pathFor } from "./router";
import { Row, SetBlock, SetFoot, SetGroup } from "./settingsrow";
import { errorText } from "./ui";
import { Badge, Button, Dot, Field, IconButton, Select, Switch, confirmDialog } from "./ui/primitives";
import "./accounts.css";

export type AccountWindow = { name: string; used_percent: number; resets_at?: number | string | null };
export type SubscriptionAccount = {
  upstream: string;
  id: string;
  label: string;
  position: number;
  default: boolean;
  configured: boolean;
  email: string;
  plan: string;
  resting: boolean;
  resting_until: string | null;
  rest_reason: string;
  usage: { windows: AccountWindow[]; limit_reached: boolean; error: string } | null;
  calls_24h: number;
  last_call_at: string | null;
  sign_in_command: string;
};
export type SubscriptionUpstream = {
  upstream: string;
  providers: string[];
  sign_in: "terminal" | "key";
  default_account: string;
  fallover: boolean;
  accounts: SubscriptionAccount[];
};
export type SubscriptionAccounts = {
  proxy: { reachable: boolean; state: string; error: string };
  terminal: boolean;
  upstreams: SubscriptionUpstream[];
};

const DEFAULT_ACCOUNT = "default";
const ACCOUNTS_KEY = "/api/subscription-accounts";
/** Product names, the same in every language. */
const UPSTREAM_TITLES: Record<string, string> = {
  claude: "Claude",
  codex: "Codex (ChatGPT)",
  grok: "Grok",
  antigravity: "Google (Antigravity)",
  opencode: "OpenCode Go",
  zai_coding: "Z.AI Coding Plan",
  minimax_plan: "MiniMax Token Plan",
  kimi_coding: "Kimi Code",
};

export function upstreamTitle(upstream: string): string {
  return UPSTREAM_TITLES[upstream] ?? upstream;
}

/** The name an account goes by: the operator's, else the default login's own words, else its id. */
export function accountName(account: Pick<SubscriptionAccount, "id" | "label">): string {
  return account.label || (account.id === DEFAULT_ACCOUNT ? t("accounts.default.name") : account.id);
}

/** The accounts of every subscription; `usage: false` skips the quota reading, for lists that only need names. */
export function useSubscriptionAccounts(usage = true) {
  const [data, setData] = useState<SubscriptionAccounts | null>(null);
  const load = useCallback(() => {
    api.get<SubscriptionAccounts>(`${ACCOUNTS_KEY}${usage ? "" : "?usage=false"}`).then(setData).catch(() => setData(null));
  }, [usage]);
  useEffect(load, [load]);
  return { data, load };
}

/** One usage window as a meter: its name and share in words, and the share again as a bar that turns
 *  amber near the limit and red at it, so a spent account is seen before it is read. */
function UsageWindow({ window }: { window: AccountWindow }) {
  const used = Math.max(0, Math.min(100, Math.round(window.used_percent)));
  return (
    <span className="account-window" data-tone={used >= 100 ? "bad" : used >= 80 ? "warn" : undefined}>
      <span>{t("accounts.window", { name: window.name, n: used })}</span>
      <span className="account-meter" aria-hidden="true"><i style={{ width: `${used}%` }} /></span>
    </span>
  );
}

export function statusOf(account: SubscriptionAccount): { tone: string; text: string } {
  if (!account.configured) return { tone: "failed", text: t("accounts.status.off") };
  if (account.resting) {
    const reason = t(`accounts.reason.${account.rest_reason}`);
    return { tone: "warn", text: account.resting_until ? t("accounts.status.resting.until", { reason, when: relTimeLong(account.resting_until) }) : t("accounts.status.resting", { reason }) };
  }
  if (account.usage?.limit_reached) return { tone: "warn", text: t("accounts.status.limit") };
  return { tone: "ok", text: t("accounts.status.on") };
}

function AccountRow({ upstream, account, order, first, last, busy, onRename, onMove, onSignIn, onKey, onRemove }: {
  upstream: SubscriptionUpstream;
  account: SubscriptionAccount;
  /** Its place in the order calls fall over in, from 1. */
  order: number;
  first: boolean;
  last: boolean;
  busy: boolean;
  onRename: (label: string) => void;
  onMove: (step: -1 | 1) => void;
  onSignIn: () => void;
  onKey: (key: string) => void;
  onRemove: () => void;
}) {
  const [label, setLabel] = useState(account.label);
  const [key, setKey] = useState("");
  // The name, the key and Remove are rarely wanted, so they open under the row instead of sitting in
  // every row of the list.
  const [editing, setEditing] = useState(false);
  useEffect(() => setLabel(account.label), [account.label]);
  const status = statusOf(account);
  const who = [account.email, account.plan].filter(Boolean).join(" · ");
  const keyed = upstream.sign_in === "key";
  const editable = keyed && account.id !== DEFAULT_ACCOUNT;
  return (
    <div className="account-row" data-account={account.id} data-upstream={upstream.upstream} data-default={account.default ? "" : undefined}>
      <div className="account-line">
        <span className="account-order num" aria-hidden="true">{order}</span>
        <div className="account-who">
          <span className="account-name">
            <span className="truncate">{accountName(account)}</span>
            {account.default && <Badge tone="accent">{t("accounts.default")}</Badge>}
          </span>
          <span className="account-meta">{who || t(account.configured ? "accounts.who.unknown" : "accounts.who.none")}</span>
        </div>
        <span className="account-status" data-account-status={status.tone}>
          <Dot tone={status.tone === "ok" ? "live" : "attention"} />
          <span>{status.text}</span>
        </span>
        <div className="account-end">
          {!keyed && <Button size="sm" variant={account.configured ? "default" : "primary"} data-action="signin" disabled={busy} onClick={onSignIn}>{t(account.configured ? "accounts.signin.again" : "accounts.signin")}</Button>}
          <IconButton icon="up" size={28} label={t("accounts.up")} disabled={busy || first} onClick={() => onMove(-1)} />
          <IconButton icon="down" size={28} label={t("accounts.down")} disabled={busy || last} onClick={() => onMove(1)} />
          <IconButton icon="pen" size={28} label={t("accounts.edit")} pressed={editing} onClick={() => setEditing((open) => !open)} />
        </div>
      </div>
      <div className="account-facts">
        {account.usage?.windows.length ? <span className="account-windows" data-account-usage="">{account.usage.windows.map((w) => <UsageWindow key={w.name} window={w} />)}</span> : null}
        {account.usage?.error ? <span>{t("accounts.usage.error")}</span> : null}
        <span>{t("accounts.calls", { n: account.calls_24h })}{account.last_call_at ? ` · ${t("accounts.lastcall", { when: relTimeLong(account.last_call_at) })}` : ""}</span>
      </div>
      {!keyed && !account.configured && account.sign_in_command && <div className="mono account-command">{account.sign_in_command}</div>}
      {editing && (
        <div className="account-edit">
          <Field
            label={t("accounts.label")}
            value={label}
            maxLength={60}
            placeholder={account.id === DEFAULT_ACCOUNT ? t("accounts.default.name") : t("accounts.label.placeholder")}
            onChange={(e) => setLabel(e.target.value)}
            onBlur={() => label.trim() !== account.label && onRename(label.trim())}
          />
          {editable && (
            <div className="account-key">
              <Field label={t("accounts.key.replace")} type="password" autoComplete="off" value={key} placeholder={t("accounts.key.placeholder")} onChange={(e) => setKey(e.target.value)} />
              <Button size="sm" disabled={busy || !key.trim()} onClick={() => { onKey(key.trim()); setKey(""); }}>{t("common.save")}</Button>
            </div>
          )}
          {account.id !== DEFAULT_ACCOUNT && (
            <div className="account-edit-foot">
              <Button size="sm" variant="danger" icon="trash" data-action="remove" disabled={busy} onClick={onRemove}>{t("accounts.remove")}</Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function UpstreamBlock({ row, terminal, toast, reload }: { row: SubscriptionUpstream; terminal: boolean; toast: (text: string) => void; reload: () => void }) {
  const [busy, setBusy] = useState(false);
  const [label, setLabel] = useState("");
  const [key, setKey] = useState("");
  const base = `${ACCOUNTS_KEY}/${encodeURIComponent(row.upstream)}`;
  const act = async (work: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await work();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
      reload();
    }
  };
  const signIn = (id: string) => act(async () => {
    const answer = await api.post<{ terminal_id: string }>(`${base}/${encodeURIComponent(id)}/login`, {});
    if (answer?.terminal_id) navigate(pathFor("terminals", answer.terminal_id));
  });
  const add = () => act(async () => {
    const created = await api.post<SubscriptionAccount>(base, { label: label.trim(), ...(row.sign_in === "key" ? { key: key.trim() } : {}) });
    setLabel("");
    setKey("");
    // A login account is nothing until it signs in: the terminal opens at once rather than leaving a step behind.
    if (row.sign_in === "terminal" && terminal && created?.id) {
      const answer = await api.post<{ terminal_id: string }>(`${base}/${encodeURIComponent(created.id)}/login`, {});
      if (answer?.terminal_id) navigate(pathFor("terminals", answer.terminal_id));
    }
  });
  const move = (index: number, step: -1 | 1) => act(async () => {
    // Positions are rewritten for the whole list, so two rows at the same position cannot stay tied.
    const order = row.accounts.map((a) => a.id);
    const [moved] = order.splice(index, 1);
    order.splice(index + step, 0, moved);
    for (const [position, id] of order.entries()) await api.patch(`${base}/${encodeURIComponent(id)}`, { position });
  });
  const remove = (account: SubscriptionAccount) => act(async () => {
    if (!(await confirmDialog({ title: t("accounts.remove.title", { name: accountName(account) }), body: t("accounts.remove.body"), action: t("accounts.remove"), danger: true }))) return;
    await api.delete(`${base}/${encodeURIComponent(account.id)}`);
  });
  const several = row.accounts.length > 1;
  return (
    <SetGroup title={upstreamTitle(row.upstream)} note={t("accounts.providers", { list: row.providers.join(", ") })} data-account-upstream={row.upstream}>
      {several && (
        <>
          <Row title={t("accounts.default.title")} desc={t("accounts.default.sub")}>
            <Select
              id={`accounts-default-${row.upstream}`}
              label={t("accounts.default.title")}
              value={row.default_account}
              disabled={busy}
              onChange={(default_account) => act(() => api.put(base, { default_account }))}
              options={row.accounts.map((a) => ({ id: a.id, label: accountName(a) }))}
            />
          </Row>
          <Row title={t("accounts.fallover")} desc={t("accounts.fallover.sub")} data-setting={`accounts.${row.upstream}.fallover`}>
            <Switch checked={row.fallover} disabled={busy} onChange={(fallover) => act(() => api.put(base, { fallover }))} label={t("accounts.fallover")} />
          </Row>
        </>
      )}
      {row.accounts.map((account, index) => (
        <AccountRow
          key={account.id}
          upstream={row}
          account={account}
          order={index + 1}
          first={index === 0}
          last={index === row.accounts.length - 1}
          busy={busy}
          onRename={(value) => act(() => api.patch(`${base}/${encodeURIComponent(account.id)}`, { label: value }))}
          onMove={(step) => move(index, step)}
          onSignIn={() => signIn(account.id)}
          onKey={(value) => act(() => api.put(`${base}/${encodeURIComponent(account.id)}/key`, { key: value }))}
          onRemove={() => remove(account)}
        />
      ))}
      <SetFoot className="account-add">
        <input className="field" value={label} maxLength={60} placeholder={t("accounts.add.placeholder")} aria-label={t("accounts.label")} onChange={(e) => setLabel(e.target.value)} />
        {row.sign_in === "key" && <input className="field" type="password" autoComplete="off" value={key} placeholder={t("accounts.key.placeholder")} aria-label={t("accounts.key")} onChange={(e) => setKey(e.target.value)} />}
        <Button size="sm" icon="plus" data-action="add" disabled={busy || (row.sign_in === "key" && !key.trim())} onClick={() => void add()}>{t("accounts.add")}</Button>
      </SetFoot>
    </SetGroup>
  );
}

/** Settings → Subscription accounts: every subscription's accounts. A subscription is a group of its
 *  own (its two rules, its accounts in order, the line that adds one), so where one ends and the next
 *  begins is drawn rather than inferred; as one long card the rows of two subscriptions ran together.
 *  The section's name is the page's heading, so it is not said again here.
 *  With no subscription behind any model the section says so rather than leaving the page blank. */
export function SubscriptionAccountsCard({ toast }: { toast: (text: string) => void }) {
  const { data, load } = useSubscriptionAccounts();
  if (!data || !data.upstreams.length) return (
    <div className="accounts" data-setting="accounts">
      <SetGroup><SetBlock><span className="sub">{t(data ? "accounts.none" : "common.loading")}</span></SetBlock></SetGroup>
    </div>
  );
  return (
    <div className="accounts" data-setting="accounts">
      <p className="set-help">{t("accounts.sub")}</p>
      {!data.proxy.reachable && <div className="set-note" data-tone="warn"><Icon name="alert" size={14} /> {t(data.proxy.state === "outdated" ? "accounts.proxy.outdated" : "accounts.proxy.unreachable")}</div>}
      {!data.terminal && <div className="set-note"><Icon name="terminal" size={14} /> {t("accounts.noterminal")}</div>}
      {data.upstreams.map((row) => <UpstreamBlock key={row.upstream} row={row} terminal={data.terminal} toast={toast} reload={load} />)}
      <p className="set-help accounts-foot">{t("accounts.cache")}</p>
    </div>
  );
}
