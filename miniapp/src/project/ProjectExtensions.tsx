// Extensions are discovered after a project exists. The catalogue is an offer; only the installed
// projection and a successful test establish that an extension is actually usable.

import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { invalidate, useOffline, useQuery } from "../store";
import { errorText } from "../ui";

type InputField = { type?: string; title?: string; enum?: (string | number)[] };
type PluginTool = { name: string; read_model?: string; input_schema: { properties?: Record<string, InputField>; required?: string[] } };
type UiExtension = { id: string; slot: string; component: string; tool: string };
type Manifest = { id: string; version: string; display_name: string; description: string; capabilities: string[]; tools: PluginTool[]; ui_extensions: UiExtension[] };
type CatalogItem = { manifest: Manifest; valid: boolean; digest: string; required_capabilities: string[]; ui_extensions: UiExtension[] };
type InstalledItem = { id: string; version: string; digest: string; manifest?: Manifest; status: string; health: string; activation_state?: string; created_at: string };
type Installed = { items: InstalledItem[]; collection_revision: number };
type Health = { id: string; state: "healthy" | "configured_unverified" | "unhealthy" | "inactive" | "disabled"; reason?: string };
type ReadModel = { id: string; input_schema: PluginTool["input_schema"] };

const installedKey = "/api/plugins";

