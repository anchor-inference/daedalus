// What the key proxy holds, set from the app: every upstream's key, address and header, the
// operator's own upstreams, and the proxy's daily cap. The proxy reads its file again on every call,
// so a value saved here holds from the next request with nothing restarted.
//
// Before this card the search backends (Keenable, Serper, Tavily, Exa, Perplexity) could only be
// keyed by editing keyproxy.env by hand and restarting the stack, and so could any value the
// provider cards did not happen to show. A key is typed here and never comes back: the app only
// learns whether one is set.
import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { relTimeLong } from "./format";
import { t } from "./i18n";
import { confirmAsync, errorText } from "./ui";
import { Badge, Button, IconButton, confirmDialog } from "./ui/primitives";
import { NumInput, Row, SetBlock, SetFoot, SetGroup } from "./settingsrow";

/** One variable of the proxy's file. A secret carries no value, only whether it is set. */
export type KeyproxyVar = { name: string; secret: boolean; set: boolean; source: "file" | "environment" | "default"; value?: string; default?: string };
export type KeyproxyUpstream = { upstream: string; group: "model" | "search" | "extra"; builtin: boolean; shares_key_with: string; key: KeyproxyVar; address: KeyproxyVar; header: KeyproxyVar };
export type KeyproxySettings = {
  reachable: boolean;
  state: "ok" | "none" | "unreachable" | "refused";
  writable?: boolean;
  upstreams?: KeyproxyUpstream[];
  cli_logins?: { upstream: string; configured: boolean }[];
  /** False where a command the agent runs could read the keys saved here: no sandbox hides them. */
  keys_hidden?: boolean;
};

/** The proxy's settings and one way to write a value; `onChanged` lets the page re-read what depends on them. */
export function useKeyproxy(toast: (text: string) => void, onChanged?: () => void) {
  const [data, setData] = useState<KeyproxySettings | null>(null);
  const load = useCallback(() => {
    api.get<KeyproxySettings>("/api/keyproxy").then(setData).catch(() => setData({ reachable: false, state: "unreachable" }));
  }, []);
  useEffect(load, [load]);
  const write = useCallback(async (name: string, value: string): Promise<boolean> => {
    try {
      setData(await api.put<KeyproxySettings>(`/api/keyproxy/${encodeURIComponent(name)}`, { value }));
      onChanged?.();
      return true;
    } catch (e) {
      toast(errorText(e));
      return false;
    }
  }, [toast, onChanged]);
  return { data, write, load };
}

const SEARCH_TITLES: Record<string, string> = { serper: "Serper (Google)", keenable: "Keenable", tavily: "Tavily", exa: "Exa", perplexity: "Perplexity" };
const KEY_LINKS: Record<string, string> = {
  serper: "https://serper.dev",
  keenable: "https://keenable.ai",
  tavily: "https://app.tavily.com",
  exa: "https://dashboard.exa.ai",
  perplexity: "https://www.perplexity.ai/settings/api",
};

function upstreamTitle(row: KeyproxyUpstream): string {
  return SEARCH_TITLES[row.upstream] ?? row.upstream;
}

/** A key field: typed, saved when left or on Enter, never shown again; Forget clears it. */
function KeyInput({ v, label, onSave, disabled }: { v: KeyproxyVar; label: string; onSave: (value: string) => Promise<boolean>; disabled?: boolean }) {
  const [draft, setDraft] = useState("");
  const commit = async () => {
    const value = draft.trim();
    if (value && (await onSave(value))) setDraft("");
  };
  return (
    <div className="row keyproxy-key" style={{ gap: 8, width: "100%" }}>
      <input
        className="field"
        style={{ flex: 1, minWidth: 0 }}
        type="password"
        autoComplete="new-password"
        aria-label={label}
        disabled={disabled}
        placeholder={t(v.set ? "keyproxy.key.stored" : "keyproxy.key.none")}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => void commit()}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
      {v.set && (
        <button className="btn small danger" disabled={disabled} onClick={async () => {
          if (await confirmAsync(t("keyproxy.forget.title"), { body: t("keyproxy.forget.body"), action: t("keyproxy.forget"), danger: true })) void onSave("");
        }}>{t("keyproxy.forget")}</button>
      )}
    </div>
  );
}

