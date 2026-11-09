// One canvas open: the board fills the window under one compact bar (back, the title, where the canvas
// belongs, whether it is saved, history, sharing, the .canvas file), and nothing sits beside it. The
// history opens as a drawer on the right on a desktop and as a sheet on a phone, closed until asked for.
//
// Saving sends what the operator did as operations on single items with the version the editor last
// had. The host applies a stale batch anyway when nobody else touched the same items and answers
// `merged`; the editor then shows the host's document with its own unsent work on top. Only when the
// same card was changed on both sides does it stop and ask, naming the cards, instead of the old
// whole-drawing "someone changed it, reload or overwrite".

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError } from "../api";
import { useEvent, useStreamUp } from "../events";
import { Icon } from "../icons";
import { t } from "../i18n";
import { latinKey } from "../navigation";
import { back, navigate, pathFor } from "../router";
import { invalidate } from "../store";
import { errorText } from "../ui";
import { OverflowMenu, Popover, Sheet, useMedia } from "../ui/index";
import type { MenuItem } from "../ui/dialogs";
import { copyText, useCopied } from "../ui/components";
import { relTimeLong } from "../format";
import { Board } from "./Board";
import { changeSummary, clockTime, groupByDay, type RevisionItem } from "./history";
import { newId, normalizeDoc, nodeLabel, type Canvas, type CanvasDoc, type CanvasHead, type CanvasNode } from "./model";
import { UndoStack, applyOps, compact, type Op } from "./ops";
import { CommentBar, cardWords, type Chip } from "./CommentBar";
import { FrameContext, LiveFrame, StillFrame, frameOf, pageName, useFrameHost, type Mode, type Picked } from "./frames";
import { CanvasThumb, attachmentUrl, download, fileName, pickCanvasFile } from "./parts";
import "./canvas.css";

type Status = "saved" | "editing" | "saving" | "offline" | "conflict";
type Revision = { version: number; title: string; doc: CanvasDoc; saved_at: string; source: string; kind: string };
type Conflict = { ids: string[]; current: Canvas };

const SAVE_DELAY_MS = 600;
const HEAD_POLL_MS = 15000;

function unreachable(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status >= 500;
}

/** The canvas as the host sent it, with its document made safe to draw. */
function received(canvas: Canvas): Canvas {
  return { ...canvas, doc: normalizeDoc(canvas.doc), files: canvas.files ?? [] };
}

export function CanvasEditor({ id, listPath, toast }: { id: string; listPath: string; toast: (message: string) => void }) {
  const [canvas, setCanvas] = useState<Canvas | null>(null);
  const [failure, setFailure] = useState("");
  useEffect(() => {
    let active = true;
    setCanvas(null);
    setFailure("");
    api.get<Canvas>(`/api/canvases/${id}`).then((found) => { if (active) setCanvas(received(found)); }).catch((error) => { if (active) setFailure(errorText(error)); });
    return () => { active = false; };
  }, [id]);
  if (!canvas) {
    return (
      <div className="cv-editor">
        <div className="cv-bar"><button className="iconbtn" onClick={() => back(listPath)} aria-label={t("canvas.back")} title={t("canvas.back")}><Icon name="back" size={18} /></button></div>
        <div className="cv-loading">{failure || t("common.loading")}</div>
      </div>
    );
  }
  return <Editor key={canvas.id} initial={canvas} listPath={listPath} toast={toast} />;
}

