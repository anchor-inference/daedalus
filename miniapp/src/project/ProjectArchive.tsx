import { useRef, useState } from "react";
import { api, ApiError, Project } from "../api";
import { t } from "../i18n";
import { useOffline, useQuery } from "../store";
import { errorText } from "../ui";

type SelectedPath = { folder_id: string; path: string };
type ExportReceipt = { receipt_id: string; archive_artifact_id: string; archive_digest: string; format_version: number; size_bytes: number; selected_files: SelectedPath[]; row_counts: Record<string, number>; private: boolean };
type ImportPreview = { valid: boolean; archive_digest: string; format_version: number; row_counts: Record<string, number>; selected_files: SelectedPath[]; selected_file_count: number; capability_handles: string[]; collision: boolean; conflicts: string[]; missing_secrets: string[]; reconnect_required: string[]; restores_runtime: boolean };
type Upload = { archive_artifact_id: string; private: boolean; preview: ImportPreview };
type ApplyReceipt = { receipt_id: string; project_id: string; archive_digest: string; runtime_state: string };
type Intent<T> = { body: T };

function readIntent<T>(key: string): Intent<T> | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) ?? "null");
    return value && typeof value.body?.client_operation_id === "string" ? value as Intent<T> : null;
  } catch { return null; }
}

function keepIntent<T>(key: string, intent: Intent<T> | null): void {
  try {
    if (intent) sessionStorage.setItem(key, JSON.stringify(intent));
    else sessionStorage.removeItem(key);
  } catch { /* mounted state keeps the exact request for retry */ }
}

function archiveName(project: Project): string {
  return `${project.name.replace(/[^\p{L}\p{N}._-]+/gu, "-").slice(0, 60) || "workspace"}.zip`;
}