/** A plain value — an address or a header name — saved when left; empty puts the default back. */
function ValueInput({ v, label, onSave, disabled }: { v: KeyproxyVar; label: string; onSave: (value: string) => Promise<boolean>; disabled?: boolean }) {
  const [draft, setDraft] = useState(v.value ?? "");
  useEffect(() => setDraft(v.value ?? ""), [v.value]);
  return (
    <input
      className="field"
      aria-label={label}
      disabled={disabled}
      value={draft}
      placeholder={v.default || ""}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={async () => { if (draft.trim() !== (v.value ?? "") && !(await onSave(draft.trim()))) setDraft(v.value ?? ""); }}
      onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
    />
  );
}

function UpstreamRow({ row, write, writable }: { row: KeyproxyUpstream; write: (name: string, value: string) => Promise<boolean>; writable: boolean }) {
  const [open, setOpen] = useState(false);
  const title = upstreamTitle(row);
  const shared = !!row.shares_key_with;
  const ready = row.key.set || (row.group === "extra" && row.address.set);
  const link = KEY_LINKS[row.upstream];
  return (
    <div className={`kp-item ${open ? "open" : ""}`} data-upstream={row.upstream}>
      <Row title={title} desc={<span className="mono">{row.address.value || row.address.default}</span>}>
        {shared
          ? <Badge>{t("keyproxy.shared", { name: row.shares_key_with })}</Badge>
          : <Badge tone={ready ? undefined : "attention"}>{t(row.key.set ? "keyproxy.pill.key" : row.group === "extra" ? "keyproxy.pill.nokeyneeded" : "keyproxy.pill.nokey")}</Badge>}
        <IconButton className="kp-open" icon="chevron" size={28} label={t(open ? "common.close" : "common.edit")} expanded={open} onClick={() => setOpen((o) => !o)} />
      </Row>
      {open && (
        <SetBlock className="kp-edit">
          {!shared && (
            <label className="kp-field">
              <span className="field-label">{t("keyproxy.key")} <code>{row.key.name}</code></span>
              <KeyInput v={row.key} label={`${title}: ${t("keyproxy.key")}`} disabled={!writable} onSave={(value) => write(row.key.name, value)} />
              {row.key.source === "environment" && <span className="sub">{t("keyproxy.fromenv")}</span>}
              {link && <span className="sub">{t("free.key.get")} <a href={link} target="_blank" rel="noopener noreferrer">{title} ↗</a></span>}
            </label>
          )}
          <label className="kp-field">
            <span className="field-label">{t("keyproxy.address")} <code>{row.address.name}</code></span>
            <ValueInput v={row.address} label={`${title}: ${t("keyproxy.address")}`} disabled={!writable} onSave={(value) => write(row.address.name, value)} />
          </label>
          <label className="kp-field">
            <span className="field-label">{t("keyproxy.header")} <code>{row.header.name}</code></span>
            <ValueInput v={{ ...row.header, default: row.header.default || "Authorization: Bearer" }} label={`${title}: ${t("keyproxy.header")}`} disabled={!writable} onSave={(value) => write(row.header.name, value)} />
          </label>
          {!row.builtin && (
            <div className="kp-edit-foot">
              <Button size="sm" variant="danger" icon="trash" disabled={!writable} onClick={async () => {
                if (!(await confirmDialog({ title: t("keyproxy.remove.title", { name: row.upstream }), body: t("keyproxy.remove.body"), action: t("common.remove"), danger: true }))) return;
                if (row.key.set) await write(row.key.name, "");
                if (row.header.set) await write(row.header.name, "");
                await write(row.address.name, "");
              }}>{t("common.remove")}</Button>
            </div>
          )}
        </SetBlock>
      )}
    </div>
  );
}