function Editor({ initial, listPath, toast }: { initial: Canvas; listPath: string; toast: (message: string) => void }) {
  const id = initial.id;
  const phone = useMedia("(max-width: 680px)");
  const [meta, setMeta] = useState(initial);
  const [doc, setDoc] = useState<CanvasDoc>(initial.doc);
  const [title, setTitle] = useState(initial.title);
  const [status, setStatus] = useState<Status>("saved");
  const [conflict, setConflict] = useState<Conflict | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [shareOpen, setShareOpen] = useState(false);
  const [undoState, setUndoState] = useState({ undo: false, redo: false });
  const shareButton = useRef<HTMLButtonElement>(null);
  // What the host last confirmed, and the operations made on top of it: in flight, and waiting.
  const server = useRef({ doc: initial.doc, version: initial.version });
  const inflight = useRef<Op[] | null>(null);
  const pending = useRef<Op[]>([]);
  const docRef = useRef(doc);
  docRef.current = doc;
  const undo = useRef(new UndoStack());
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const conflictRef = useRef(false);
  const titleRef = useRef(initial.title);
  const historyRefresh = useRef<() => void>(() => undefined);
  const registerHistory = useCallback((refresh: () => void) => { historyRefresh.current = refresh; }, []);

  const refreshUndo = () => setUndoState({ undo: undo.current.canUndo, redo: undo.current.canRedo });

  /** The document the operator sees: the host's, with every operation it has not confirmed on top. */
  const rebuild = useCallback(() => {
    const next = applyOps(applyOps(server.current.doc, inflight.current ?? []), pending.current);
    setDoc(next);
  }, []);

  const adopt = useCallback((next: Canvas) => {
    const fresh = received(next);
    server.current = { doc: fresh.doc, version: fresh.version };
    setMeta(fresh);
    if (document.activeElement?.classList.contains("cv-title") !== true) {
      setTitle(fresh.title);
      titleRef.current = fresh.title;
    }
  }, []);

  const flush = useCallback(async () => {
    if (timer.current) clearTimeout(timer.current);
    if (inflight.current || conflictRef.current || !pending.current.length) return;
    const batch = compact(pending.current);
    inflight.current = batch;
    pending.current = [];
    setStatus("saving");
    try {
      const result = await api.post<{ canvas: Canvas; merged: boolean }>(`/api/canvases/${id}/ops`, { base_version: server.current.version, ops: batch });
      inflight.current = null;
      adopt(result.canvas);
      rebuild();
      historyRefresh.current();
      invalidate("/api/canvases");
      if (pending.current.length) {
        setStatus("editing");
        timer.current = setTimeout(() => void flush(), SAVE_DELAY_MS);
      } else setStatus("saved");
    } catch (error) {
      pending.current = [...batch, ...pending.current];
      inflight.current = null;
      const detail = error instanceof ApiError ? (error.data.detail as { conflicts?: string[]; current?: Canvas } | undefined) : undefined;
      if (error instanceof ApiError && error.status === 409 && detail?.current) {
        conflictRef.current = true;
        setConflict({ ids: detail.conflicts ?? [], current: received(detail.current) });
        setStatus("conflict");
      } else if (unreachable(error)) {
        // Kept, not lost: the work stays queued and goes out with the next attempt.
        setStatus("offline");
        timer.current = setTimeout(() => void flush(), 5000);
      } else {
        // Refused as invalid: the batch cannot be saved as it is, and keeping it would refuse every
        // later save too. The board goes back to what the host has.
        pending.current = [];
        rebuild();
        setStatus("saved");
        toast(errorText(error));
      }
    }
  }, [adopt, id, rebuild, toast]);

  const onOps = useCallback((ops: Op[]) => {
    undo.current.record(docRef.current, ops);
    refreshUndo();
    pending.current = [...pending.current, ...ops];
    setDoc((current) => applyOps(current, ops));
    if (conflictRef.current) return;
    setStatus("editing");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void flush(), SAVE_DELAY_MS);
  }, [flush]);

  const replay = useCallback((ops: Op[] | null) => {
    refreshUndo();
    if (!ops?.length) return;
    pending.current = [...pending.current, ...ops];
    setDoc((current) => applyOps(current, ops));
    if (conflictRef.current) return;
    setStatus("editing");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void flush(), SAVE_DELAY_MS);
  }, [flush]);

  const onUndo = useCallback(() => replay(undo.current.undo(docRef.current)), [replay]);
  const onRedo = useCallback(() => replay(undo.current.redo(docRef.current)), [replay]);

  /** The other side's version wins: the queued work is dropped and the board shows theirs. */
  const takeTheirs = useCallback(() => {
    if (!conflict) return;
    pending.current = [];
    conflictRef.current = false;
    adopt(conflict.current);
    undo.current.clear();
    refreshUndo();
    rebuild();
    setConflict(null);
    setStatus("saved");
    historyRefresh.current();
  }, [adopt, conflict, rebuild]);

  /** Mine on top: the queued work is applied over their version, which stays in the history. */
  const keepMine = useCallback(() => {
    if (!conflict) return;
    conflictRef.current = false;
    adopt(conflict.current);
    rebuild();
    setConflict(null);
    void flush();
  }, [adopt, conflict, flush, rebuild]);

  const busy = useRef(false);
  const pull = useCallback(async () => {
    if (busy.current || inflight.current || pending.current.length || conflictRef.current) return;
    try {
      const latest = await api.get<Canvas>(`/api/canvases/${id}`);
      // A save of ours that started meanwhile answers with this version or a newer one itself.
      if (busy.current || inflight.current || pending.current.length || latest.version <= server.current.version) return;
      adopt(latest);
      rebuild();
      historyRefresh.current();
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        toast(t("canvas.gone"));
        navigate(listPath, { replace: true });
      }
    }
  }, [adopt, id, listPath, rebuild, toast]);

  // What was held back while the operator typed or dragged is taken in once they stop, unless their own
  // work is now queued, in which case the save settles it (merged, or a conflict that names the card).
  const onBusy = useCallback((now: boolean) => {
    busy.current = now;
    if (!now) window.setTimeout(() => void pull(), 0);
  }, [pull]);

  useEvent(["canvas.changed"], (event) => {
    const payload = event.payload as { canvas_id?: string; version?: number; change?: string };
    if (payload.canvas_id !== id) return;
    if (payload.change === "deleted") {
      toast(t("canvas.gone"));
      navigate(listPath, { replace: true });
      return;
    }
    if ((payload.version ?? 0) > server.current.version) void pull();
  }, [id, pull]);

  // The event is live only; a slow head poll covers a stream that dropped, and a stream that comes back.
  const up = useStreamUp();
  useEffect(() => { if (up) void pull(); }, [up, pull]);
  useEffect(() => {
    const tick = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const head = await api.get<CanvasHead>(`/api/canvases/${id}/head`);
        if (head.version > server.current.version) void pull();
      } catch { /* the next tick tries again */ }
    };
    const handle = window.setInterval(() => void tick(), HEAD_POLL_MS);
    const onVisible = () => void tick();
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(handle); document.removeEventListener("visibilitychange", onVisible); };
  }, [id, pull]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && latinKey(event) === "s") {
        event.preventDefault();
        void flush();
      }
    };
    const onLeave = (event: BeforeUnloadEvent) => { if (pending.current.length || inflight.current) event.preventDefault(); };
    const onOnline = () => { if (pending.current.length) void flush(); };
    window.addEventListener("keydown", onKey);
    window.addEventListener("beforeunload", onLeave);
    window.addEventListener("online", onOnline);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("beforeunload", onLeave);
      window.removeEventListener("online", onOnline);
      // Leaving with work still waiting for its timer: send it now rather than drop it.
      if (pending.current.length && !conflictRef.current) void flush();
      if (timer.current) clearTimeout(timer.current);
    };
  }, [flush]);

  async function rename(next: string) {
    const clean = next.trim();
    if (!clean || clean === meta.title) {
      setTitle(meta.title);
      return;
    }
    // Pending operations first: a rename bumps the version, and a batch sent after it on the old
    // version would be checked against a change that is only a title.
    await flush();
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const head = await api.patch<CanvasHead>(`/api/canvases/${id}`, { title: clean, version: server.current.version });
        server.current = { ...server.current, version: head.version };
        setMeta((current) => ({ ...current, title: head.title, version: head.version }));
        titleRef.current = head.title;
        invalidate("/api/canvases");
        historyRefresh.current();
        return;
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 409) || attempt) {
          toast(errorText(error));
          return;
        }
        // A title does not conflict with anyone's cards: take the newer version and rename that.
        const latest = await api.get<Canvas>(`/api/canvases/${id}`).catch(() => null);
        if (!latest) return;
        if (!pending.current.length && !inflight.current) {
          adopt(latest);
          rebuild();
        }
        setTitle(clean);
      }
    }
  }

  async function exportFile() {
    try {
      const response = await api.fetch(`/api/canvases/${id}/export`);
      if (!response.ok) throw new ApiError(response.status, t("canvas.export.failed"));
      download(await response.blob(), `${fileName(titleRef.current)}.canvas`);
    } catch (error) { toast(errorText(error)); }
  }

  async function importFile() {
    const picked = await pickCanvasFile();
    if (!picked) return;
    try {
      const made = await api.post<Canvas>("/api/canvases/import", { title: picked.name || t("canvas.untitled"), scope: meta.scope, scope_id: meta.scope_id, owner_session_id: meta.owner_session_id, text: picked.text });
      invalidate("/api/canvases");
      toast(t("canvas.imported", { title: made.title }));
      navigate(pathFor("canvas", made.id, Object.fromEntries(new URLSearchParams(listPath.split("?")[1] ?? ""))));
    } catch (error) { toast(errorText(error)); }
  }

  async function restore(revision: number) {
    await flush();
    try {
      const restored = await api.post<Canvas>(`/api/canvases/${id}/restore`, { revision, version: server.current.version });
      adopt(restored);
      pending.current = [];
      undo.current.clear();
      refreshUndo();
      rebuild();
      setSelected(null);
      setStatus("saved");
      invalidate("/api/canvases");
      historyRefresh.current();
    } catch (error) {
      toast(error instanceof ApiError && error.status === 409 ? t("canvas.restore.stale") : errorText(error));
    }
  }

  const fileItems: MenuItem[] = [
    { label: t("canvas.export"), icon: "download", onSelect: () => void exportFile() },
    { label: t("canvas.import"), icon: "import", onSelect: () => void importFile() },
  ];
  const statusIcon = status === "saved" ? "check" : status === "conflict" || status === "offline" ? "alert" : "dot";
  const sharePanel = <SharePanel id={id} title={meta.title} token={meta.share_token} onToken={(token) => setMeta((current) => ({ ...current, share_token: token, shared: !!token }))} toast={toast} />;
  const fileUrl = useCallback((fileId: string) => attachmentUrl(id, fileId), [id]);
  const marked = useMemo(() => new Set(conflict?.ids ?? []), [conflict]);

  // -- live frames, pointing and the bar ----------------------------------------------------------
  const [mode, setMode] = useState<Mode>("interact");
  const [chips, setChips] = useState<Chip[]>([]);
  const [picking, setPicking] = useState<{ frameId: string; picked: Picked } | null>(null);
  const [highlight, setHighlight] = useState<{ frameId: string; selector: string } | null>(null);
  const [originsVersion] = useState(0);
  // Origins allowed for one load of one frame, keyed by the frame and the time the agent showed it:
  // they live in this window only, and showing the page again asks again.
  const once = useRef(new Map<string, string[]>());
  const [onceVersion, setOnceVersion] = useState(0);
  const blocked = useRef(new Map<string, Set<string>>());
  const onceKey = useCallback((frameId: string) => {
    const node = docRef.current.nodes.find((item) => item.id === frameId);
    return `${frameId}@${node ? frameOf(node)?.rev ?? 0 : 0}`;
  }, []);
  const onceFor = useCallback((frameId: string) => once.current.get(onceKey(frameId)) ?? [], [onceKey, onceVersion]); // eslint-disable-line react-hooks/exhaustive-deps
  const onPicked = useCallback((frameId: string, picked: Picked) => {
    if (mode === "comment") setPicking({ frameId, picked });
  }, [mode]);
  const onBlocked = useCallback((frameId: string, origin: string) => {
    const seen = blocked.current.get(frameId) ?? new Set<string>();
    seen.add(origin);
    blocked.current.set(frameId, seen);
  }, []);
  const frameHost = useFrameHost({ canvasId: id, mode, highlight, originsVersion, onPicked, onBlocked, onceFor });
  const renderFrame = useCallback((node: CanvasNode, near: boolean) => <LiveFrame node={node} near={near} />, []);
  const onPoint = useCallback((node: CanvasNode) => {
    setChips((current) => (current.some((chip) => chip.kind === "card" && chip.nodeId === node.id) ? current : [...current, { id: newId(), kind: "card", nodeId: node.id, words: cardWords(node) }]));
  }, []);
  const changeMode = useCallback((next: Mode) => {
    setMode(next);
    if (next === "interact") setPicking(null);
  }, []);
  const removeChip = useCallback((chipId: string | null) => {
    setChips((current) => (chipId === null ? [] : current.filter((chip) => chip.id !== chipId)));
    setHighlight(null);
  }, []);
  const pendingNode = picking ? doc.nodes.find((node) => node.id === picking.frameId) : undefined;
  const overlay = picking && pendingNode ? (
    <PickNote
      node={pendingNode}
      picked={picking.picked}
      onCancel={() => setPicking(null)}
      onAdd={(note) => {
        const { rect: _rect, ...element } = picking.picked;
        setChips((current) => [...current, { id: newId(), kind: "pick", frameId: picking.frameId, page: pageName(pendingNode), note, ...element }]);
        setPicking(null);
      }}
    />
  ) : null;
  const conflictNames = useMemo(() => {
    if (!conflict) return "";
    const names = conflict.ids.map((item) => {
      const node = doc.nodes.find((node) => node.id === item) ?? conflict.current.doc.nodes.find((node) => node.id === item);
      if (node) return `«${nodeLabel(node, t(`canvas.kind.${node.type}`))}»`;
      return t("canvas.kind.edge");
    });
    const unique = [...new Set(names)];
    return unique.slice(0, 4).join(", ") + (unique.length > 4 ? ` +${unique.length - 4}` : "");
  }, [conflict, doc]);

  return (
    <div className={`cv-editor ${historyOpen ? "history-open" : ""}`}>
      <header className="cv-bar">
        <button className="iconbtn" onClick={() => { void flush(); back(listPath); }} aria-label={t("canvas.back")} title={t("canvas.back")}><Icon name="back" size={18} /></button>
        <input className="cv-title" aria-label={t("canvas.title")} value={title} maxLength={160} onChange={(event) => setTitle(event.target.value)} onBlur={(event) => void rename(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") (event.target as HTMLInputElement).blur(); if (event.key === "Escape") { setTitle(meta.title); (event.target as HTMLInputElement).blur(); } }} />
        <span className="cv-scope-chip" title={meta.scope_name}><Icon name={meta.scope === "project" ? "folder" : "chat"} size={12} />{meta.scope_name}</span>
        <span className={`cv-status ${status}`} role="status" aria-label={t(`canvas.status.${status}`)} title={status === "saved" ? t("canvas.status.saved.version", { n: meta.version }) : t(`canvas.status.${status}`)}>
          <Icon name={statusIcon} size={14} /><span className="cv-status-word">{t(`canvas.status.${status}`)}</span>
        </span>
        <button className={`btn cv-tool-btn ${historyOpen ? "on" : ""}`} onClick={() => { setHistoryOpen((open) => !open); setSelected(null); }} aria-pressed={historyOpen} aria-label={t("canvas.history")} title={t("canvas.history")}>
          <Icon name="clock" size={16} /><span className="cv-tool-word">{t("canvas.history")}</span>
        </button>
        {!phone && (
          <button ref={shareButton} className={`btn cv-tool-btn ${shareOpen ? "on" : ""}`} onClick={() => setShareOpen((open) => !open)} aria-haspopup="dialog" aria-expanded={shareOpen} aria-label={t("canvas.share")}>
            <Icon name={meta.share_token ? "link" : "share"} size={16} /><span className="cv-tool-word">{t("canvas.share")}</span>
          </button>
        )}
        {!phone && <OverflowMenu className="btn cv-tool-btn" label={t("canvas.file")} items={fileItems} trigger={<><Icon name="download" size={16} /><span className="cv-tool-word">{t("canvas.file")}</span><Icon name="down" size={14} /></>} />}
        {phone && <OverflowMenu label={t("canvas.more")} items={[{ label: t("canvas.share"), icon: "share", onSelect: () => setShareOpen(true) }, "-", ...fileItems]} />}
      </header>
      {conflict && (
        <div className="cv-banner conflict" role="alert">
          <Icon name="alert" size={16} />
          <span>{t(conflict.current.updated_by === "agent" ? "canvas.conflict.agent" : "canvas.conflict.other", { items: conflictNames })}</span>
          <button className="btn small" onClick={takeTheirs}>{t("canvas.conflict.theirs")}</button>
          <button className="btn small primary" onClick={keepMine}>{t("canvas.conflict.mine")}</button>
        </div>
      )}
      <div className="cv-stage">
        {/* The preview of an earlier version lies over the board only, never under the history drawer
            beside it, whose rows choose what it shows. */}
        <div className="cv-board-wrap">
          <FrameContext.Provider value={frameHost}>
            <Board doc={doc} label={meta.title} fileUrl={fileUrl} onOps={onOps} onUndo={onUndo} onRedo={onRedo} canUndo={undoState.undo} canRedo={undoState.redo} marked={marked} compact={phone} onBusy={onBusy} renderFrame={renderFrame} mode={mode} onModeChange={changeMode} onPoint={onPoint} overlay={overlay} />
          </FrameContext.Provider>
          {selected !== null && <RevisionPreview id={id} version={selected} current={selected === meta.version} onRestore={() => void restore(selected)} onClose={() => setSelected(null)} />}
        </div>
        {historyOpen && (
          <HistoryPanel
            id={id}
            current={meta.version}
            selected={selected}
            phone={phone}
            hidden={phone && selected !== null}
            onSelect={(next) => setSelected(next === meta.version ? null : next)}
            onClose={() => { setHistoryOpen(false); setSelected(null); }}
            register={registerHistory}
          />
        )}
      </div>
      <CommentBar
        canvas={meta}
        doc={doc}
        chips={chips}
        onRemove={removeChip}
        onHighlight={(chip) => setHighlight(chip && chip.kind === "pick" ? { frameId: chip.frameId, selector: chip.selector } : null)}
        onOwner={(sessionId) => setMeta((current) => ({ ...current, owner_session_id: sessionId }))}
        toast={toast}
      />
      {shareOpen && !phone && <Popover anchor={shareButton.current} onClose={() => setShareOpen(false)} align="right" className="cv-share-pop" label={t("canvas.share")}>{sharePanel}</Popover>}
      {shareOpen && phone && <Sheet title={t("canvas.share")} onClose={() => setShareOpen(false)} size="narrow">{sharePanel}</Sheet>}
    </div>
  );
}

function SharePanel({ id, title, token, onToken, toast }: { id: string; title: string; token: string; onToken: (token: string) => void; toast: (message: string) => void }) {
  const [copied, copy] = useCopied();
  const [busy, setBusy] = useState(false);
  const url = token ? `${window.location.origin}/app/d/${token}` : "";
  async function create() {
    setBusy(true);
    try {
      const result = await api.post<{ url: string }>(`/api/canvases/${id}/share`, {});
      onToken(result.url.split("/").pop() ?? "");
      invalidate("/api/canvases");
      await copyText(`${window.location.origin}${result.url}`);
    } catch (error) { toast(errorText(error)); } finally { setBusy(false); }
  }
  async function revoke() {
    setBusy(true);
    try {
      await api.delete(`/api/canvases/${id}/share`);
      onToken("");
      invalidate("/api/canvases");
    } catch (error) { toast(errorText(error)); } finally { setBusy(false); }
  }
  return (
    <div className="cv-share">
      <div className="cv-share-head">
        <Icon name={token ? "link" : "lock"} size={16} />
        <strong>{token ? t("canvas.share.on") : t("canvas.share.off")}</strong>
      </div>
      <p>{token ? t("canvas.share.on.body") : t("canvas.share.off.body")}</p>
      {token ? (
        <>
          <input className="field" aria-label={t("canvas.share.link")} readOnly value={url} onFocus={(event) => event.target.select()} />
          <div className="cv-share-actions">
            <button className="btn primary" onClick={() => void copy(url).then((ok) => { if (!ok) toast(t("canvas.link.copy.failed")); })}><Icon name={copied ? "check" : "copy"} size={15} />{t("canvas.share.copy")}</button>
            {typeof navigator.share === "function" && <button className="btn" onClick={() => void navigator.share({ title, url }).catch(() => undefined)}><Icon name="share" size={15} />{t("canvas.share.send")}</button>}
            <button className="btn danger" disabled={busy} onClick={() => void revoke()}><Icon name="unlink" size={15} />{t("canvas.share.revoke")}</button>
          </div>
        </>
      ) : (
        <div className="cv-share-actions"><button className="btn primary" disabled={busy} onClick={() => void create()}><Icon name="link" size={15} />{t("canvas.share.create")}</button></div>
      )}
    </div>
  );
}

function HistoryPanel({ id, current, selected, phone, hidden, onSelect, onClose, register }: { id: string; current: number; selected: number | null; phone: boolean; hidden: boolean; onSelect: (version: number) => void; onClose: () => void; register: (refresh: () => void) => void }) {
  const [items, setItems] = useState<RevisionItem[] | null>(null);
  const [failed, setFailed] = useState("");
  const load = useCallback(() => {
    api.get<RevisionItem[]>(`/api/canvases/${id}/versions`).then((list) => { setItems(list); setFailed(""); }).catch((error) => setFailed(errorText(error)));
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
        <section key={group.key} className="cv-history-day">
          <h4>{group.label}</h4>
          {group.items.map((item) => (
            <button key={item.version} className={`cv-revision ${selected === item.version || (selected === null && item.version === current) ? "active" : ""}`} onClick={() => onSelect(item.version)} aria-current={item.version === current ? "true" : undefined}>
              <CanvasThumb path={`/api/canvases/${id}/versions/${item.version}/preview`} version={item.version} />
              <span className="cv-revision-text">
                <span className="cv-revision-top">
                  <time dateTime={item.saved_at} title={relTimeLong(item.saved_at)}>{clockTime(item.saved_at)}</time>
                  <span className={`cv-who ${item.source}`}><Icon name={item.source === "agent" ? "bots" : "user"} size={12} />{t(item.source === "agent" ? "canvas.who.agent" : "canvas.who.you")}</span>
                  {item.version === current && <span className="cv-current">{t("canvas.current")}</span>}
                </span>
                <span className="cv-revision-summary">{changeSummary(item.summary, item.kind)}</span>
              </span>
            </button>
          ))}
        </section>
      ))}
    </>
  );
  if (phone) {
    if (hidden) return null;
    return <Sheet title={t("canvas.history")} onClose={onClose} className="cv-history-sheet">{body}</Sheet>;
  }
  return (
    <aside className="cv-history" aria-label={t("canvas.history")}>
      <div className="cv-history-head">
        <strong>{t("canvas.history")}</strong>
        <button className="iconbtn small" onClick={onClose} aria-label={t("common.close")} title={t("common.close")}><Icon name="close" size={16} /></button>
      </div>
      <div className="cv-history-body">{body}</div>
    </aside>
  );
}