export function ProjectArchive({ project, toast, onChanged, onOpenRestored, readFailed }: { project: Project; toast: (message: string) => void; onChanged: () => void; onOpenRestored: (id: string) => void; readFailed: boolean }) {
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<SelectedPath[]>([]);
  const [folderId, setFolderId] = useState(project.folders[0]?.id ?? "");
  const [relativePath, setRelativePath] = useState("");
  const [receipt, setReceipt] = useState<ExportReceipt | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [applied, setApplied] = useState<ApplyReceipt | null>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const exportKey = `daedalus.archive.export.${project.id}`;
  const importKey = `daedalus.archive.import.${project.id}`;
  const exportIntent = useRef<Intent<{ expected_entity_revision: number; client_operation_id: string; selected_paths: SelectedPath[] }> | null>(readIntent(exportKey));
  const importIntent = useRef<Intent<{ expected_collection_revision: number; client_operation_id: string; archive_artifact_id: string }> | null>(readIntent(importKey));
  const offline = useOffline();
  const globalRevision = useQuery<{ collection_revision: number }>("/api/control/revisions", { staleMs: 0 });
  const latest = useQuery<{ latest: ExportReceipt | null; available: boolean }>(`/api/projects/${encodeURIComponent(project.id)}/workspace-archive`, { staleMs: 0 });
  const downloadReceipt = receipt ?? (latest.data?.available ? latest.data.latest : null);
  const readableFolders = project.folders.filter((folder) => folder.reachable && folder.reach === "agents");
  const canExport = !offline && !readFailed && !busy && Number.isInteger(project.entity_revision) && (project.entity_revision ?? 0) > 0;

  function addFile() {
    const path = relativePath.trim();
    if (!path || !folderId || selected.some((item) => item.folder_id === folderId && item.path === path)) return;
    setSelected([...selected, { folder_id: folderId, path }]);
    setRelativePath("");
    setReceipt(null);
  }

  async function exportNow(retry = false) {
    if (!canExport || (!retry && !!exportIntent.current)) return;
    const intent = retry ? exportIntent.current : { body: { expected_entity_revision: project.entity_revision!,
      client_operation_id: crypto.randomUUID(), selected_paths: selected } };
    if (!intent) return;
    exportIntent.current = intent;
    keepIntent(exportKey, intent);
    setBusy(true);
    setWarning(null);
    try {
      const result = await api.post<ExportReceipt>(`/api/projects/${encodeURIComponent(project.id)}/workspace-archive`, intent.body);
      if (!result.receipt_id || !result.archive_artifact_id || result.private !== true) throw new Error(t("archive.badReceipt"));
      exportIntent.current = null;
      keepIntent(exportKey, null);
      setReceipt(result);
      latest.refresh();
      onChanged();
      toast(t("archive.ready"));
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        exportIntent.current = null;
        keepIntent(exportKey, null);
      }
      setWarning(errorText(error));
    } finally { setBusy(false); }
  }

  async function download() {
    if (!downloadReceipt || offline || busy) return;
    setBusy(true);
    try {
      const blob = await api.archiveDownload(downloadReceipt.archive_artifact_id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = archiveName(project);
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (error) { setWarning(errorText(error)); }
    finally { setBusy(false); }
  }

  async function inspect(file: File | undefined) {
    if (!file || offline || busy) return;
    setBusy(true);
    setWarning(null);
    setUpload(null);
    setApplied(null);
    try {
      const result = await api.archiveUpload<Upload>(file);
      if (!result.private || !result.archive_artifact_id || !result.preview) throw new Error(t("archive.badReceipt"));
      setUpload(result);
      globalRevision.refresh();
    } catch (error) { setWarning(errorText(error)); }
    finally { setBusy(false); }
  }

  async function apply(retry = false) {
    const revision = globalRevision.data?.collection_revision;
    if (offline || busy || (!retry && (!upload?.preview.valid || !Number.isInteger(revision)))) return;
    const intent = retry ? importIntent.current : { body: { archive_artifact_id: upload!.archive_artifact_id,
      expected_collection_revision: revision!, client_operation_id: crypto.randomUUID() } };
    if (!intent) return;
    importIntent.current = intent;
    keepIntent(importKey, intent);
    setBusy(true);
    setWarning(null);
    try {
      const result = await api.post<ApplyReceipt>("/api/import/apply", intent.body);
      if (!result.receipt_id || !result.project_id || result.runtime_state !== "inactive") throw new Error(t("archive.badReceipt"));
      importIntent.current = null;
      keepIntent(importKey, null);
      setApplied(result);
      globalRevision.refresh();
      onChanged();
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        importIntent.current = null;
        keepIntent(importKey, null);
        globalRevision.refresh();
      }
      setWarning(errorText(error));
    } finally { setBusy(false); }
  }

  return <details className="sheet-section">
    <summary>{t("archive.title")}</summary>
    <div className="project-extension-list">
      <p className="sub">{t("archive.intro")}</p>
      <div className="sub">{t("archive.default")}</div>
      <details><summary>{t("archive.files.title")}</summary>
        <p className="sub">{t("archive.files.hint")}</p>
        {readableFolders.length > 0 ? <>
          <label className="field" htmlFor="archive-folder">{t("archive.folder")}</label>
          <select id="archive-folder" className="field" value={folderId} onChange={(event) => setFolderId(event.target.value)}>
            {readableFolders.map((folder) => <option key={folder.id} value={folder.id}>{folder.label || t("archive.folder.default")}</option>)}
          </select>
          <label className="field" htmlFor="archive-relative-path">{t("archive.relativePath")}</label>
          <input id="archive-relative-path" className="field" value={relativePath} onChange={(event) => setRelativePath(event.target.value)} placeholder={t("archive.pathExample")} />
          <button type="button" className="btn small" onClick={addFile} disabled={!relativePath.trim() || !folderId}>{t("archive.files.add")}</button>
        </> : <div className="result-warning">{t("archive.files.unavailable")}</div>}
        {selected.length > 0 && <ul className="plain-list">{selected.map((item) => <li key={`${item.folder_id}:${item.path}`}>{item.path} <button type="button" className="linkbtn" onClick={() => setSelected(selected.filter((row) => row !== item))}>{t("common.remove")}</button></li>)}</ul>}
      </details>
      <button type="button" className="btn small" disabled={!canExport || !!exportIntent.current} onClick={() => void exportNow()}>{t("archive.export")}</button>
      {exportIntent.current && <div className="result-warning" role="status">{t("archive.pending")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void exportNow(true)}>{t("common.retry")}</button></div>}
      {downloadReceipt && <div className="sub" role="status">{t("archive.receipt", { count: downloadReceipt.selected_files.length, bytes: downloadReceipt.size_bytes })} <button type="button" className="btn small" disabled={offline || busy} onClick={() => void download()}>{t("archive.download")}</button><details><summary>{t("archive.details")}</summary><code style={{ overflowWrap: "anywhere" }}>{downloadReceipt.archive_digest}</code></details></div>}
      {latest.data?.latest && !latest.data.available && <div className="result-warning" role="status">{t("archive.fileMissing")}</div>}
      <label className="field" htmlFor="archive-import">{t("archive.import")}</label>
      <input id="archive-import" type="file" accept=".zip,application/zip" disabled={offline || busy} onChange={(event) => void inspect(event.target.files?.[0])} />
      {upload && <div className="sub" role="status">
        <div>{t("archive.preview", { count: upload.preview.selected_file_count, rows: Object.values(upload.preview.row_counts).reduce((sum, n) => sum + n, 0) })}</div>
        <div>{t("archive.inactive")}</div>
        <div>{t("archive.reconnect")}: {upload.preview.reconnect_required.join(" · ")}</div>
        {upload.preview.missing_secrets.length > 0 && <div>{t("archive.secrets")}: {upload.preview.missing_secrets.join(" · ")}</div>}
        {upload.preview.capability_handles?.length > 0 && <div>{t("archive.capabilities")}: {upload.preview.capability_handles.join(" · ")}</div>}
        {!upload.preview.valid && <div className="result-warning">{upload.preview.conflicts.join(" · ") || t("archive.invalid")}</div>}
        {upload.preview.valid && <button type="button" className="btn small" disabled={offline || busy || !Number.isInteger(globalRevision.data?.collection_revision) || !!globalRevision.error || !!importIntent.current} onClick={() => void apply()}>{t("archive.restore")}</button>}
      </div>}
      {importIntent.current && <div className="result-warning" role="status">{t("archive.pending")} <button type="button" className="linkbtn" disabled={offline || busy} onClick={() => void apply(true)}>{t("common.retry")}</button></div>}
      {applied && <div className="sub" role="status">{t("archive.restored")} <button type="button" className="btn small" onClick={() => onOpenRestored(applied.project_id)}>{t("archive.openRestored")}</button></div>}
      {warning && <div className="result-warning" role="status">{warning}</div>}
      {(offline || globalRevision.error || latest.error) && <div className="result-warning" role="status">{t("archive.unconfirmed")}</div>}
    </div>
  </details>;
}
