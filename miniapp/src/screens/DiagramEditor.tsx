// The diagram editor: the canvas fills the window under one compact bar (back, the title, whether it
// is saved, history, sharing, export), and nothing sits beside it. The history opens as a drawer on
// the right on a desktop and as a sheet from the bottom on a phone, closed until asked for.
//
// Saving is automatic, a second after the last stroke, and carries the version the editor last saw;
// a save on top of someone else's newer version is refused by the server rather than merged. The
// editor learns of the agent's edits by polling a small head record and fetches the scene only when
// the version moved: a canvas with no unsaved strokes takes the new scene in place (keeping the
// viewport), one with unsaved strokes keeps them and asks.

import { useCallback, useEffect, useRef, useState } from "react";
import { CaptureUpdateAction, Excalidraw, MainMenu, exportToBlob, exportToClipboard, exportToSvg, restoreElements } from "@excalidraw/excalidraw";
import type { ExcalidrawImperativeAPI } from "@excalidraw/excalidraw/types";
import "@excalidraw/excalidraw/index.css";
import { api, ApiError } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { back, navigate, pathFor } from "../router";
import { invalidate } from "../store";
import { errorText } from "../ui";
import { OverflowMenu, Popover, Sheet, useMedia } from "../ui/index";
import type { MenuItem } from "../ui/dialogs";
import { copyText } from "../ui/components";
import { changeSummary, clockTime, groupByDay, type RevisionItem } from "../diagram-history";
import { relTimeLong } from "../format";
import { DiagramThumb, UI_OPTIONS, download, fileName, exposeView, fitOnOpen, sceneSignature, useExcalidrawLang, useScheme, type Diagram, type Head, type Scene } from "./diagramparts";
import "./diagrams.css";

type Status = "saved" | "editing" | "saving" | "offline" | "conflict";
type Revision = RevisionItem & { scene: Scene };

const SAVE_DELAY_MS = 1100;
const HEAD_POLL_MS = 4000;

/** Whether a failed request never reached the server (the network, not a refusal). */
function unreachable(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status >= 500;
}

export function DiagramEditor({ id, sessionId, toast }: { id: string; sessionId: string | null; toast: (message: string) => void }) {
  const [doc, setDoc] = useState<Diagram | null>(null);
  const [failure, setFailure] = useState("");
  const listPath = pathFor("diagrams", null, { session: sessionId });
  useEffect(() => {
    let active = true;
    setDoc(null);
    setFailure("");
    api.get<Diagram>(`/api/diagrams/${id}`).then((found) => { if (active) setDoc(found); }).catch((error) => { if (active) setFailure(errorText(error)); });
    return () => { active = false; };
  }, [id]);
  if (!doc) {
    return (
      <div className="diagram-editor">
        <div className="diagram-bar"><button className="iconbtn" onClick={() => back(listPath)} aria-label={t("diagrams.back")} title={t("diagrams.back")}><Icon name="back" size={18} /></button></div>
        <div className="diagram-loading">{failure || t("common.loading")}</div>
      </div>
    );
  }
  return <Editor key={doc.id} initial={doc} listPath={listPath} toast={toast} />;
}

