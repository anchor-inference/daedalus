import { useCallback, useEffect, useRef, useState } from "react";
import { Excalidraw, exportToBlob, exportToSvg } from "@excalidraw/excalidraw";
import "@excalidraw/excalidraw/index.css";
import { latinKey } from "../navigation";
import { api } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { navigate, pathFor, useRoute } from "../router";
import { invalidate, prime, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { PageHeader } from "../ui/index";
import { diagramDiff } from "../diagram-diff";
import "./diagrams.css";

type Scene = { elements: any[]; appState: Record<string, unknown>; files: Record<string, unknown> };
type Diagram = { id: string; title: string; version: number; created_at: string; updated_at: string; scene: Scene; share_token?: string };
type DiagramItem = Omit<Diagram, "scene">;
type Revision = { version: number; title: string; saved_at: string; scene: Scene };
type RevisionItem = Omit<Revision, "scene">;

function download(data: Blob, filename: string) {
  const url = URL.createObjectURL(data);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Editor({ diagram, toast, returnPath, items, sessionId, onCreate, refreshDiagram }: { diagram: Diagram; toast: (message: string) => void; returnPath: string; items: DiagramItem[]; sessionId: string | null; onCreate: () => void; refreshDiagram: () => Promise<void> }) {
  const [title, setTitle] = useState(diagram.title);
  const [status, setStatus] = useState<"saved" | "editing" | "saving" | "conflict">("saved");
  const sceneRef = useRef<Scene>(diagram.scene);
  const signatureRef = useRef(JSON.stringify({ elements: diagram.scene.elements, files: diagram.scene.files }));
  const initializedRef = useRef(false);
  const titleRef = useRef(diagram.title);
  const versionRef = useRef(diagram.version);
  const pendingRef = useRef(false);
  const savingRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [sideOpen, setSideOpen] = useState(true);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [canvasVersion, setCanvasVersion] = useState(diagram.version);
  const remoteConflictRef = useRef(false);
  const [shareUrl, setShareUrl] = useState(diagram.share_token ? `/app/d/${diagram.share_token}` : "");
  const { data: versions, refresh: refreshVersions } = useQuery<RevisionItem[]>(`/api/diagrams/${diagram.id}/versions`, { pollMs: 5000, staleMs: 0 });
  const { data: preview } = useQuery<Revision>(selectedVersion === null ? null : `/api/diagrams/${diagram.id}/versions/${selectedVersion}`, { staleMs: 0 });
  const diff = preview ? diagramDiff(preview.scene.elements, diagram.scene.elements) : null;

  useEffect(() => {
    if (diagram.version <= versionRef.current) return;
    // A remote agent edit must not unmount a canvas with local unsaved strokes. Keep it
    // available for export and ask before replacing it with the newer stored version.
    if (pendingRef.current || savingRef.current) {
      remoteConflictRef.current = true;
      setStatus("conflict");
      return;
    }
    sceneRef.current = diagram.scene;
    signatureRef.current = JSON.stringify({ elements: diagram.scene.elements, files: diagram.scene.files });
    initializedRef.current = false;
    titleRef.current = diagram.title;
    setTitle(diagram.title);
    versionRef.current = diagram.version;
    setCanvasVersion(diagram.version);
  }, [diagram]);

  const flush = useCallback(async () => {
    if (savingRef.current || !pendingRef.current || remoteConflictRef.current) return;
    savingRef.current = true;
    try {
      while (pendingRef.current) {
        pendingRef.current = false;
        setStatus("saving");
        const saved = await api.put<Diagram>(`/api/diagrams/${diagram.id}`, { title: titleRef.current, scene: sceneRef.current, version: versionRef.current });
        versionRef.current = saved.version;
      }
      setStatus("saved");
      invalidate("/api/diagrams");
      void refreshVersions();
    } catch (exc) {
      pendingRef.current = true;
      remoteConflictRef.current = true;
      setStatus("conflict");
      toast(errorText(exc));
      void refreshDiagram();
    } finally { savingRef.current = false; }
  }, [diagram.id, toast, refreshVersions, refreshDiagram]);

  function showCurrent() {
    initializedRef.current = false;
    setSelectedVersion(null);
  }

  async function reloadRemote() {
    if (!(await confirmAsync(t("diagrams.reload"), { body: t("diagrams.reload.body"), action: t("diagrams.reload") }))) return;
    try {
      const latest = await api.get<Diagram>(`/api/diagrams/${diagram.id}`);
      if (timerRef.current) clearTimeout(timerRef.current);
      pendingRef.current = false;
      remoteConflictRef.current = false;
      sceneRef.current = latest.scene;
      signatureRef.current = JSON.stringify({ elements: latest.scene.elements, files: latest.scene.files });
      initializedRef.current = false;
      titleRef.current = latest.title;
      setTitle(latest.title);
      versionRef.current = latest.version;
      setCanvasVersion(latest.version);
      setStatus("saved");
      prime(`/api/diagrams/${diagram.id}`, latest);
    } catch (error) { toast(errorText(error)); }
  }

  async function share() {
    try {
      const result = await api.post<{ url: string }>(`/api/diagrams/${diagram.id}/share`, {});
      setShareUrl(result.url);
      invalidate(`/api/diagrams/${diagram.id}`);
      const url = `${window.location.origin}${result.url}`;
      if (navigator.share) await navigator.share({ title: titleRef.current, url });
      else { await navigator.clipboard.writeText(url); toast(t("diagrams.link.copied")); }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      toast(errorText(error));
    }
  }

  async function revoke() {
    if (!(await confirmAsync(t("diagrams.share.revoke"), { action: t("diagrams.share.revoke") }))) return;
    try {
      await api.delete(`/api/diagrams/${diagram.id}/share`);
      setShareUrl("");
      invalidate(`/api/diagrams/${diagram.id}`);
    } catch (error) { toast(errorText(error)); }
  }

  async function exportScene(format: "png" | "svg" | "excalidraw") {
    const scene = sceneRef.current;
    const name = titleRef.current.trim().replace(/[^\p{L}\p{N}._-]+/gu, "-").slice(0, 80) || "diagram";
    try {
      if (format === "excalidraw") {
        download(new Blob([JSON.stringify({ type: "excalidraw", version: 2, source: "Daedalus", ...scene })], { type: "application/json" }), `${name}.excalidraw`);
      } else if (format === "svg") {
        const svg = await exportToSvg({ elements: scene.elements as any, appState: scene.appState as any, files: scene.files as any });
        download(new Blob([new XMLSerializer().serializeToString(svg)], { type: "image/svg+xml" }), `${name}.svg`);
      } else {
        const blob = await exportToBlob({ elements: scene.elements as any, appState: scene.appState as any, files: scene.files as any, mimeType: "image/png" });
        download(blob, `${name}.png`);
      }
    } catch (error) { toast(errorText(error)); }
  }

  function changed() {
    pendingRef.current = true;
    if (remoteConflictRef.current) { setStatus("conflict"); return; }
    setStatus("editing");
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => void flush(), 1100);
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && latinKey(event) === "s") { event.preventDefault(); if (timerRef.current) clearTimeout(timerRef.current); void flush(); }
    };
    const onLeave = (event: BeforeUnloadEvent) => { if (pendingRef.current) event.preventDefault(); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("beforeunload", onLeave);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("beforeunload", onLeave); if (timerRef.current) clearTimeout(timerRef.current); };
  }, [flush]);

  return <div className="diagram-editor">
    <div className="diagram-bar"><button className="iconbtn" onClick={() => { if (pendingRef.current) void flush(); navigate(returnPath); }} aria-label={t("diagrams.back")}><Icon name="back" size={18} /></button>
      <input aria-label={t("diagrams.title")} value={title} maxLength={160} onChange={(event) => { setTitle(event.target.value); titleRef.current = event.target.value; changed(); }} />
      <span className="diagram-version">{t("diagrams.version", { n: versionRef.current })}</span>
      <span className={`diagram-status ${status}`}>{t(`diagrams.${status}`)}</span>
      {status === "conflict" && <button className="btn" onClick={() => void reloadRemote()}>{t("diagrams.reload")}</button>}
      <button className="btn" onClick={() => void share()}><Icon name="share" size={15} /> {t("diagrams.share")}</button>
      <select className="btn diagram-export" aria-label={t("diagrams.export")} value="" onChange={(event) => { const value = event.target.value as "png" | "svg" | "excalidraw"; event.target.value = ""; void exportScene(value); }}><option value="" disabled>{t("diagrams.export")}</option><option value="png">PNG</option><option value="svg">SVG</option><option value="excalidraw">{t("diagrams.export.editable")}</option></select>
      <button className="btn" onClick={() => { if (timerRef.current) clearTimeout(timerRef.current); void flush(); }} disabled={status === "saved" || status === "saving" || status === "conflict"}>{t("common.save")}</button>
      <button className="iconbtn diagram-side-toggle" onClick={() => setSideOpen((open) => !open)} aria-label={t("diagrams.sidebar")} aria-expanded={sideOpen}><Icon name="sidebar" size={18} /></button>
    </div>
    <div className="diagram-workspace">
      {sideOpen && <aside className="diagram-side" aria-label={t("diagrams.sidebar")}>
        <div className="diagram-side-heading"><strong>{t("nav.diagrams")}</strong><button className="iconbtn" onClick={onCreate} aria-label={t("diagrams.new")}><Icon name="plus" size={16} /></button></div>
        <div className="diagram-side-list">{items.map((item) => <button key={item.id} className={`diagram-side-item ${item.id === diagram.id ? "active" : ""}`} onClick={() => navigate(pathFor("diagrams", item.id, { session: sessionId }))}><Icon name="pen" size={15} /><span>{item.title}</span></button>)}</div>
        <div className="diagram-side-heading"><strong>{t("diagrams.history")}</strong></div>
        <div className="diagram-side-list"><button className={`diagram-side-item ${selectedVersion === null ? "active" : ""}`} onClick={showCurrent}>{t("diagrams.current", { n: versionRef.current })}</button>{versions?.map((item) => <button key={item.version} className={`diagram-side-item ${selectedVersion === item.version ? "active" : ""}`} disabled={status !== "saved"} onClick={() => setSelectedVersion(item.version)}>{t("diagrams.version", { n: item.version })}<small>{new Date(item.saved_at).toLocaleString()}</small></button>)}</div>
        {diff && <div className="diagram-diff"><strong>{t("diagrams.diff")}</strong><p>{t("diagrams.diff.counts", { added: diff.added.length, removed: diff.removed.length, changed: diff.changed.length })}</p>{preview?.title !== diagram.title && <p>{t("diagrams.diff.title", { from: preview?.title ?? "", to: diagram.title })}</p>}{diff.added.map((item) => <small key={`a-${item.id}`}>+ {item.type} · {item.id}</small>)}{diff.removed.map((item) => <small key={`r-${item.id}`}>− {item.type} · {item.id}</small>)}{diff.changed.map((item) => <small key={`c-${item.id}`}>~ {item.type} · {item.id}</small>)}</div>}
        {shareUrl && <div className="diagram-share-state"><span>{t("diagrams.share.active")}</span><input aria-label={t("diagrams.share.link")} readOnly value={`${window.location.origin}${shareUrl}`} onFocus={(event) => event.target.select()} /><button className="btn small" onClick={() => void revoke()}>{t("diagrams.share.revoke")}</button></div>}
      </aside>}
      <div className="diagram-canvas">{selectedVersion !== null && preview ? <><div className="diagram-preview-note">{t("diagrams.preview", { n: selectedVersion })}<button className="btn small" onClick={showCurrent}>{t("diagrams.return")}</button></div><Excalidraw key={`preview-${selectedVersion}`} viewModeEnabled initialData={{ elements: preview.scene.elements as any, appState: { ...preview.scene.appState, collaborators: new Map() } as any, files: preview.scene.files as any }} /></> : <Excalidraw key={canvasVersion} initialData={{ elements: sceneRef.current.elements as any, appState: { ...sceneRef.current.appState, collaborators: new Map() } as any, files: sceneRef.current.files as any }} onChange={(elements, appState, files) => {
      const scene = { elements: elements as any[], appState: { viewBackgroundColor: appState.viewBackgroundColor, gridSize: appState.gridSize }, files: files as Record<string, unknown> };
      // Excalidraw calls onChange while loading initialData and when the viewport moves.
      // Neither changes the document; saving either used to overwrite a newer agent edit.
      const signature = JSON.stringify({ elements: scene.elements, files: scene.files });
      if (!initializedRef.current) {
        initializedRef.current = true;
        signatureRef.current = signature;
        sceneRef.current = scene;
        return;
      }
      if (signatureRef.current === signature) return;
      signatureRef.current = signature;
      sceneRef.current = scene;
      changed();
    }} />}</div>
    </div>
  </div>;
}