function RevisionPreview({ id, version, current, onRestore, onClose }: { id: string; version: number; current: boolean; onRestore: () => void; onClose: () => void }) {
  const [revision, setRevision] = useState<Revision | null>(null);
  useEffect(() => {
    let active = true;
    setRevision(null);
    api.get<Revision>(`/api/canvases/${id}/versions/${version}`).then((found) => { if (active) setRevision({ ...found, doc: normalizeDoc(found.doc) }); }).catch(() => undefined);
    return () => { active = false; };
  }, [id, version]);
  return (
    <div className="cv-preview" aria-label={t("canvas.preview.label")}>
      <div className="cv-banner preview" role="status">
        <Icon name="eye" size={16} />
        <span>{revision ? t("canvas.preview.at", { time: relTimeLong(revision.saved_at) }) : t("common.loading")}</span>
        <button className="btn small" onClick={onClose}>{t("canvas.return")}</button>
        {!current && <button className="btn small primary" disabled={!revision} onClick={onRestore}><Icon name="undo" size={14} />{t("canvas.restore")}</button>}
      </div>
      {revision && <Board key={version} doc={revision.doc} label={revision.title} readOnly fileUrl={(fileId) => attachmentUrl(id, fileId)} renderFrame={(node) => <StillFrame node={node} />} />}
    </div>
  );
}

