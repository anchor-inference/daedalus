// Settings → Tools → MCP servers: every configured server with its state, and adding, changing,
// signing in to and removing one while the host runs. Before this the card only listed the names
// and said to edit config.toml, and a server that needed a sign-in had no button anywhere.

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { confirmAsync, errorText } from "../ui";
import { Segmented } from "../ui/components";


type OAuthView = { scopes: string[]; issuer: string; client_id: string; client_secret: string; linked: boolean; redirect_uri: string };
export type McpServerView = {
  name: string; transport: "stdio" | "http"; command: string; args: string[]; url: string; description: string;
  headers: Record<string, string>; env: Record<string, string>; oauth: OAuthView | null;
  approval: string; tool_approval: Record<string, string>;
  state: string; error: string | null; tools: string[];
};
type Probe = { state: string; error: string | null; tools: string[] };
type Draft = {
  name: string; transport: "stdio" | "http"; command: string; args: string; env: string; url: string; headers: string;
  description: string; oauth: boolean; scopes: string; clientId: string; clientSecret: string;
};

const TONE: Record<string, string> = { ready: "ok", connecting: "warn", auth_required: "warn", unavailable: "bad" };
const enc = encodeURIComponent;

const lines = (values: Record<string, string>, sep: string) => Object.entries(values).map(([key, value]) => `${key}${sep}${value}`).join("\n");
function pairs(text: string, sep: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const line of text.split("\n")) {
    const at = line.indexOf(sep);
    if (at > 0) out[line.slice(0, at).trim()] = line.slice(at + sep.length).trim();
  }
  return out;
}

const blank = (): Draft => ({ name: "", transport: "http", command: "", args: "", env: "", url: "", headers: "", description: "", oauth: false, scopes: "", clientId: "", clientSecret: "" });
const draftOf = (s: McpServerView): Draft => ({
  name: s.name, transport: s.transport, command: s.command, args: s.args.join("\n"), env: lines(s.env, "="), url: s.url,
  headers: lines(s.headers, ": "), description: s.description, oauth: !!s.oauth, scopes: (s.oauth?.scopes ?? []).join(" "),
  clientId: s.oauth?.client_id ?? "", clientSecret: s.oauth?.client_secret ?? "",
});
// No approval and no per-tool approval: the form has no fields for them, and the host keeps a
// stored server's own when they are left out. Before it did, editing a description here put a server
// the operator had allowed back to "auto" and dropped its per-tool answers.
function body(d: Draft) {
  const stdio = d.transport === "stdio";
  return {
    transport: d.transport, description: d.description.trim(),
    command: stdio ? d.command.trim() : "", args: stdio ? d.args.split("\n").map((a) => a.trim()).filter(Boolean) : [],
    env: stdio ? pairs(d.env, "=") : {}, url: stdio ? "" : d.url.trim(), headers: stdio ? {} : pairs(d.headers, ":"),
    oauth: !stdio && d.oauth ? { scopes: d.scopes.split(/\s+/).filter(Boolean), client_id: d.clientId.trim(), client_secret: d.clientSecret } : null,
  };
}

/** Sends the browser to the server's sign-in page; it comes back to this page when done. */
async function signIn(name: string, toast: (text: string) => void) {
  try {
    const { auth_url } = await api.post<{ auth_url: string }>(`/api/mcp/servers/${enc(name)}/oauth/begin`, {});
    window.location.assign(auth_url);
  } catch (error) {
    toast(errorText(error));
  }
}