/** An upstream of the operator's own: a name, an address and, when it takes one, a key. */
function AddUpstream({ write, taken, writable }: { write: (name: string, value: string) => Promise<boolean>; taken: string[]; writable: boolean }) {
  const [name, setName] = useState("");
  const [address, setAddress] = useState("");
  const [key, setKey] = useState("");
  const id = name.trim().toUpperCase().replace(/[^A-Z0-9_]/g, "_");
  const valid = /^[A-Z][A-Z0-9_]*$/.test(id) && !taken.includes(id.toLowerCase()) && /^https?:\/\//.test(address.trim());
  return (
    <SetGroup title={t("keyproxy.add.title")} help={t("keyproxy.add.sub")} data-setting="keyproxy.add">
      <SetBlock className="kp-add">
        <label className="kp-field"><span className="field-label">{t("keyproxy.add.name")}</span><input className="field" value={name} placeholder="myllm" onChange={(e) => setName(e.target.value)} /></label>
        <label className="kp-field"><span className="field-label">{t("keyproxy.address")}</span><input className="field mono" value={address} placeholder="https://<host>/v1" onChange={(e) => setAddress(e.target.value)} /></label>
        <label className="kp-field"><span className="field-label">{t("keyproxy.key")}</span><input className="field" type="password" autoComplete="new-password" value={key} placeholder={t("keyproxy.add.keyoptional")} onChange={(e) => setKey(e.target.value)} /></label>
      </SetBlock>
      <SetFoot>
        <Button size="sm" variant="primary" disabled={!writable || !valid} onClick={async () => {
          if (key.trim() && !(await write(`KEYPROXY_KEY_${id}`, key.trim()))) return;
          if (!(await write(`KEYPROXY_UPSTREAM_${id}`, address.trim()))) return;
          setName(""); setAddress(""); setKey("");
        }}>{t("keyproxy.add.action")}</Button>
      </SetFoot>
    </SetGroup>
  );
}

/** One CLI the key proxy imitates for a subscription, as the proxy reports it. */
export type KeyproxyClient = {
  name: string;
  label: string;
  logged_in: boolean | null;
  version: string;
  source: "default" | "stored" | "installed" | "environment";
  pinned_by: string | null;
  latest: string | null;
  checked_at: number | null;
  check_error: string | null;
  stale: boolean;
  kept_on_update: string[];
  field_labels?: Record<string, string>;
  previous_version: string | null;
  pending: boolean;
  reverted: { from: string; to?: string; at: number; reason: string; automatic: boolean } | null;
};
export type KeyproxyClients = { reachable: boolean; state: "ok" | "none" | "unreachable" | "outdated"; writable?: boolean; clients: KeyproxyClient[] };
type ClientResult = { client: KeyproxyClient; changed?: Record<string, { from: string; to: string }>; kept?: Record<string, string>; reverted?: { from: string; to?: string } };

function keptLabels(client: KeyproxyClient, keys: string[]): string {
  return keys.map((key) => client.field_labels?.[key] ?? key).join(", ");
}

/**
 * The version each imitated CLI presents, the newest release the proxy found, and the buttons that
 * move it. A vendor refuses a version it considers too old, so this is where a stale one is fixed:
 * the proxy stores the new profile in its own file and uses it from the next request.
 */