/** The note beside an element picked in a frame, drawn in board coordinates next to the element and
 *  kept at its own size on screen whatever the zoom. */
function PickNote({ node, picked, onAdd, onCancel }: { node: CanvasNode; picked: Picked; onAdd: (note: string) => void; onCancel: () => void }) {
  const [note, setNote] = useState("");
  const x = node.x + Math.max(0, Math.min(node.width, picked.rect.x + picked.rect.width)) + 12;
  const y = node.y + Math.max(0, Math.min(node.height, picked.rect.y));
  return (
    <div className="cv-pick-note" data-ui role="dialog" aria-label={t("canvas.pick.title")} style={{ left: x, top: y }} onPointerDown={(event) => event.stopPropagation()}>
      <div className="cv-pick-what"><code>{picked.selector || picked.tag}</code>{picked.text && <span>{picked.text}</span>}</div>
      <textarea
        ref={(element) => { if (element && document.activeElement !== element && !element.dataset.focused) { element.dataset.focused = "1"; element.focus({ preventScroll: true }); } }}
        rows={3}
        value={note}
        placeholder={t("canvas.pick.placeholder")}
        aria-label={t("canvas.pick.placeholder")}
        onChange={(event) => setNote(event.target.value)}
        onKeyDown={(event) => {
          event.stopPropagation();
          if (event.key === "Escape") onCancel();
          if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) onAdd(note);
        }}
      />
      <div className="cv-pick-actions">
        <button type="button" className="btn small" onClick={onCancel}>{t("common.cancel")}</button>
        <button type="button" className="btn small primary" onClick={() => onAdd(note)}>{t("canvas.pick.add")}</button>
      </div>
    </div>
  );
}