function ExtensionCard({ item, installed, activeVersion, revision, projectId, toast, safe }: { item: CatalogItem; installed: InstalledItem | undefined; activeVersion: string | undefined; revision: number | undefined; projectId: string; toast: (message: string) => void; safe: boolean }) {
  const offline = useOffline();
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const operation = useRef<{ digest: string; revision: number; id: string } | null>(null);
  const lifecycleOperation = useRef<{ action: string; revision: number; id: string } | null>(null);
  const health = useQuery<Health>(installed ? `/api/plugins/${encodeURIComponent(item.manifest.id)}/health` : null, { staleMs: 3000 });
  const extension = item.manifest.ui_extensions.find((entry) => entry.slot === "project.settings");
  const tool = item.manifest.tools.find((entry) => entry.name === extension?.tool);
  const fields = Object.entries(tool?.input_schema.properties ?? {}).filter(([name]) => name !== "project_id");
  const supported = !!extension && !!tool && fields.every(([, field]) => ["string", "integer", "number", "boolean"].includes(field.type ?? ""));
  const active = installed?.status === "active" && (health.data?.state === "configured_unverified" || health.data?.state === "healthy") && !safe;
  const installBlocked = offline || !item.valid || !Number.isInteger(revision) || !!installed;

  async function install() {
    if (installBlocked || revision === undefined || busy) return;
    if (!operation.current || operation.current.digest !== item.digest || operation.current.revision !== revision) {
      operation.current = { digest: item.digest, revision, id: crypto.randomUUID() };
    }
    setBusy(true);
    try {
      await api.post("/api/plugins/install", { manifest: item.manifest, expected_digest: item.digest, expected_collection_revision: revision, client_operation_id: operation.current.id });
      operation.current = null;
      toast(t("extension.staged"));
      invalidate(installedKey);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) operation.current = null;
      toast(errorText(error));
      invalidate(installedKey);
    } finally { setBusy(false); }
  }

  async function test() {
    if (!active || !supported || !tool || busy) return;
    const arguments_: Record<string, string | number | boolean> = {};
    for (const [name, field] of fields) {
      const value = inputs[name] ?? "";
      if (!value && !tool.input_schema.required?.includes(name)) continue;
      if (!value) return;
      if (field.type === "boolean") arguments_[name] = value === "true";
      else if (field.type === "integer" || field.type === "number") {
        const parsed = Number(value);
        if (!Number.isFinite(parsed) || (field.type === "integer" && !Number.isInteger(parsed))) return;
        arguments_[name] = parsed;
      } else arguments_[name] = value;
    }
    setBusy(true);
    setTestResult(null);
    try {
      const response = await api.post<{ result: unknown }>(`/api/projects/${encodeURIComponent(projectId)}/plugins/${encodeURIComponent(item.manifest.id)}/invoke-read`, { tool: tool.name, arguments: arguments_ });
      setTestResult(JSON.stringify(response.result, null, 2) ?? "null");
    } catch (error) { setTestResult(errorText(error)); }
    finally { setBusy(false); }
  }

  async function changeLifecycle(action: "remove" | "rollback" | "retry") {
    if (!Number.isInteger(revision) || offline || busy || !installed) return;
    if (action === "remove" && installed.status !== "active") return;
    if (action === "rollback" && (installed.status !== "revoked" || !activeVersion || !item.manifest.tools.every((entry) => entry.read_model))) return;
    if (action === "retry" && (installed.status !== "failed" || !["failed", "cancelled"].includes(installed.activation_state ?? ""))) return;
    if (!lifecycleOperation.current || lifecycleOperation.current.action !== action || lifecycleOperation.current.revision !== revision) {
      lifecycleOperation.current = { action, revision: revision!, id: crypto.randomUUID() };
    }
    setBusy(true);
    try {
      const path = `/api/plugins/${encodeURIComponent(item.manifest.id)}/${encodeURIComponent(item.manifest.version)}`;
      const body = { expected_collection_revision: revision, client_operation_id: lifecycleOperation.current.id };
      if (action === "remove") await api.request("DELETE", path, body);
      else if (action === "rollback") await api.post(`${path}/rollback`, { ...body, current_version: activeVersion });
      else await api.post(`${path}/retry`, body);
      lifecycleOperation.current = null;
      invalidate(installedKey);
      toast(action === "remove" ? t("extension.removeRecorded") : t("extension.staged"));
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) lifecycleOperation.current = null;
      toast(errorText(error));
      invalidate(installedKey);
    } finally { setBusy(false); }
  }

  return <article className="project-extension">
    <div className="project-extension-head"><b>{item.manifest.display_name}</b><span className="sub">{installed ? t(`extension.state.${installed.status}`) : t("extension.available")}</span></div>
    <p>{item.manifest.description}</p>
    <details><summary>{t("extension.permissions")}</summary>
      <ul>{item.required_capabilities.map((capability) => <li key={capability}>{capability}</li>)}</ul>
      <div className="mono">{t("extension.version", { version: item.manifest.version })} · {item.digest}</div>
    </details>
    {installed && health.data && <div className={health.data.state === "healthy" || health.data.state === "configured_unverified" ? "sub" : "result-warning"} role="status">{t(`extension.health.${health.data.state}`)}{health.data.reason ? ` · ${health.data.reason}` : ""}</div>}
    {safe && installed && <div className="result-warning" role="status">{t("extension.safeBlocked")}</div>}
    {!installed && <button type="button" className="btn small" disabled={busy || installBlocked} onClick={() => void install()}>{t("extension.install")}</button>}
    {installed && fields.map(([name, field]) => <label key={name} className="field">
      <span>{field.title || name}</span>
      {field.type === "boolean" || field.enum ? <select value={inputs[name] ?? ""} disabled={busy || !active} onChange={(event) => setInputs({ ...inputs, [name]: event.target.value })}>
        <option value=""></option>
        {(field.enum ?? ["true", "false"]).map((value) => <option key={String(value)} value={String(value)}>{String(value)}</option>)}
      </select> : <input type={field.type === "integer" || field.type === "number" ? "number" : "text"} value={inputs[name] ?? ""} disabled={busy || !active} onChange={(event) => setInputs({ ...inputs, [name]: event.target.value })} />}
    </label>)}
    {installed && <button type="button" className="btn small" disabled={busy || !active || !supported} onClick={() => void test()}>{t("extension.test")}</button>}
    {installed?.status === "active" && <button type="button" className="btn small" disabled={busy || offline || !Number.isInteger(revision)} onClick={() => void changeLifecycle("remove")}>{t("extension.remove")}</button>}
    {installed?.status === "revoked" && activeVersion && item.manifest.tools.every((entry) => entry.read_model) && <button type="button" className="btn small" disabled={busy || offline || !Number.isInteger(revision)} onClick={() => void changeLifecycle("rollback")}>{t("extension.rollback")}</button>}
    {installed?.status === "failed" && ["failed", "cancelled"].includes(installed.activation_state ?? "") && <button type="button" className="btn small" disabled={busy || offline || !Number.isInteger(revision)} onClick={() => void changeLifecycle("retry")}>{t("extension.retry")}</button>}
    {installed && !supported && <div className="result-warning" role="status">{t("extension.unconfirmed")}</div>}
    {installed?.status === "staged" && <div className="sub" role="status">{t("extension.waitActive")}</div>}
    {testResult && <pre className="sub" role="status">{testResult}</pre>}
    {(offline || !item.valid || (installed && !active && !safe && !health.data)) && <div className="result-warning" role="status">{t("extension.unconfirmed")}</div>}
  </article>;
}