function ClientRow({ client, writable, onDone, toast }: { client: KeyproxyClient; writable: boolean; onDone: (result: ClientResult) => void; toast: (text: string) => void }) {
  const [busy, setBusy] = useState<"" | "update" | "revert">("");
  const [said, setSaid] = useState("");
  const act = async (action: "update" | "revert") => {
    setBusy(action);
    try {
      const result = await api.post<ClientResult>(`/api/keyproxy/clients/${encodeURIComponent(client.name)}/${action}`, {});
      const moved = result.changed?.version;
      const kept = Object.keys(result.kept ?? {});
      const text = action === "update"
        ? [t("keyproxy.clients.updated", { label: client.label, from: moved?.from ?? client.version, to: result.client.version }), kept.length ? t("keyproxy.clients.kept", { fields: keptLabels(client, kept) }) : ""].filter(Boolean).join(" ")
        : t("keyproxy.clients.reverted.done", { label: client.label, version: result.client.version });
      setSaid(text);
      onDone(result);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy("");
    }
  };
  const source = client.source === "environment" ? t("keyproxy.clients.source.pinned", { name: client.pinned_by ?? "" }) : t(`keyproxy.clients.source.${client.source}`);
  return (
    <div className="kp-item" data-client={client.name}>
      <Row
        title={<>{client.label} <code>{client.version}</code></>}
        stack
        desc={
          <>
            {source}{client.logged_in === false ? ` · ${t("keyproxy.login.off")}` : ""}
            <br />
            {client.checked_at ? t("keyproxy.clients.checked", { when: relTimeLong(client.checked_at) }) : t("keyproxy.clients.unchecked")}
            {client.check_error ? ` · ${t("keyproxy.clients.checkfailed", { error: client.check_error })}` : ""}
            {client.stale && client.kept_on_update.length ? ` · ${t("keyproxy.clients.willkeep", { fields: keptLabels(client, client.kept_on_update) })}` : ""}
            {client.reverted ? ` · ${t(client.reverted.automatic ? "keyproxy.clients.reverted.auto" : "keyproxy.clients.reverted.manual", { version: client.reverted.from, reason: client.reverted.reason })}` : ""}
            {said && <span className="clients-said" data-said={client.name}> · {said}</span>}
          </>
        }
      >
        {client.pending && <Badge tone="attention">{t("keyproxy.clients.pending")}</Badge>}
        {client.stale
          ? <Badge tone="attention">{t("keyproxy.clients.out", { version: client.latest ?? "" })}</Badge>
          : client.latest ? <Badge>{t("keyproxy.clients.current")}</Badge> : null}
        {client.stale && (
          <Button size="sm" variant="primary" data-action="update" disabled={!writable || !!busy} onClick={() => void act("update")}>{t(busy === "update" ? "keyproxy.clients.updating" : "keyproxy.clients.update")}</Button>
        )}
        {client.previous_version && (
          <Button size="sm" data-action="revert" disabled={!writable || !!busy} title={t("keyproxy.clients.revert.to", { version: client.previous_version })} onClick={() => void act("revert")}>{t("keyproxy.clients.revert")}</Button>
        )}
      </Row>
    </div>
  );
}

/** The imitated clients' versions, inside the key proxy's card. Hidden where no endpoint goes through a proxy. */
export function ClientVersions({ toast }: { toast: (text: string) => void }) {
  const [data, setData] = useState<KeyproxyClients | null>(null);
  const [checking, setChecking] = useState(false);
  const load = useCallback(() => {
    api.get<KeyproxyClients>("/api/keyproxy/clients").then(setData).catch(() => setData({ reachable: false, state: "unreachable", clients: [] }));
  }, []);
  useEffect(load, [load]);
  if (!data || data.state === "none") return null;
  const check = async () => {
    setChecking(true);
    try {
      setData(await api.post<KeyproxyClients>("/api/keyproxy/clients/check", {}));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setChecking(false);
    }
  };
  const replace = (result: ClientResult) => setData((d) => d && { ...d, clients: (d.clients ?? []).map((c) => (c.name === result.client.name ? result.client : c)) });
  return (
    <SetGroup className="keyproxy-clients" title={t("keyproxy.clients.title")} help={t("keyproxy.clients.sub")} data-setting="keyproxy.clients">
      {!data.reachable
        ? <SetBlock><span className="sub">{t(data.state === "outdated" ? "keyproxy.clients.outdated" : "keyproxy.unreachable")}</span></SetBlock>
        : (
          <>
            {(data.clients ?? []).map((client) => <ClientRow key={client.name} client={client} writable={!!data.writable} onDone={replace} toast={toast} />)}
            <SetFoot>
              <Button size="sm" data-action="check" disabled={checking} onClick={() => void check()}>{t(checking ? "keyproxy.clients.checking" : "keyproxy.clients.check")}</Button>
            </SetFoot>
          </>
        )}
    </SetGroup>
  );
}

