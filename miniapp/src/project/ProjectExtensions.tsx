// Extensions are discovered after a project exists. The catalogue is an offer; only the installed
// projection and a successful test establish that an extension is actually usable.

import { useRef, useState } from "react";
import { api, ApiError } from "../api";
import { t } from "../i18n";
import { invalidate, useOffline, useQuery } from "../store";
import { errorText } from "../ui";

type Manifest = { id: string; version: string; display_name: string; description: string; capabilities: string[]; tools: { name: string }[] };
type CatalogItem = { manifest: Manifest; valid: boolean; digest: string; required_capabilities: string[]; ui_extensions: { slot: string; component: string }[] };
type InstalledItem = { id: string; version: string; digest: string; status: string; health: string; created_at: string };
type Installed = { items: InstalledItem[]; collection_revision: number };
type Health = { id: string; state: "healthy" | "configured_unverified" | "unhealthy" | "inactive" | "disabled"; reason?: string };

const installedKey = "/api/plugins";

function ExtensionCard({ item, installed, revision, projectId, toast, safe }: { item: CatalogItem; installed: InstalledItem | undefined; revision: number | undefined; projectId: string; toast: (message: string) => void; safe: boolean }) {
  const offline = useOffline();
  const [busy, setBusy] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);
  const operation = useRef<{ digest: string; revision: number; id: string } | null>(null);
  const health = useQuery<Health>(installed ? `/api/plugins/${encodeURIComponent(item.manifest.id)}/health` : null, { staleMs: 3000 });
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
    if (!active || busy) return;
    setBusy(true);
    setTestResult(null);
    try {
      const response = await api.post<{ result: { project_id: string; name: string; tasks: Record<string, number> } }>(`/api/plugins/${encodeURIComponent(item.manifest.id)}/test`, { tool: "inspect_project", arguments: { project_id: projectId } });
      setTestResult(t("extension.testResult", { name: response.result.name, count: Object.values(response.result.tasks ?? {}).reduce((sum, value) => sum + Number(value || 0), 0) }));
    } catch (error) { setTestResult(errorText(error)); }
    finally { setBusy(false); }
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
    {installed && <button type="button" className="btn small" disabled={busy || !active} onClick={() => void test()}>{t("extension.test")}</button>}
    {installed?.status === "staged" && <div className="sub" role="status">{t("extension.waitActive")}</div>}
    {testResult && <div className="sub" role="status">{testResult}</div>}
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

function ExtensionContent({ projectId, toast }: { projectId: string; toast: (message: string) => void }) {
  const offline = useOffline();
  const catalog = useQuery<CatalogItem[]>("/api/plugins/catalog", { staleMs: 3000 });
  const installed = useQuery<Installed>(installedKey, { staleMs: 3000 });
  const safeMode = useQuery<{ enabled: boolean }>("/api/plugins/safe-mode", { staleMs: 3000 });
  const [busy, setBusy] = useState(false);
  const operation = useRef<{ enabled: boolean; revision: number; id: string } | null>(null);
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
    {Array.isArray(catalog.data) && catalog.data.map((item) => <ExtensionCard key={item.manifest.id} item={item} installed={installed.data?.items.find((row) => row.id === item.manifest.id && row.version === item.manifest.version)} revision={installed.data?.collection_revision} projectId={projectId} toast={toast} safe={safeMode.data?.enabled ?? true} />)}
    {Array.isArray(catalog.data) && catalog.data.length === 0 && <div className="sub">{t("extension.none")}</div>}
  </div>;
}