export function ProjectExtensions({ projectId, toast }: { projectId: string; toast: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  return <details className="project-extensions" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("extension.title")}</summary>
    {open && <ExtensionContent projectId={projectId} toast={toast} />}
  </details>;
}

function CustomReadBuilder({ models, installed, projectId, offline, safe, toast }: {
  models: ReadModel[]; installed: Installed | undefined; projectId: string; offline: boolean;
  safe: boolean; toast: (message: string) => void;
}) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [version, setVersion] = useState("1.0.0");
  const [modelId, setModelId] = useState("task_counts");
  const [busy, setBusy] = useState(false);
  const [reviewed, setReviewed] = useState<{ source: string; digest: string; result: unknown } | null>(null);
  const operation = useRef<{ source: string; revision: number; id: string } | null>(null);
  const model = models.find((item) => item.id === modelId);
  const manifest = model ? {
    id, version, display_name: name, description, host_api: "1", capabilities: ["board.read"],
    tools: [{ name: "read_card", input_schema: model.input_schema, read_model: model.id }],
    events: [], dependencies: [],
    ui_extensions: [{ id: "card", slot: "project.settings", schema_version: 1, component: "status", tool: "read_card" }],
  } : null;
  const source = manifest ? JSON.stringify(manifest) : "";
  const current = installed?.items.find((item) => item.id === id && item.status === "active");
  const exactVersionExists = installed?.items.some((item) => item.id === id && item.version === version);

  async function preview() {
    if (!manifest || offline || safe || busy) return;
    setBusy(true);
    setReviewed(null);
    try {
      const validated = await api.post<{ valid: boolean; digest: string }>("/api/plugins/validate", { manifest });
      const shown = await api.post<{ digest: string; preview_only: boolean; result: unknown }>(
        `/api/projects/${encodeURIComponent(projectId)}/plugins/preview-read`,
        { manifest, expected_digest: validated.digest, tool: "read_card", arguments: {} },
      );
      if (!validated.valid || !shown.preview_only || shown.digest !== validated.digest) throw new Error(t("extension.previewChanged"));
      setReviewed({ source, digest: validated.digest, result: shown.result });
    } catch (error) { toast(errorText(error)); }
    finally { setBusy(false); }
  }

  async function install() {
    const revision = installed?.collection_revision;
    if (!manifest || !reviewed || reviewed.source !== source || !Number.isInteger(revision) ||
        offline || safe || busy || exactVersionExists) return;
    if (!operation.current || operation.current.source !== source || operation.current.revision !== revision) {
      operation.current = { source, revision: revision!, id: crypto.randomUUID() };
    }
    setBusy(true);
    try {
      await api.post("/api/plugins/install", {
        manifest, expected_digest: reviewed.digest, expected_collection_revision: revision,
        client_operation_id: operation.current.id, replaces_version: current?.version ?? null,
      });
      operation.current = null;
      setReviewed(null);
      invalidate(installedKey);
      toast(t("extension.staged"));
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) operation.current = null;
      toast(errorText(error));
      invalidate(installedKey);
    } finally { setBusy(false); }
  }

  return <details className="project-extension">
    <summary>{t("extension.createRead")}</summary>
    <p className="sub">{t("extension.readOnlyHint")}</p>
    <label className="field"><span>{t("extension.id")}</span><input value={id} onChange={(event) => setId(event.target.value)} /></label>
    <label className="field"><span>{t("extension.name")}</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>
    <label className="field"><span>{t("extension.description")}</span><input value={description} onChange={(event) => setDescription(event.target.value)} /></label>
    <label className="field"><span>{t("extension.versionLabel")}</span><input value={version} onChange={(event) => setVersion(event.target.value)} /></label>
    <label className="field"><span>{t("extension.readModel")}</span><select value={modelId} onChange={(event) => setModelId(event.target.value)}>
      {models.map((entry) => <option key={entry.id} value={entry.id}>{t(`extension.model.${entry.id}`)}</option>)}
    </select></label>
    <button type="button" className="btn small" disabled={!model || offline || safe || busy} onClick={() => void preview()}>{t("extension.preview")}</button>
    {reviewed && reviewed.source === source && <div className="project-extension">
      <div className="sub">{t("extension.previewOnly")}</div>
      <div className="mono">{reviewed.digest}</div>
      <pre className="sub">{JSON.stringify(manifest, null, 2)}</pre>
      <pre className="sub">{JSON.stringify(reviewed.result, null, 2)}</pre>
      <button type="button" className="btn small" disabled={offline || safe || busy || exactVersionExists || !installed} onClick={() => void install()}>{current ? t("extension.upgrade") : t("extension.install")}</button>
    </div>}
    {reviewed && reviewed.source !== source && <div className="result-warning" role="status">{t("extension.previewChanged")}</div>}
  </details>;
}