function Editor({ initial, listPath, toast }: { initial: Diagram; listPath: string; toast: (message: string) => void }) {
  const id = initial.id;
  const phone = useMedia("(max-width: 680px)");
  const theme = useScheme();
  const langCode = useExcalidrawLang();
  const [title, setTitle] = useState(initial.title);
  const [status, setStatus] = useState<Status>("saved");
  const [remote, setRemote] = useState<Head | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [shareToken, setShareToken] = useState(initial.share_token ?? "");
  const [shareOpen, setShareOpen] = useState(false);
  const [version, setVersion] = useState(initial.version);
  const shareButton = useRef<HTMLButtonElement>(null);
  const canvasBox = useRef<HTMLDivElement>(null);
  const excalidraw = useRef<ExcalidrawImperativeAPI | null>(null);
  const sceneRef = useRef<Scene>(initial.scene);
  const signatureRef = useRef("");
  const initializedRef = useRef(false);
  const titleRef = useRef(initial.title);
  const versionRef = useRef(initial.version);
  const pendingRef = useRef(false);
  const savingRef = useRef(false);
  const conflictRef = useRef(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const historyRefresh = useRef<() => void>(() => undefined);
  const registerHistory = useCallback((refresh: () => void) => { historyRefresh.current = refresh; }, []);

  const setCurrentVersion = (next: number) => {
    versionRef.current = next;
    setVersion(next);
  };

  const flush = useCallback(async () => {
    if (savingRef.current || !pendingRef.current || conflictRef.current) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    savingRef.current = true;
    try {
      while (pendingRef.current && !conflictRef.current) {
        pendingRef.current = false;
        setStatus("saving");
        const saved = await api.put<Diagram>(`/api/diagrams/${id}`, { title: titleRef.current.trim() || t("diagrams.untitled"), scene: sceneRef.current, version: versionRef.current });
        setCurrentVersion(saved.version);
      }
      if (!conflictRef.current) setStatus("saved");
      invalidate("/api/diagrams");
      historyRefresh.current();
    } catch (error) {
      pendingRef.current = true;
      if (error instanceof ApiError && error.status === 409) {
        conflictRef.current = true;
        setStatus("conflict");
        api.get<Head>(`/api/diagrams/${id}/head`).then(setRemote).catch(() => undefined);
      } else if (unreachable(error)) {
        // Kept, not lost: the strokes stay pending and go out with the next attempt.
        setStatus("offline");
        timerRef.current = setTimeout(() => void flush(), 5000);
      } else {
        setStatus("editing");
        toast(errorText(error));
      }
    } finally {
      savingRef.current = false;
    }
  }, [id, toast]);

  const changed = useCallback(() => {
    pendingRef.current = true;
    if (conflictRef.current) return;
    setStatus("editing");
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => void flush(), SAVE_DELAY_MS);
  }, [flush]);

  /** Put a stored diagram on the canvas in place: the viewport and the undo history stay. */
  const apply = useCallback((next: Diagram) => {
    sceneRef.current = next.scene;
    titleRef.current = next.title;
    setTitle(next.title);
    setCurrentVersion(next.version);
    setShareToken(next.share_token ?? "");
    const canvas = excalidraw.current;
    if (!canvas) {
      initializedRef.current = false;
      return;
    }
    const files = Object.values(next.scene.files ?? {}) as any[];
    if (files.length) canvas.addFiles(files);
    canvas.updateScene({ elements: restoreElements(next.scene.elements as any, null, { repairBindings: true }), captureUpdate: CaptureUpdateAction.NEVER });
    // The baseline is what the canvas now holds, not what the server sent: restoring fills in fields,
    // and comparing against the raw scene read that as the operator's edit and saved it straight back.
    signatureRef.current = sceneSignature(canvas.getSceneElementsIncludingDeleted(), canvas.getFiles());
  }, []);

  const loadLatest = useCallback(async () => {
    try {
      const latest = await api.get<Diagram>(`/api/diagrams/${id}`);
      if (timerRef.current) clearTimeout(timerRef.current);
      pendingRef.current = false;
      conflictRef.current = false;
      setRemote(null);
      apply(latest);
      setStatus("saved");
      historyRefresh.current();
    } catch (error) { toast(errorText(error)); }
  }, [apply, id, toast]);

  /** Keep the strokes on screen and save them over the newer version, which stays in the history. */
  const keepMine = useCallback(async () => {
    try {
      const head = await api.get<Head>(`/api/diagrams/${id}/head`);
      setCurrentVersion(head.version);
      conflictRef.current = false;
      pendingRef.current = true;
      setRemote(null);
      await flush();
    } catch (error) { toast(errorText(error)); }
  }, [flush, id, toast]);

  // The head poll: a few bytes every few seconds while the page is visible, the scene only on a change.
  useEffect(() => {
    let active = true;
    const tick = async () => {
      if (document.visibilityState !== "visible") return;
      let head: Head;
      try {
        head = await api.get<Head>(`/api/diagrams/${id}/head`);
      } catch {
        return;
      }
      // A save of ours in flight is about to answer with this very version; a real conflict
      // surfaces as that save's refusal instead.
      if (!active || savingRef.current || head.version <= versionRef.current) return;
      historyRefresh.current();
      if (pendingRef.current || conflictRef.current) {
        conflictRef.current = true;
        if (timerRef.current) clearTimeout(timerRef.current);
        setRemote(head);
        setStatus("conflict");
        return;
      }
      try {
        const latest = await api.get<Diagram>(`/api/diagrams/${id}`);
        if (active && !pendingRef.current && !savingRef.current && latest.version > versionRef.current) apply(latest);
      } catch { /* the next tick tries again */ }
    };
    const timer = window.setInterval(() => void tick(), HEAD_POLL_MS);
    const onVisible = () => void tick();
    document.addEventListener("visibilitychange", onVisible);
    return () => { active = false; window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [apply, id]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        void flush();
      }
    };
    const onLeave = (event: BeforeUnloadEvent) => { if (pendingRef.current) event.preventDefault(); };
    const onOnline = () => { if (pendingRef.current) void flush(); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("beforeunload", onLeave);
    window.addEventListener("online", onOnline);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("beforeunload", onLeave);
      window.removeEventListener("online", onOnline);
      // Leaving with strokes still waiting for their timer: send them now rather than drop them.
      if (pendingRef.current && !conflictRef.current) void flush();
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [flush]);

  const leave = () => {
    void flush();
    back(listPath);
  };

  function currentScene() {
    const canvas = excalidraw.current;
    if (!canvas) return { elements: sceneRef.current.elements, appState: sceneRef.current.appState, files: sceneRef.current.files };
    return { elements: canvas.getSceneElements(), appState: { ...canvas.getAppState(), exportBackground: true, exportWithDarkMode: false }, files: canvas.getFiles() };
  }

  async function exportAs(format: "png" | "svg" | "excalidraw" | "clipboard") {
    const scene = currentScene();
    const name = fileName(titleRef.current);
    try {
      if (format === "excalidraw") {
        const json = JSON.stringify({ type: "excalidraw", version: 2, source: "Daedalus", elements: scene.elements, appState: { viewBackgroundColor: (scene.appState as any).viewBackgroundColor, gridSize: (scene.appState as any).gridSize ?? null }, files: scene.files });
        download(new Blob([json], { type: "application/json" }), `${name}.excalidraw`);
      } else if (format === "svg") {
        const svg = await exportToSvg({ elements: scene.elements as any, appState: scene.appState as any, files: scene.files as any, exportPadding: 16 });
        download(new Blob([new XMLSerializer().serializeToString(svg)], { type: "image/svg+xml" }), `${name}.svg`);
      } else if (format === "png") {
        download(await exportToBlob({ elements: scene.elements as any, appState: scene.appState as any, files: scene.files as any, mimeType: "image/png", exportPadding: 16 }), `${name}.png`);
      } else {
        await exportToClipboard({ elements: scene.elements as any, appState: scene.appState as any, files: scene.files as any, type: "png" });
        toast(t("diagrams.export.copied"));
      }
    } catch (error) { toast(format === "clipboard" ? t("diagrams.export.copy.failed") : errorText(error)); }
  }

  const exportItems: MenuItem[] = [
    { label: t("diagrams.export.png"), icon: "image", onSelect: () => void exportAs("png") },
    { label: t("diagrams.export.svg"), icon: "file", onSelect: () => void exportAs("svg") },
    { label: t("diagrams.export.editable"), icon: "braces", onSelect: () => void exportAs("excalidraw") },
    { label: t("diagrams.export.copy"), icon: "copy", onSelect: () => void exportAs("clipboard") },
  ];

  async function restore(revision: number) {
    if (savingRef.current || pendingRef.current) await flush();
    try {
      const restored = await api.post<Diagram>(`/api/diagrams/${id}/restore`, { revision, version: versionRef.current });
      apply(restored);
      setSelected(null);
      setStatus("saved");
      invalidate("/api/diagrams");
      historyRefresh.current();
      toast(t("diagrams.restored"));
    } catch (error) {
      toast(error instanceof ApiError && error.status === 409 ? t("diagrams.restore.stale") : errorText(error));
    }
  }

  const statusIcon = status === "saved" ? "check" : status === "conflict" || status === "offline" ? "alert" : "dot";
  const sharePanel = <SharePanel id={id} title={title} token={shareToken} onToken={setShareToken} toast={toast} />;
  const previewing = selected !== null;

  return (
    <div className={`diagram-editor ${historyOpen ? "history-open" : ""} ${previewing ? "previewing" : ""}`}>
      <header className="diagram-bar">
        <button className="iconbtn" onClick={leave} aria-label={t("diagrams.back")} title={t("diagrams.back")}><Icon name="back" size={18} /></button>
        <input className="diagram-title" aria-label={t("diagrams.title")} value={title} maxLength={160} onChange={(event) => { setTitle(event.target.value); titleRef.current = event.target.value; changed(); }} onKeyDown={(event) => { if (event.key === "Enter") (event.target as HTMLInputElement).blur(); }} />
        <span className={`diagram-status ${status}`} role="status" aria-label={t(`diagrams.status.${status}`)} title={status === "saved" ? t("diagrams.status.saved.version", { n: version }) : t(`diagrams.status.${status}`)}>
          <Icon name={statusIcon} size={14} /><span className="diagram-status-word">{t(`diagrams.status.${status}`)}</span>
        </span>
        <button className={`btn diagram-tool ${historyOpen ? "on" : ""}`} onClick={() => { setHistoryOpen((open) => !open); setSelected(null); }} aria-pressed={historyOpen} aria-label={t("diagrams.history")} title={t("diagrams.history")}>
          <Icon name="clock" size={16} /><span className="diagram-tool-word">{t("diagrams.history")}</span>
        </button>
        {!phone && (
          <button ref={shareButton} className={`btn diagram-tool ${shareOpen ? "on" : ""}`} onClick={() => setShareOpen((open) => !open)} aria-haspopup="dialog" aria-expanded={shareOpen} aria-label={t("diagrams.share")}>
            <Icon name={shareToken ? "link" : "share"} size={16} /><span className="diagram-tool-word">{t("diagrams.share")}</span>
          </button>
        )}
        {!phone && <OverflowMenu className="btn diagram-tool" label={t("diagrams.export")} items={exportItems} trigger={<><Icon name="download" size={16} /><span className="diagram-tool-word">{t("diagrams.export")}</span><Icon name="down" size={14} /></>} />}
        {phone && <OverflowMenu label={t("diagrams.more")} items={[{ label: t("diagrams.share"), icon: "share", onSelect: () => setShareOpen(true) }, "-", ...exportItems]} />}
      </header>
      {status === "conflict" && (
        <div className="diagram-banner conflict" role="alert">
          <Icon name="alert" size={16} />
          <span>{remote?.updated_by === "agent" ? t("diagrams.conflict.agent") : t("diagrams.conflict.other")}</span>
          <button className="btn small" onClick={() => void keepMine()}>{t("diagrams.conflict.keep")}</button>
          <button className="btn small primary" onClick={() => void loadLatest()}>{t("diagrams.reload")}</button>
        </div>
      )}
      <div className="diagram-stage">
        <div ref={canvasBox} className="diagram-canvas">
          <Excalidraw
            excalidrawAPI={(handle) => { excalidraw.current = handle; fitOnOpen(handle); exposeView(handle, canvasBox.current); }}
            initialData={{ elements: initial.scene.elements as any, appState: { ...initial.scene.appState, collaborators: new Map() } as any, files: initial.scene.files as any, scrollToContent: true }}
            theme={theme}
            langCode={langCode}
            UIOptions={UI_OPTIONS}
            name={title}
            onChange={(elements, appState, files) => {
              const scene = { elements: (elements as any[]).filter((element) => !element.isDeleted), appState: { viewBackgroundColor: appState.viewBackgroundColor, gridSize: appState.gridSize ?? null }, files: files as Record<string, unknown> };
              // Excalidraw calls this while loading the scene and whenever the viewport moves. Neither
              // changes the document, and saving either used to overwrite a newer agent edit.
              const signature = sceneSignature(elements as any[], files as Record<string, unknown>);
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
            }}
          >
            <MainMenu>
              <MainMenu.DefaultItems.SearchMenu />
              <MainMenu.DefaultItems.Help />
              <MainMenu.DefaultItems.ClearCanvas />
              <MainMenu.Separator />
              <MainMenu.DefaultItems.ChangeCanvasBackground />
            </MainMenu>
          </Excalidraw>
          {selected !== null && <RevisionPreview id={id} version={selected} theme={theme} langCode={langCode} onRestore={() => void restore(selected)} onClose={() => setSelected(null)} current={selected === version} />}
        </div>
        {historyOpen && (
          <HistoryPanel
            id={id}
            current={version}
            selected={selected}
            phone={phone}
            hidden={phone && previewing}
            onSelect={(next) => setSelected(next === version ? null : next)}
            onClose={() => { setHistoryOpen(false); setSelected(null); }}
            register={registerHistory}
          />
        )}
      </div>
      {shareOpen && !phone && <Popover anchor={shareButton.current} onClose={() => setShareOpen(false)} align="right" className="diagram-share-pop" label={t("diagrams.share")}>{sharePanel}</Popover>}
      {shareOpen && phone && <Sheet title={t("diagrams.share")} onClose={() => setShareOpen(false)} size="narrow">{sharePanel}</Sheet>}
    </div>
  );
}

function SharePanel({ id, title, token, onToken, toast }: { id: string; title: string; token: string; onToken: (token: string) => void; toast: (message: string) => void }) {
  const [busy, setBusy] = useState(false);
  const url = token ? `${window.location.origin}/app/d/${token}` : "";
  async function create() {
    setBusy(true);
    try {
      const result = await api.post<{ url: string }>(`/api/diagrams/${id}/share`, {});
      onToken(result.url.split("/").pop() ?? "");
      invalidate("/api/diagrams");
      if (await copyText(`${window.location.origin}${result.url}`)) toast(t("diagrams.link.copied"));
    } catch (error) { toast(errorText(error)); } finally { setBusy(false); }
  }
  async function revoke() {
    setBusy(true);
    try {
      await api.delete(`/api/diagrams/${id}/share`);
      onToken("");
      invalidate("/api/diagrams");
      toast(t("diagrams.share.revoked"));
    } catch (error) { toast(errorText(error)); } finally { setBusy(false); }
  }
  return (
    <div className="diagram-share">
      <div className="diagram-share-head">
        <Icon name={token ? "link" : "lock"} size={16} />
        <strong>{token ? t("diagrams.share.on") : t("diagrams.share.off")}</strong>
      </div>
      <p>{token ? t("diagrams.share.on.body") : t("diagrams.share.off.body")}</p>
      {token ? (
        <>
          <input className="field" aria-label={t("diagrams.share.link")} readOnly value={url} onFocus={(event) => event.target.select()} />
          <div className="diagram-share-actions">
            <button className="btn primary" onClick={() => void copyText(url).then((ok) => toast(ok ? t("diagrams.link.copied") : t("diagrams.link.copy.failed")))}><Icon name="copy" size={15} />{t("diagrams.share.copy")}</button>
            {typeof navigator.share === "function" && <button className="btn" onClick={() => void navigator.share({ title, url }).catch(() => undefined)}><Icon name="share" size={15} />{t("diagrams.share.send")}</button>}
            <button className="btn danger" disabled={busy} onClick={() => void revoke()}><Icon name="unlink" size={15} />{t("diagrams.share.revoke")}</button>
          </div>
        </>
      ) : (
        <div className="diagram-share-actions"><button className="btn primary" disabled={busy} onClick={() => void create()}><Icon name="link" size={15} />{t("diagrams.share.create")}</button></div>
      )}
    </div>
  );
}

function HistoryPanel({ id, current, selected, phone, hidden, onSelect, onClose, register }: { id: string; current: number; selected: number | null; phone: boolean; hidden: boolean; onSelect: (version: number) => void; onClose: () => void; register: (refresh: () => void) => void }) {
  const [items, setItems] = useState<RevisionItem[] | null>(null);
  const [failed, setFailed] = useState("");
  const load = useCallback(() => {
    api.get<RevisionItem[]>(`/api/diagrams/${id}/versions`).then((list) => { setItems(list); setFailed(""); }).catch((error) => setFailed(errorText(error)));
  }, [id]);
  useEffect(() => {
    load();
    register(load);
    return () => register(() => undefined);
  }, [load, register]);
  const body = (
    <>
      {failed && <div className="empty">{failed}</div>}
      {!items && !failed && <div className="empty">{t("common.loading")}</div>}
      {items && groupByDay(items).map((group) => (
        <section key={group.key} className="diagram-history-day">
          <h4>{group.label}</h4>
          {group.items.map((item) => (
            <button key={item.version} className={`diagram-revision ${selected === item.version || (selected === null && item.version === current) ? "active" : ""}`} onClick={() => onSelect(item.version)} aria-current={item.version === current ? "true" : undefined}>
              <DiagramThumb path={`/api/diagrams/${id}/versions/${item.version}/preview`} version={item.version} />
              <span className="diagram-revision-text">
                <span className="diagram-revision-top">
                  <time dateTime={item.saved_at} title={relTimeLong(item.saved_at)}>{clockTime(item.saved_at)}</time>
                  <span className={`diagram-who ${item.source}`}><Icon name={item.source === "agent" ? "bots" : "user"} size={12} />{t(item.source === "agent" ? "diagrams.who.agent" : "diagrams.who.you")}</span>
                  {item.version === current && <span className="diagram-current">{t("diagrams.current")}</span>}
                </span>
                <span className="diagram-revision-summary">{changeSummary(item.summary, item.kind)}</span>
              </span>
            </button>
          ))}
        </section>
      ))}
    </>
  );
  if (phone) {
    if (hidden) return null;
    return <Sheet title={t("diagrams.history")} onClose={onClose} className="diagram-history-sheet">{body}</Sheet>;
  }
  return (
    <aside className="diagram-history" aria-label={t("diagrams.history")}>
      <div className="diagram-history-head">
        <strong>{t("diagrams.history")}</strong>
        <button className="iconbtn small" onClick={onClose} aria-label={t("common.close")} title={t("common.close")}><Icon name="close" size={16} /></button>
      </div>
      <div className="diagram-history-body">{body}</div>
    </aside>
  );
}

function RevisionPreview({ id, version, theme, langCode, current, onRestore, onClose }: { id: string; version: number; theme: "light" | "dark"; langCode: string; current: boolean; onRestore: () => void; onClose: () => void }) {
  const [revision, setRevision] = useState<Revision | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let active = true;
    setRevision(null);
    api.get<Revision>(`/api/diagrams/${id}/versions/${version}`).then((found) => { if (active) setRevision(found); }).catch(() => undefined);
    return () => { active = false; };
  }, [id, version]);
  return (
    <div className="diagram-preview" aria-label={t("diagrams.preview.label")}>
      <div className="diagram-banner preview" role="status">
        <Icon name="eye" size={16} />
        <span>{revision ? t("diagrams.preview.at", { time: relTimeLong(revision.saved_at) }) : t("common.loading")}</span>
        <button className="btn small" onClick={onClose}>{t("diagrams.return")}</button>
        {!current && <button className="btn small primary" disabled={!revision} onClick={onRestore}><Icon name="undo" size={14} />{t("diagrams.restore")}</button>}
      </div>
      <div ref={box} className="diagram-preview-canvas">{revision && (
        <Excalidraw
          key={version}
          excalidrawAPI={(handle) => { fitOnOpen(handle); exposeView(handle, box.current); }}
          viewModeEnabled
          theme={theme}
          langCode={langCode}
          UIOptions={UI_OPTIONS}
          initialData={{ elements: revision.scene.elements as any, appState: { ...revision.scene.appState, collaborators: new Map() } as any, files: revision.scene.files as any, scrollToContent: true }}
        />
      )}</div>
    </div>
  );
}

/** Opening a diagram from the list or a chat, written once for both. */
export function openDiagram(id: string, sessionId: string | null): void {
  navigate(pathFor("diagrams", id, { session: sessionId }));
}