export function McpServers({ toast }: { toast: (text: string) => void }) {
  const [servers, setServers] = useState<McpServerView[] | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const load = useCallback(() => {
    api.get<{ servers: McpServerView[] }>("/api/mcp/servers").then((view) => setServers(view.servers)).catch((error) => toast(errorText(error)));
  }, [toast]);
  useEffect(load, [load]);
  // The sign-in comes back here with its outcome in the address; it is said once and taken off.
  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const name = query.get("mcp");
    if (name === null) return;
    const failed = query.get("mcp_error");
    toast(failed ? t("settings.mcp.signin.failed", { name, error: failed }) : t("settings.mcp.signin.done", { name }));
    for (const key of ["mcp", "mcp_error", "mcp_linked"]) query.delete(key);
    const rest = query.toString();
    window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : "") + window.location.hash);
  }, [toast]);

  const said = (name: string, probe: Probe) => {
    if (probe.state === "ready") toast(t("settings.mcp.ready", { name, n: probe.tools.length }));
    else if (probe.state === "auth_required") toast(t("settings.mcp.needsSignin", { name }));
    else toast(t("settings.mcp.notAnswering", { name, error: probe.error || probe.state }));
  };
  async function save() {
    if (!draft) return;
    const name = (editing ?? draft.name).trim();
    setBusy(name);
    try {
      const answer = await api.put<{ probe: Probe; servers: McpServerView[] }>(`/api/mcp/servers/${enc(name)}`, body(draft));
      setServers(answer.servers);
      setDraft(null);
      setEditing(null);
      said(name, answer.probe);
    } catch (error) {
      toast(errorText(error));
    } finally { setBusy(null); }
  }
  async function remove(name: string) {
    if (!(await confirmAsync(t("settings.mcp.remove.title", { name }), { body: t("settings.mcp.remove.body"), action: t("settings.mcp.remove.action"), danger: true }))) return;
    setBusy(name);
    try {
      setServers((await api.delete<{ servers: McpServerView[] }>(`/api/mcp/servers/${enc(name)}`)).servers);
      toast(t("settings.mcp.removed", { name }));
    } catch (error) {
      toast(errorText(error));
    } finally { setBusy(null); }
  }
  async function connect(name: string) {
    setBusy(name);
    try {
      const answer = await api.post<{ probe: Probe; servers: McpServerView[] }>(`/api/mcp/servers/${enc(name)}/connect`, {});
      setServers(answer.servers);
      said(name, answer.probe);
    } catch (error) {
      toast(errorText(error));
    } finally { setBusy(null); }
  }
  async function signOut(name: string) {
    setBusy(name);
    try {
      setServers((await api.post<{ servers: McpServerView[] }>(`/api/mcp/servers/${enc(name)}/oauth/disconnect`, {})).servers);
    } catch (error) {
      toast(errorText(error));
    } finally { setBusy(null); }
  }

  const form = draft && (
    <div className="mfields mcp-form" data-mcp-form>
      <label className="mfield">
        <span>{t("settings.mcp.name")}</span>
        <input className="field mono" value={draft.name} disabled={!!editing} placeholder="linear" spellCheck={false} onChange={(e) => setDraft({ ...draft, name: e.target.value })} />
      </label>
      <div className="mfield">
        <span>{t("settings.mcp.transport")}</span>
        <Segmented value={draft.transport} label={t("settings.mcp.transport")} onChange={(transport) => setDraft({ ...draft, transport })}
          options={[{ id: "http", label: t("settings.mcp.transport.http") }, { id: "stdio", label: t("settings.mcp.transport.stdio") }]} />
      </div>
      {draft.transport === "stdio" ? <>
        <label className="mfield wide">
          <span>{t("settings.mcp.command")}</span>
          <input className="field mono" value={draft.command} placeholder="npx" spellCheck={false} onChange={(e) => setDraft({ ...draft, command: e.target.value })} />
        </label>
        <label className="mfield wide">
          <span>{t("settings.mcp.args")}</span>
          <textarea className="field mono" rows={3} value={draft.args} placeholder={"-y\n@modelcontextprotocol/server-filesystem\n/data"} spellCheck={false} onChange={(e) => setDraft({ ...draft, args: e.target.value })} />
        </label>
        <label className="mfield wide">
          <span>{t("settings.mcp.env")}</span>
          <textarea className="field mono" rows={2} value={draft.env} placeholder="API_KEY=…" spellCheck={false} onChange={(e) => setDraft({ ...draft, env: e.target.value })} />
        </label>
      </> : <>
        <label className="mfield wide">
          <span>{t("settings.mcp.url")}</span>
          <input className="field mono" value={draft.url} placeholder="https://mcp.example.com/mcp" spellCheck={false} onChange={(e) => setDraft({ ...draft, url: e.target.value })} />
        </label>
        <label className="mfield wide">
          <span>{t("settings.mcp.headers")}</span>
          <textarea className="field mono" rows={2} value={draft.headers} placeholder={t("settings.mcp.headers.example")} spellCheck={false} onChange={(e) => setDraft({ ...draft, headers: e.target.value })} />
        </label>
        <label className="mfield wide mcp-check">
          <input type="checkbox" checked={draft.oauth} onChange={(e) => setDraft({ ...draft, oauth: e.target.checked })} />
          <span>{t("settings.mcp.oauth")}</span>
        </label>
        {draft.oauth && <>
          <label className="mfield wide">
            <span>{t("settings.mcp.scopes")}</span>
            <input className="field mono" value={draft.scopes} placeholder={t("settings.mcp.scopes.hint")} spellCheck={false} onChange={(e) => setDraft({ ...draft, scopes: e.target.value })} />
          </label>
          <label className="mfield">
            <span>{t("settings.mcp.clientId")}</span>
            <input className="field mono" value={draft.clientId} placeholder={t("settings.mcp.clientId.hint")} spellCheck={false} onChange={(e) => setDraft({ ...draft, clientId: e.target.value })} />
          </label>
          <label className="mfield">
            <span>{t("settings.mcp.clientSecret")}</span>
            <input className="field" type="password" autoComplete="off" value={draft.clientSecret} onChange={(e) => setDraft({ ...draft, clientSecret: e.target.value })} />
          </label>
        </>}
      </>}
      <label className="mfield wide">
        <span>{t("settings.mcp.description")}</span>
        <input className="field" value={draft.description} placeholder={t("settings.mcp.description.hint")} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
      </label>
      <div className="mfield end mcp-actions">
        <button type="button" className="btn ghost" onClick={() => { setDraft(null); setEditing(null); }}>{t("common.cancel")}</button>
        <button type="button" className="btn primary" disabled={!!busy || !(editing ?? draft.name).trim() || (draft.transport === "stdio" ? !draft.command.trim() : !draft.url.trim())} onClick={() => void save()}>
          {busy ? t("settings.mcp.saving") : editing ? t("common.save") : t("settings.mcp.add")}
        </button>
      </div>
    </div>
  );

  return (
    <div className="card" data-setting="mcp.servers">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.mcp.title")}</div>
      <div className="sub">{t("settings.mcp.sub")}</div>
      <div className="dt-mcp mcp-list">
        {servers !== null && servers.length === 0 && !draft && <div className="sub" style={{ marginTop: 6 }}>{t("settings.mcp.none")}</div>}
        {(servers ?? []).map((s) => editing === s.name ? <div key={s.name}>{form}</div> : (
          <div key={s.name} className="dt-server" data-mcp-server={s.name}>
            <div className="dt-server-head">
              <span className="dt-server-name">{s.name}</span>
              <span className="dt-state" data-tone={TONE[s.state] ?? ""}>{t(`session.mcp.state.${s.state}`)}</span>
              <span className="grow" />
              {s.oauth && !s.oauth.linked && <button type="button" className="btn small primary" disabled={!!busy} onClick={() => void signIn(s.name, toast)}>{t("settings.mcp.signin")}</button>}
              {s.oauth?.linked && <button type="button" className="btn small ghost" disabled={!!busy} onClick={() => void signOut(s.name)}>{t("settings.mcp.signout")}</button>}
              <button type="button" className="btn small ghost" disabled={!!busy} onClick={() => void connect(s.name)}>{busy === s.name ? t("settings.mcp.checking") : t("settings.mcp.check")}</button>
              <button type="button" className="btn small ghost" disabled={!!busy || !!draft} onClick={() => { setEditing(s.name); setDraft(draftOf(s)); }}>{t("common.edit")}</button>
              <button type="button" className="btn small danger" disabled={!!busy} onClick={() => void remove(s.name)}>{t("common.remove")}</button>
            </div>
            <div className="dt-hint mono">{s.transport === "stdio" ? [s.command, ...s.args].join(" ") : s.url}</div>
            {s.description && <div className="dt-hint">{s.description}</div>}
            {s.error && <div className="dt-hint" data-tone="bad">{s.error}</div>}
            {s.tools.length > 0 && <div className="dt-hint">{t("settings.mcp.tools", { n: s.tools.length })}</div>}
          </div>
        ))}
      </div>
      {draft && !editing ? form : !draft && <button type="button" className="btn" style={{ marginTop: 10 }} onClick={() => setDraft(blank())}>{t("settings.mcp.new")}</button>}
      <div className="sub" style={{ marginTop: 8 }}>{t("settings.mcp.apply")}</div>
    </div>
  );
}