function ExtensionContent({ projectId, toast }: { projectId: string; toast: (message: string) => void }) {
  const offline = useOffline();
  const catalog = useQuery<CatalogItem[]>("/api/plugins/catalog", { staleMs: 3000 });
  const readModels = useQuery<ReadModel[]>("/api/plugins/read-models", { staleMs: 3000 });
  const installed = useQuery<Installed>(installedKey, { staleMs: 3000 });
  const safeMode = useQuery<{ enabled: boolean }>("/api/plugins/safe-mode", { staleMs: 3000 });
  const [busy, setBusy] = useState(false);
  const operation = useRef<{ enabled: boolean; revision: number; id: string } | null>(null);
  const offered = Array.isArray(catalog.data) ? catalog.data : [];
  const custom = (installed.data?.items ?? []).filter((row) => row.manifest && !offered.some((item) => item.manifest.id === row.id && item.manifest.version === row.version))
    .map((row): CatalogItem => ({ manifest: row.manifest!, valid: true, digest: row.digest,
                                  required_capabilities: row.manifest!.capabilities,
                                  ui_extensions: row.manifest!.ui_extensions }));
  async function setSafeMode(enabled: boolean) {
    const revision = installed.data?.collection_revision;
    if (offline || !Number.isInteger(revision) || busy) return;
    if (!operation.current || operation.current.enabled !== enabled || operation.current.revision !== revision) operation.current = { enabled, revision: revision!, id: crypto.randomUUID() };
    setBusy(true);
    try {
      await api.post("/api/plugins/safe-mode", { enabled, expected_collection_revision: revision, client_operation_id: operation.current.id });
      operation.current = null;
      safeMode.refresh();
      installed.refresh();
      toast(t("extension.safeChanged"));
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) operation.current = null;
      toast(errorText(error));
      safeMode.refresh();
      installed.refresh();
    } finally { setBusy(false); }
  }
  return <div className="project-extension-list">
    <p className="sub">{t("extension.intro")}</p>
    {(catalog.error || installed.error || safeMode.error) && <div className="result-warning" role="status">{t("extension.unconfirmed")} <button type="button" className="linkbtn" onClick={() => { catalog.refresh(); installed.refresh(); safeMode.refresh(); }}>{t("common.retry")}</button></div>}
    {safeMode.data && <label className="toggle-row"><input type="checkbox" checked={safeMode.data.enabled} disabled={busy || offline || !installed.data} onChange={(event) => void setSafeMode(event.target.checked)} /><span>{t("extension.safeMode")}</span><span className="sub">{t("extension.safeModeHint")}</span></label>}
    {[...offered, ...custom].map((item) => <ExtensionCard key={`${item.manifest.id}:${item.manifest.version}`} item={item} installed={installed.data?.items.find((row) => row.id === item.manifest.id && row.version === item.manifest.version)} activeVersion={installed.data?.items.find((row) => row.id === item.manifest.id && row.status === "active")?.version} revision={installed.data?.collection_revision} projectId={projectId} toast={toast} safe={safeMode.data?.enabled ?? true} />)}
    {catalog.data && offered.length === 0 && custom.length === 0 && <div className="sub">{t("extension.none")}</div>}
    <CustomReadBuilder models={readModels.data ?? []} installed={installed.data} projectId={projectId} offline={offline} safe={safeMode.data?.enabled ?? true} toast={toast} />
  </div>;
}