export function DiagramsScreen({ toast, selected }: { toast: (message: string) => void; selected: string | null }) {
  const route = useRoute();
  const sessionId = route.query.get("session");
  const { data: items, loading, error, refresh } = useQuery<DiagramItem[]>(sessionId ? `/api/diagrams?session_id=${encodeURIComponent(sessionId)}` : "/api/diagrams", { pollMs: 5000, staleMs: 0 });
  const { data: diagram, error: detailError, refresh: refreshDiagram } = useQuery<Diagram>(selected ? `/api/diagrams/${selected}` : "/api/diagrams", { pollMs: selected ? 5000 : 0, staleMs: 0 });

  async function create() {
    try {
      const made = await api.post<Diagram>("/api/diagrams", { title: t("diagrams.untitled") });
      refresh(); navigate(pathFor("diagrams", made.id));
    } catch (exc) { toast(errorText(exc)); }
  }

  async function remove(item: DiagramItem) {
    if (!(await confirmAsync(t("diagrams.delete"), { body: item.title, action: t("common.delete") }))) return;
    try { await api.delete(`/api/diagrams/${item.id}`); refresh(); }
    catch (exc) { toast(errorText(exc)); }
  }

  if (selected) return diagram && "scene" in diagram ? <Editor key={selected} diagram={diagram} toast={toast} returnPath={pathFor("diagrams", null, { session: sessionId })} items={items ?? []} sessionId={sessionId} onCreate={() => void create()} refreshDiagram={refreshDiagram} /> : <div className="screen"><PageHeader title={t("nav.diagrams")} />{detailError || t("common.loading")}</div>;
  return <div className="screen diagrams-screen"><PageHeader title={sessionId ? t("diagrams.session") : t("nav.diagrams")} actions={<button className="btn primary" onClick={() => void create()}><Icon name="plus" size={16} /> {t("diagrams.new")}</button>} />
    {error && <div className="empty">{error}</div>}
    {loading && !items && <div className="empty">{t("common.loading")}</div>}
    {items?.length === 0 && <div className="empty">{t("diagrams.empty")}</div>}
    <div className="diagram-list">{(items ?? []).map((item) => <div className="diagram-row" key={item.id}><button onClick={() => navigate(pathFor("diagrams", item.id, { session: sessionId }))}><span className="diagram-thumb"><Icon name="pen" size={19} /></span><span><strong>{item.title}</strong><small>{new Date(item.updated_at).toLocaleString()}</small></span></button><button className="iconbtn" onClick={() => void remove(item)} aria-label={t("common.delete")}><Icon name="trash" size={16} /></button></div>)}</div>
  </div>;
}
