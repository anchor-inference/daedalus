// Settings → Tools → MCP servers: every configured server with its state, and adding, changing,
// signing in to and removing one while the host runs. Before this the card only listed the names
// and said to edit config.toml, and a server that needed a sign-in had no button anywhere.

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { errorText } from "../ui";
import { Button, confirmDialog, Segmented, Select, Sheet, Switch } from "../ui/primitives";
import { Row, SetBlock, SetGroup } from "../settingsrow";
import { McpQuickAdd, locationOptions, policyOptions, type McpHost, type McpLocation, type McpPolicy } from "../mcpquickadd";


type OAuthView = { scopes: string[]; issuer: string; client_id: string; client_secret: string; linked: boolean; redirect_uri: string };
export type McpServerView = {
  name: string; transport: "stdio" | "http"; command: string; args: string[]; url: string; description: string;
  headers: Record<string, string>; env: Record<string, string>; oauth: OAuthView | null;
  approval: string; tool_approval: Record<string, string>;
  state: string; error: string | null; tools: string[]; remote_tools?: string[]; stderr?: string; runs_in?: string; location?: McpLocation;
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
  const [host, setHost] = useState<McpHost | null>(null);
  const load = useCallback(() => {
    api.get<{ servers: McpServerView[]; host?: McpHost }>("/api/mcp/servers").then((view) => { setServers(view.servers); setHost(view.host ?? null); })
      .catch((error) => toast(errorText(error)));
  }, [toast]);
  useEffect(load, [load]);
  // The sign-in comes back here with its outcome in the address; it is said once and taken off.
  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const name = query.get("mcp");
    if (name === null) return;
    const failed = query.get("mcp_error");
    if (failed) toast(t("settings.mcp.signin.failed", { name, error: failed }));
    for (const key of ["mcp", "mcp_error", "mcp_linked"]) query.delete(key);
    const rest = query.toString();
    window.history.replaceState(null, "", window.location.pathname + (rest ? `?${rest}` : "") + window.location.hash);
  }, [toast]);

  const said = (name: string, probe: Probe) => {
    if (probe.state !== "ready" && probe.state !== "auth_required") toast(t("settings.mcp.notAnswering", { name, error: probe.error || probe.state }));
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
    if (!(await confirmDialog({ title: t("settings.mcp.remove.title", { name }), body: t("settings.mcp.remove.body"), action: t("settings.mcp.remove.action"), danger: true }))) return;
    setBusy(name);
    try {
      setServers((await api.delete<{ servers: McpServerView[] }>(`/api/mcp/servers/${enc(name)}`)).servers);
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
  // The server's policy and its tools' own, saved with the rest of the card as it stands (its keys
  // go back masked, and the host keeps the stored ones).
  async function setPolicy(s: McpServerView, approval: string, toolApproval: Record<string, string>) {
    setBusy(s.name);
    try {
      const answer = await api.put<{ servers: McpServerView[] }>(`/api/mcp/servers/${enc(s.name)}`, { ...body(draftOf(s)), approval, tool_approval: toolApproval });
      setServers(answer.servers);
    } catch (error) {
      toast(errorText(error));
    } finally { setBusy(null); }
  }
  // Where a stdio server runs: moving it restarts its process in the other place on its next use,
  // and the check that follows says at once whether it answers there.
  async function setLocation(s: McpServerView, location: McpLocation) {
    setBusy(s.name);
    try {
      const answer = await api.put<{ probe: Probe; servers: McpServerView[] }>(`/api/mcp/servers/${enc(s.name)}`, { ...body(draftOf(s)), location });
      setServers(answer.servers);
      said(s.name, answer.probe);
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

  const canSave = !busy && !!(editing ?? draft?.name ?? "").trim() && (draft?.transport === "stdio" ? !!draft.command.trim() : !!draft?.url.trim());
  const closeForm = () => { setDraft(null); setEditing(null); };
  // Adding and changing are one form, in a sheet over the page: it is a task of its own with a
  // decision at its foot, not a fifth block among the servers' rows.
  const form = draft && (
    <Sheet
      title={editing ?? t("settings.mcp.new")}
      size="md"
      onClose={closeForm}
      footer={<>
        <Button variant="quiet" onClick={closeForm}>{t("common.cancel")}</Button>
        <Button variant="primary" disabled={!canSave} busy={!!busy} onClick={() => void save()}>{busy ? t("settings.mcp.saving") : editing ? t("common.save") : t("settings.mcp.add")}</Button>
      </>}
    >
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
          <Row title={t("settings.mcp.oauth")}>
            <Switch checked={draft.oauth} onChange={(oauth) => setDraft({ ...draft, oauth })} label={t("settings.mcp.oauth")} />
          </Row>
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
      </div>
    </Sheet>
  );

  return (
    <SetGroup
      title={t("settings.mcp.title")}
      help={t("settings.mcp.sub")}
      data-setting="mcp.servers"
      action={<Button size="sm" disabled={!!draft} onClick={() => setDraft(blank())}>{t("settings.mcp.new")}</Button>}
    >
      {servers !== null && servers.length === 0 && <SetBlock><span className="set-hint">{t("settings.mcp.none")}</span></SetBlock>}
      {(servers ?? []).map((s) => (
        <div key={s.name} className="mcp-server" data-mcp-server={s.name}>
          <Row
            title={<span className="mcp-name"><span className="mono">{s.name}</span><span className="dt-state" data-tone={TONE[s.state] ?? ""}>{t(`session.mcp.state.${s.state}`)}</span></span>}
            desc={<>
              <span className="mono mcp-cmd">{s.transport === "stdio" ? [s.command, ...s.args].join(" ") : s.url}</span>
              {s.description && <span className="mcp-line">{s.description}</span>}
              {s.runs_in && <span className="mcp-line" data-runs-in={s.runs_in}>{t(`mcp.where.${s.runs_in}`)}</span>}
            </>}
          >
            {s.oauth && !s.oauth.linked && <Button size="sm" variant="primary" disabled={!!busy} onClick={() => void signIn(s.name, toast)}>{t("settings.mcp.signin")}</Button>}
            {s.oauth?.linked && <Button size="sm" variant="quiet" disabled={!!busy} onClick={() => void signOut(s.name)}>{t("settings.mcp.signout")}</Button>}
            <Button size="sm" variant="quiet" disabled={!!busy} onClick={() => void connect(s.name)}>{busy === s.name ? t("settings.mcp.checking") : t("settings.mcp.check")}</Button>
            <Button size="sm" variant="quiet" disabled={!!busy || !!draft} onClick={() => { setEditing(s.name); setDraft(draftOf(s)); }}>{t("common.edit")}</Button>
            <Button size="sm" variant="danger" disabled={!!busy} onClick={() => void remove(s.name)}>{t("common.remove")}</Button>
          </Row>
          {s.error && <SetBlock><span className="set-hint mcp-bad">{s.error}</span></SetBlock>}
          {s.stderr && s.state !== "ready" && <SetBlock><details className="mcp-stderr"><summary>{t("mcp.quick.stderr")}</summary><pre className="mono">{s.stderr}</pre></details></SetBlock>}
          {s.transport === "stdio" && host?.offered && (
            <Row title={t("mcp.location")} data-mcp-location={s.location ?? "server"} desc={s.location === "host" && host.problem ? <span className="mcp-warn">{t("mcp.location.hostProblem", { problem: host.problem })}</span> : undefined}>
              <Segmented value={s.location ?? "server"} label={t("mcp.location")} options={locationOptions()} onChange={(location) => void setLocation(s, location)} />
            </Row>
          )}
          <Row title={t("mcp.policy")} data-mcp-policy={s.approval}>
            <Segmented value={s.approval as McpPolicy} label={t("mcp.policy")} options={policyOptions()} onChange={(approval) => void setPolicy(s, approval, s.tool_approval)} />
          </Row>
          {(s.remote_tools ?? []).length > 0 && (
            <SetBlock>
              <details className="mcp-tools">
                <summary>{t("settings.mcp.tools", { n: s.remote_tools!.length })}</summary>
                {s.remote_tools!.map((tool) => (
                  <div key={tool} className="mcp-tool-row" data-mcp-tool={tool}>
                    <span className="mono grow truncate">{tool}</span>
                    <Select
                      label={t("mcp.policy.tool", { tool })}
                      value={s.tool_approval[tool] ?? "auto"}
                      disabled={!!busy}
                      onChange={(approval) => void setPolicy(s, s.approval, { ...s.tool_approval, [tool]: approval })}
                      options={policyOptions().map((option) => ({ id: option.id, label: option.id === "auto" ? t("mcp.policy.tool.auto") : option.label }))}
                    />
                  </div>
                ))}
              </details>
            </SetBlock>
          )}
        </div>
      ))}
      <SetBlock><McpQuickAdd toast={toast} onAdded={() => load()} /></SetBlock>
      <SetBlock><span className="set-hint">{t("settings.mcp.apply")}</span></SetBlock>
      {form}
    </SetGroup>
  );
}
