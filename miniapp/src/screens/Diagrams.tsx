import { useCallback, useEffect, useRef, useState } from "react";
import { Excalidraw } from "@excalidraw/excalidraw";
import "@excalidraw/excalidraw/index.css";
import { api } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { navigate, pathFor } from "../router";
import { invalidate, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { PageHeader } from "../ui/index";
import "./diagrams.css";

type Scene = { elements: any[]; appState: Record<string, unknown>; files: Record<string, unknown> };
type Diagram = { id: string; title: string; version: number; created_at: string; updated_at: string; scene: Scene };
type DiagramItem = Omit<Diagram, "scene">;

function Editor({ diagram, toast }: { diagram: Diagram; toast: (message: string) => void }) {
  const [title, setTitle] = useState(diagram.title);
  const [status, setStatus] = useState<"saved" | "editing" | "saving" | "conflict">("saved");
  const sceneRef = useRef<Scene>(diagram.scene);
  const signatureRef = useRef("");
  const titleRef = useRef(diagram.title);
  const versionRef = useRef(diagram.version);
  const pendingRef = useRef(false);
  const savingRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = useCallback(async () => {
    if (savingRef.current || !pendingRef.current) return;
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
    } catch (exc) {
      pendingRef.current = true;
      setStatus("conflict");
      toast(errorText(exc));
    } finally { savingRef.current = false; }
  }, [diagram.id, toast]);

  function changed() {
    pendingRef.current = true;
    setStatus("editing");
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => void flush(), 1100);
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); if (timerRef.current) clearTimeout(timerRef.current); void flush(); }
    };
    const onLeave = (event: BeforeUnloadEvent) => { if (pendingRef.current) event.preventDefault(); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("beforeunload", onLeave);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("beforeunload", onLeave); if (timerRef.current) clearTimeout(timerRef.current); };
  }, [flush]);

  return <div className="diagram-editor">
    <div className="diagram-bar"><button className="iconbtn" onClick={() => { if (pendingRef.current) void flush(); navigate(pathFor("diagrams")); }} aria-label={t("diagrams.back")}><Icon name="back" size={18} /></button>
      <input aria-label={t("diagrams.title")} value={title} maxLength={160} onChange={(event) => { setTitle(event.target.value); titleRef.current = event.target.value; changed(); }} />
      <span className={`diagram-status ${status}`}>{t(`diagrams.${status}`)}</span>
      <button className="btn" onClick={() => { if (timerRef.current) clearTimeout(timerRef.current); void flush(); }} disabled={status === "saved" || status === "saving"}>{t("common.save")}</button>
    </div>
    <div className="diagram-canvas"><Excalidraw initialData={{ elements: diagram.scene.elements as any, appState: { ...diagram.scene.appState, collaborators: new Map() } as any, files: diagram.scene.files as any }} onChange={(elements, appState, files) => {
      const scene = { elements: elements as any[], appState: { viewBackgroundColor: appState.viewBackgroundColor, gridSize: appState.gridSize }, files: files as Record<string, unknown> };
      const signature = JSON.stringify(scene);
      if (signatureRef.current === signature) return;
      signatureRef.current = signature;
      sceneRef.current = scene;
      changed();
    }} /></div>
  </div>;
}

export function DiagramsScreen({ toast, selected }: { toast: (message: string) => void; selected: string | null }) {
  const { data: items, loading, error, refresh } = useQuery<DiagramItem[]>("/api/diagrams", { pollMs: 30000, staleMs: 5000 });
  const { data: diagram, error: detailError } = useQuery<Diagram>(selected ? `/api/diagrams/${selected}` : "/api/diagrams", { staleMs: 5000 });

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

  if (selected) return diagram && "scene" in diagram ? <Editor key={selected} diagram={diagram} toast={toast} /> : <div className="screen"><PageHeader title={t("nav.diagrams")} />{detailError || t("common.loading")}</div>;
  return <div className="screen diagrams-screen"><PageHeader title={t("nav.diagrams")} actions={<button className="btn primary" onClick={() => void create()}><Icon name="plus" size={16} /> {t("diagrams.new")}</button>} />
    {error && <div className="empty">{error}</div>}
    {loading && !items && <div className="empty">{t("common.loading")}</div>}
    {items?.length === 0 && <div className="empty">{t("diagrams.empty")}</div>}
    <div className="diagram-list">{(items ?? []).map((item) => <div className="diagram-row" key={item.id}><button onClick={() => navigate(pathFor("diagrams", item.id))}><span className="diagram-thumb"><Icon name="pen" size={19} /></span><span><strong>{item.title}</strong><small>{new Date(item.updated_at).toLocaleString()}</small></span></button><button className="iconbtn" onClick={() => void remove(item)} aria-label={t("common.delete")}><Icon name="trash" size={16} /></button></div>)}</div>
  </div>;
}