function Unavailable({ data }: { data: KeyproxySettings | null }) {
  if (!data) return <div className="sub">{t("common.loading")}</div>;
  return <div className="sub">{t(data.state === "none" ? "keyproxy.none" : data.state === "refused" ? "keyproxy.refused" : "keyproxy.unreachable")}</div>;
}

/** Everything the key proxy holds, as groups on the models page. */
export function KeyproxyCard({ toast, onChanged }: { toast: (text: string) => void; onChanged?: () => void }) {
  const { data, write } = useKeyproxy(toast, onChanged);
  const rows = data?.upstreams ?? [];
  const writable = !!data?.writable;
  const group = (g: KeyproxyUpstream["group"]) => rows.filter((row) => row.group === g);
  return (
    <div className="keyproxy-card" data-setting="keyproxy.upstreams">
      <p className="set-help">{t("keyproxy.sub")}</p>
      {!data?.reachable ? <SetGroup><SetBlock><Unavailable data={data} /></SetBlock></SetGroup> : (
        <>
          {!writable && <div className="set-note" data-tone="warn">{t("keyproxy.readonly")}</div>}
          {data.keys_hidden === false && <div className="set-note" data-tone="warn" data-keys-readable="">{t("keyproxy.keys.readable")}</div>}
          <SetGroup title={t("keyproxy.group.model")}>
            {group("model").map((row) => <UpstreamRow key={row.upstream} row={row} write={write} writable={writable} />)}
          </SetGroup>
          <SetGroup title={t("keyproxy.group.search")}>
            {group("search").map((row) => <UpstreamRow key={row.upstream} row={row} write={write} writable={writable} />)}
          </SetGroup>
          <SetGroup title={t("keyproxy.group.extra")}>
            {group("extra").length ? group("extra").map((row) => <UpstreamRow key={row.upstream} row={row} write={write} writable={writable} />) : <SetBlock><span className="sub">{t("keyproxy.extra.none")}</span></SetBlock>}
          </SetGroup>
          <AddUpstream write={write} writable={writable} taken={rows.map((row) => row.upstream)} />
          {!!data.cli_logins?.length && (
            <SetGroup>
              <Row title={t("keyproxy.logins")} desc={t("keyproxy.logins.sub")} stack>
                <span className="settings-value">{data.cli_logins.map((login) => `${login.upstream}: ${t(login.configured ? "keyproxy.login.on" : "keyproxy.login.off")}`).join(" · ")}</span>
              </Row>
            </SetGroup>
          )}
          <ClientVersions toast={toast} />
        </>
      )}
    </div>
  );
}

/** The search backends' keys, beside the search settings where a reader looks for them. */
export function SearchKeys({ toast, onChanged }: { toast: (text: string) => void; onChanged?: () => void }) {
  const { data, write } = useKeyproxy(toast, onChanged);
  const rows = (data?.upstreams ?? []).filter((row) => row.group === "search");
  return (
    <SetGroup title={t("keyproxy.search.title")} help={t("keyproxy.search.sub")} data-setting="tools.web.search.keys">
      {!data?.reachable ? <SetBlock><Unavailable data={data} /></SetBlock> : rows.map((row) => (
        <Row key={row.upstream} title={upstreamTitle(row)} desc={KEY_LINKS[row.upstream] ? <>{t("free.key.get")} <a href={KEY_LINKS[row.upstream]} target="_blank" rel="noopener noreferrer">{upstreamTitle(row)} ↗</a></> : undefined}>
          <span className="set-key"><KeyInput v={row.key} label={`${upstreamTitle(row)}: ${t("keyproxy.key")}`} disabled={!data.writable} onSave={(value) => write(row.key.name, value)} /></span>
        </Row>
      ))}
    </SetGroup>
  );
}
