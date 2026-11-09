// Live pages on the board. A frame node is a file node whose `daedalus.frame` says it is a page the
// agent showed; the board draws it as an iframe of that page, served by the services door on another
// origin under a signed address that expires.
//
// The iframe is the second wall. Its sandbox has no `allow-same-origin`, so the page runs with an opaque
// origin even if the door's own header were lost: no cookie, no storage of the app, no popups, no
// navigation of the board around it. Everything the page says arrives through `postMessage` from its
// bridge script and is untrusted text. Coming from the frame's window proves little: the window stays
// the same when the page navigates it to some other site, and its `origin` is "null" for every
// sandboxed page. So each document's bridge carries a nonce the door gave that one document, the host
// confirms it once, and the board listens only to messages carrying the confirmed nonce of the
// document loaded last. In comment mode a glass over the frame takes the operator's clicks, and a pick
// counts only as the answer to the board's own question about a point it saw clicked.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";
import { api, ApiError } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { relTimeLong } from "../format";
import type { CanvasNode } from "./model";

export type FrameInfo = { device: string; rev: number };
export type Mode = "interact" | "comment";
export type Picked = { id: string; selector: string; tag: string; text: string; html: string; rect: { x: number; y: number; width: number; height: number } };

/** The selector grammar the bridge writes, and the host accepts; anything else did not come from it. */
const PART = String.raw`(?:#[A-Za-z][\w-]*|[A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z][\w-]*){0,2}(?::nth-of-type\(\d{1,4}\))?)`;
export const SELECTOR = new RegExp(`^${PART}(?: > ${PART}){0,5}$`);
const TAG = /^[A-Za-z][A-Za-z0-9-]{0,39}$/;

/** One id per open board, for the host to know which board allowed an origin "this time". */
export const BOARD_ID = typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const HEARTBEAT_MS = 60_000;
export type Address = { url: string; expires_at: number; origins: { canvas: string[]; all: string[] }; once: string[]; generation: number };
type AddressAnswer = Address | { needs_door: true; origins: { canvas: string[]; all: string[] } };

/** The frame description of a node, or null for any other node (an imported file card stays a card). */
export function frameOf(node: CanvasNode): FrameInfo | null {
  if (node.type !== "file") return null;
  const frame = (node.daedalus as { frame?: unknown } | undefined)?.frame;
  if (!frame || typeof frame !== "object") return null;
  const { device, rev } = frame as { device?: unknown; rev?: unknown };
  return { device: typeof device === "string" ? device : "custom", rev: typeof rev === "number" ? rev : 0 };
}

export function pageName(node: CanvasNode): string {
  return (node.file ?? "").split("/").filter(Boolean).pop() ?? "";
}

/** Bounded, single-line text from a frame: it is the page's own words and goes into the board's DOM
 *  as text only, never as markup. */
function clean(value: unknown, limit: number): string {
  return typeof value === "string" ? value.slice(0, limit) : "";
}

/** A message from a frame's bridge, read field by field into known shapes, or null. */
export type FrameMessage = ({ type: "loaded"; title: string } | { type: "console"; level: "error" | "warning" | "exception"; text: string } | { type: "blocked"; origin: string } | ({ type: "picked" } & Picked) | { type: "snapshot"; html: string; width: number; height: number }) & { nonce: string };

export function readFrameMessage(data: unknown): FrameMessage | null {
  if (!data || typeof data !== "object") return null;
  const message = data as Record<string, unknown>;
  if (message.daedalus !== "canvas-frame") return null;
  const nonce = clean(message.nonce, 64);
  if (!nonce) return null;
  const read = readBody(message);
  return read ? { ...read, nonce } : null;
}

type FrameBody = FrameMessage extends infer T ? (T extends { nonce: string } ? Omit<T, "nonce"> : never) : never;

function readBody(message: Record<string, unknown>): FrameBody | null {
  switch (message.type) {
    case "loaded":
      return { type: "loaded", title: clean(message.title, 200) };
    case "console": {
      const level = message.level === "warning" || message.level === "exception" ? message.level : "error";
      return { type: "console", level, text: clean(message.text, 1000) };
    }
    case "blocked": {
      const origin = clean(message.origin, 200);
      // Only an origin that parses as one: the bar shows it and the host would refuse anything else.
      try {
        const parsed = new URL(origin);
        if (parsed.origin !== origin || !/^https?:$/.test(parsed.protocol)) return null;
      } catch {
        return null;
      }
      return { type: "blocked", origin };
    }
    case "picked": {
      const rect = (message.rect ?? {}) as Record<string, unknown>;
      const number = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? Math.max(-100000, Math.min(100000, value)) : 0);
      const selector = clean(message.selector, 600);
      const tag = clean(message.tag, 40);
      // A selector outside the bridge's grammar is page-written text pretending to be one: dropped.
      if (!SELECTOR.test(selector) || (tag && !TAG.test(tag))) return null;
      return { type: "picked", id: clean(message.id, 64), selector, tag, text: clean(message.text, 400), html: clean(message.html, 2000), rect: { x: number(rect.x), y: number(rect.y), width: number(rect.width), height: number(rect.height) } };
    }
    case "snapshot": {
      const html = clean(message.html, 3_000_000);
      const width = typeof message.width === "number" ? Math.round(message.width) : 0;
      const height = typeof message.height === "number" ? Math.round(message.height) : 0;
      return html && width > 0 && height > 0 ? { type: "snapshot", html, width: Math.min(width, 10000), height: Math.min(height, 10000) } : null;
    }
  }
  return null;
}

/** What every live frame of one open board shares with the editor around it. */
export type FrameHost = {
  canvasId: string;
  mode: Mode;
  /** Whether the canvas has a public link: still copies of its pages are taken only then. */
  shared: boolean;
  /** The element a chip points at, outlined in its frame while the chip is hovered or focused. */
  highlight: { frameId: string; selector: string } | null;
  /** Bumped when the canvas's allowed origins change: every frame fetches a new address. */
  originsVersion: number;
  onPicked: (frameId: string, picked: Picked) => void;
  onBlocked: (frameId: string, origin: string) => void;
  /** Origins allowed for one load of one frame, until the board closes; stored nowhere. */
  onceFor: (frameId: string) => string[];
  /** "Allow this time": this frame, this board, until it closes or the page is shown again. */
  allowOnce: (frameId: string, origin: string) => void;
  /** "Always": stored on the host for this canvas, or for every canvas. */
  allowAlways: (origin: string, everywhere: boolean) => Promise<void>;
};

export const FrameContext = createContext<FrameHost | null>(null);

const SNAPSHOT_QUIET_MS = 2000;
const REPORT_DELAY_MS = 1000;
const LEFT_AFTER_MS = 3000;
const PICK_ANSWER_MS = 2000;
const HOVER_EVERY_MS = 50;
/** The last snapshot sent per frame in this window, so an unchanged page is not uploaded again. */
const sentSnapshots = new Map<string, string>();

type State =
  | { kind: "loading" }
  | { kind: "needs_door" }
  | { kind: "missing" }
  | { kind: "failed"; message: string }
  | { kind: "left" }
  | { kind: "live"; address: Address };

/** One live frame: its address, its iframe, and everything its bridge says. */
export function LiveFrame({ node, near }: { node: CanvasNode; near: boolean }) {
  const host = useContext(FrameContext);
  const frame = frameOf(node);
  const [state, setState] = useState<State>({ kind: "loading" });
  const [reloads, setReloads] = useState(0);
  // Origins the browser refused this load, in the order met, and the ones the operator waved off.
  const [refused, setRefused] = useState<string[]>([]);
  const [dismissed, setDismissed] = useState<string[]>([]);
  const live = useRef(false);
  live.current = state.kind === "live";
  const iframe = useRef<HTMLIFrameElement>(null);
  const counts = useRef({ loads: 0, bridged: 0 });
  // The nonce of the document loaded last, once the host has confirmed it; until then the frame's words
  // wait here, and words under any other nonce are dropped.
  const trusted = useRef<string | null>(null);
  const waiting = useRef<FrameMessage[]>([]);
  const asked = useRef<{ id: string; at: number } | null>(null);
  const report = useRef<{ loaded?: boolean; title?: string; lines: { level: string; text: string }[]; blocked: string[] }>({ lines: [], blocked: [] });
  const reportTimer = useRef<number>(0);
  const snapshotTimer = useRef<number>(0);
  const leftTimer = useRef<number>(0);
  const canvasId = host?.canvasId ?? "";
  const once = host?.onceFor(node.id).join(" ") ?? "";

  // A new address when the page is shown again (rev), when the allowed origins change, when this
  // frame is allowed an origin once, and when the operator reloads it.
  useEffect(() => {
    if (!canvasId) return;
    let active = true;
    setState({ kind: "loading" });
    setRefused([]);
    counts.current = { loads: 0, bridged: 0 };
    // A new address is a new page: whom the board believed before counts for nothing now.
    trusted.current = null;
    waiting.current = [];
    api.post<AddressAnswer>(`/api/canvases/${canvasId}/frames/${encodeURIComponent(node.id)}/address`, { once: once ? once.split(" ") : [], board: BOARD_ID })
      .then((answer) => {
        if (!active) return;
        setState("needs_door" in answer ? { kind: "needs_door" } : { kind: "live", address: answer });
      })
      .catch((error) => {
        if (!active) return;
        setState(error instanceof ApiError && error.status === 404 ? { kind: "missing" } : { kind: "failed", message: error instanceof Error ? error.message : String(error) });
      });
    return () => { active = false; };
  }, [canvasId, node.id, frame?.rev, host?.originsVersion, once, reloads]);

  // An origin allowed "this time" holds only while this board says it is open: once it stops (closed,
  // reloaded), the address that carries the origin stops answering within minutes.
  useEffect(() => {
    if (!once || !canvasId) return;
    const beat = () => void api.post("/api/canvases/frames/heartbeat", { board: BOARD_ID }).catch(() => undefined);
    const handle = window.setInterval(beat, HEARTBEAT_MS);
    return () => window.clearInterval(handle);
  }, [once, canvasId]);

  // An address lasts twelve hours; a board left open longer gets a new one before it runs out.
  useEffect(() => {
    if (state.kind !== "live") return;
    const wait = state.address.expires_at * 1000 - Date.now() - 60_000;
    if (wait <= 0) return;
    const handle = window.setTimeout(() => setReloads((n) => n + 1), Math.min(wait, 2 ** 31 - 1));
    return () => window.clearTimeout(handle);
  }, [state]);

  const flushReport = useCallback(() => {
    const pending = report.current;
    report.current = { lines: [], blocked: [] };
    if (!canvasId || (pending.loaded === undefined && !pending.lines.length && !pending.blocked.length)) return;
    void api.post(`/api/canvases/${canvasId}/frames/${encodeURIComponent(node.id)}/report`, pending).catch(() => undefined);
  }, [canvasId, node.id]);

  const queueReport = useCallback((change: (pending: typeof report.current) => void) => {
    change(report.current);
    window.clearTimeout(reportTimer.current);
    reportTimer.current = window.setTimeout(flushReport, REPORT_DELAY_MS);
  }, [flushReport]);

  const post = useCallback((message: Record<string, unknown>) => {
    // "*" because the frame's origin is opaque and no other target would match; the bridge checks
    // that the message came from its parent window and from the app's origin.
    iframe.current?.contentWindow?.postMessage({ daedalus: "canvas-board", ...message }, "*");
  }, []);

  const handle = useCallback((message: FrameMessage) => {
    if (message.type === "loaded") {
      counts.current.bridged += 1;
      queueReport((pending) => { pending.loaded = true; pending.title = message.title; });
      post({ type: "mode", mode: host?.mode ?? "interact" });
      window.clearTimeout(snapshotTimer.current);
      if (host?.shared) snapshotTimer.current = window.setTimeout(() => post({ type: "snapshot" }), SNAPSHOT_QUIET_MS);
    } else if (message.type === "console") {
      queueReport((pending) => { pending.lines.push({ level: message.level, text: message.text }); });
    } else if (message.type === "blocked") {
      queueReport((pending) => { if (!pending.blocked.includes(message.origin)) pending.blocked.push(message.origin); });
      // A page that retries a script refuses the same origin many times: one row for it.
      setRefused((current) => (current.includes(message.origin) || current.length >= 6 ? current : [...current, message.origin]));
      host?.onBlocked(node.id, message.origin);
    } else if (message.type === "picked") {
      // Only the answer to the board's last question, about a click the board itself saw, and soon.
      const question = asked.current;
      if (!question || question.id !== message.id || Date.now() - question.at > PICK_ANSWER_MS || host?.mode !== "comment") return;
      asked.current = null;
      host.onPicked(node.id, message);
    } else if (message.type === "snapshot") {
      if (!host?.shared) return;
      const key = `${canvasId}:${node.id}`;
      if (sentSnapshots.get(key) === message.html) return;
      sentSnapshots.set(key, message.html);
      void api.put(`/api/canvases/${canvasId}/frames/${encodeURIComponent(node.id)}/snapshot`, { html: message.html, width: message.width, height: message.height }).catch(() => sentSnapshots.delete(key));
    }
  }, [canvasId, host, node.id, post, queueReport]);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (!iframe.current || event.source !== iframe.current.contentWindow) return;
      const message = readFrameMessage(event.data);
      if (!message) return;
      if (message.nonce === trusted.current) {
        handle(message);
        return;
      }
      // A document not confirmed yet: keep its words (a page logs errors and is refused origins
      // before it has loaded) and ask the host about its nonce when it says it has loaded. Until then
      // the document confirmed last stays the one that is believed.
      if (waiting.current.length < 50) waiting.current.push(message);
      if (message.type !== "loaded") return;
      const nonce = message.nonce;
      void api.post<{ ok: boolean }>(`/api/canvases/${canvasId}/frames/${encodeURIComponent(node.id)}/nonce`, { nonce })
        .then((answer) => {
          if (!answer.ok) return;
          trusted.current = nonce;
          const held = waiting.current.filter((item) => item.nonce === nonce);
          waiting.current = waiting.current.filter((item) => item.nonce !== nonce);
          held.forEach(handle);
        })
        .catch(() => undefined);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [canvasId, handle, node.id]);

  useEffect(() => { if (state.kind === "live") post({ type: "mode", mode: host?.mode ?? "interact" }); }, [host?.mode, post, state.kind]);
  const highlight = host?.highlight && host.highlight.frameId === node.id ? host.highlight.selector : null;
  useEffect(() => { if (state.kind === "live") post({ type: "highlight", selector: highlight }); }, [highlight, post, state.kind]);

  useEffect(() => () => {
    window.clearTimeout(reportTimer.current);
    window.clearTimeout(snapshotTimer.current);
    window.clearTimeout(leftTimer.current);
    flushReport();
  }, [flushReport]);

  /** Every load of the iframe must be the page answering through its bridge. A load with no bridge
   *  after it is the page gone somewhere else (a link, a form, a script setting `location`), which
   *  could be anything; the frame is emptied and its address retired. */
  const onLoad = () => {
    // A new document: it counts as the page only once its own nonce is confirmed (see the timer).
    counts.current.loads += 1;
    const expected = counts.current.loads;
    window.clearTimeout(leftTimer.current);
    leftTimer.current = window.setTimeout(() => {
      if (counts.current.bridged >= expected || !live.current) return;
      if (iframe.current) iframe.current.src = "about:blank";
      setState({ kind: "left" });
      void api.post(`/api/canvases/${canvasId}/frames/${encodeURIComponent(node.id)}/revoke`, {}).catch(() => undefined);
    }, LEFT_AFTER_MS);
  };

  const reload = () => setReloads((n) => n + 1);
  const body: ReactNode = (() => {
    if (state.kind === "needs_door") return <FrameNotice icon="lock" title={t("canvas.frame.door")} body={t("canvas.frame.door.body")} />;
    if (state.kind === "missing") return <FrameNotice icon="file" title={pageName(node)} body={t("canvas.frame.missing")} />;
    if (state.kind === "failed") return <FrameNotice icon="alert" title={t("canvas.frame.failed")} body={state.message} action={<button type="button" className="btn small" onClick={reload}>{t("canvas.frame.reload")}</button>} />;
    if (state.kind === "left") return <FrameNotice icon="alert" title={t("canvas.frame.left")} body={t("canvas.frame.left.body")} action={<button type="button" className="btn small" onClick={reload}>{t("canvas.frame.reload")}</button>} />;
    if (state.kind === "loading" || !near) return <div className="cv-frame-wait" aria-hidden="true" />;
    return (
      <iframe
        key={state.address.url}
        ref={iframe}
        className="cv-frame-page"
        src={state.address.url}
        title={t("canvas.frame.title", { page: pageName(node) })}
        sandbox="allow-scripts allow-forms"
        referrerPolicy="no-referrer"
        allow=""
        loading="lazy"
        onLoad={onLoad}
        style={{ width: node.width, height: node.height }}
      />
    );
  })();
  const asking = state.kind === "live" ? refused.filter((origin) => !dismissed.includes(origin)) : [];
  return (
    <>
      <FrameHeader node={node} onReload={reload} />
      <div className="cv-frame-body" data-mode={host?.mode ?? "interact"}>
        {body}
        {state.kind === "live" && near && host?.mode === "comment" && (
          <Glass
            node={node}
            onHover={(x, y) => post({ type: "hover-at", x, y })}
            onPick={(x, y) => {
              const id = BOARD_ID.slice(0, 8) + Math.random().toString(36).slice(2, 12);
              asked.current = { id, at: Date.now() };
              post({ type: "pick-at", x, y, id });
            }}
          />
        )}
        {asking.length > 0 && host && (
          <OriginBar
            origins={asking}
            onOnce={(origin) => host.allowOnce(node.id, origin)}
            onAlways={(origin, everywhere) => void host.allowAlways(origin, everywhere)}
            onDismiss={(origin) => setDismissed((current) => [...current, origin])}
          />
        )}
      </div>
    </>
  );
}

/** The glass over a frame in comment mode. The page never receives the operator's clicks there; the
 *  board turns each click into the page's own coordinates and asks the bridge what lies under it. */
function Glass({ node, onHover, onPick }: { node: CanvasNode; onHover: (x: number, y: number) => void; onPick: (x: number, y: number) => void }) {
  const last = useRef(0);
  const point = (event: ReactMouseEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    return [Math.round(((event.clientX - box.left) * node.width) / Math.max(1, box.width)), Math.round(((event.clientY - box.top) * node.height) / Math.max(1, box.height))] as const;
  };
  return (
    <div
      className="cv-frame-glass"
      data-ui
      role="button"
      aria-label={t("canvas.pick.title")}
      onPointerDown={(event) => event.stopPropagation()}
      onPointerMove={(event) => {
        if (Date.now() - last.current < HOVER_EVERY_MS) return;
        last.current = Date.now();
        onHover(...point(event));
      }}
      onClick={(event) => {
        event.stopPropagation();
        if (!event.isTrusted) return;
        onPick(...point(event));
      }}
    />
  );
}

/** The question a refused origin raises, on the frame that raised it rather than in a dialog over the
 *  board: the page goes on without it until the operator answers, and other frames are not held up. */
function OriginBar({ origins, onOnce, onAlways, onDismiss }: { origins: string[]; onOnce: (origin: string) => void; onAlways: (origin: string, everywhere: boolean) => void; onDismiss: (origin: string) => void }) {
  return (
    <div className="cv-origin-bar" data-ui role="alert" onPointerDown={(event) => event.stopPropagation()}>
      {origins.map((origin) => (
        <div key={origin} className="cv-origin-row">
          <Icon name="shield" size={14} />
          <span className="cv-origin-text">{t("canvas.origin.asked")} <strong>{origin}</strong></span>
          <span className="cv-origin-actions">
            <button type="button" className="btn small primary" onClick={() => onOnce(origin)} title={t("canvas.origin.once.hint")}>{t("canvas.origin.once")}</button>
            <button type="button" className="btn small" onClick={() => onAlways(origin, false)}>{t("canvas.origin.canvas")}</button>
            <button type="button" className="btn small" onClick={() => onAlways(origin, true)}>{t("canvas.origin.all")}</button>
            <button type="button" className="btn small ghost" onClick={() => onDismiss(origin)}>{t("canvas.origin.not.now")}</button>
          </span>
        </div>
      ))}
    </div>
  );
}

/** The sites live pages of this canvas may load from, kept on the host: this canvas's and every
 *  canvas's, each with a way to take it back. "This time" never appears here: it is stored nowhere. */
export function AllowedSites({ canvasId, version, onChanged, toast }: { canvasId: string; version: number; onChanged: () => void; toast: (message: string) => void }) {
  const [origins, setOrigins] = useState<{ canvas: string[]; all: string[] } | null>(null);
  useEffect(() => {
    let active = true;
    api.get<{ canvas: string[]; all: string[] }>(`/api/canvases/${canvasId}/origins`).then((found) => { if (active) setOrigins(found); }).catch((error) => toast(error instanceof Error ? error.message : String(error)));
    return () => { active = false; };
  }, [canvasId, version, toast]);
  async function remove(origin: string, everywhere: boolean) {
    try {
      setOrigins(await api.delete<{ canvas: string[]; all: string[] }>(`/api/canvases/${canvasId}/origins?origin=${encodeURIComponent(origin)}&everywhere=${everywhere}`));
      onChanged();
    } catch (error) { toast(error instanceof Error ? error.message : String(error)); }
  }
  if (!origins) return <div className="empty">{t("common.loading")}</div>;
  const section = (title: string, list: string[], everywhere: boolean) => (
    <section className="cv-sites-section" aria-label={title}>
      <h4>{title}</h4>
      {list.length === 0 ? <p className="cv-sites-none">{t("canvas.sites.none")}</p> : list.map((origin) => (
        <div key={origin} className="cv-sites-row">
          <Icon name="globe" size={14} /><span>{origin}</span>
          <button type="button" className="btn small" onClick={() => void remove(origin, everywhere)} aria-label={t("canvas.sites.remove", { origin })}>{t("canvas.sites.remove.short")}</button>
        </div>
      ))}
    </section>
  );
  return (
    <div className="cv-sites">
      <p className="cv-sites-intro">{t("canvas.sites.intro")}</p>
      {section(t("canvas.sites.canvas"), origins.canvas, false)}
      {section(t("canvas.sites.all"), origins.all, true)}
    </div>
  );
}

function FrameHeader({ node, onReload, label }: { node: CanvasNode; onReload?: () => void; label?: string }) {
  return (
    <div className="cv-frame-head">
      <Icon name="globe" size={13} />
      <span className="cv-frame-name">{pageName(node)}</span>
      <span className="cv-frame-size">{label ?? `${node.width}×${node.height}`}</span>
      {onReload && <button type="button" className="cv-frame-reload" data-ui onPointerDown={(event) => event.stopPropagation()} onClick={onReload} aria-label={t("canvas.frame.reload")} title={t("canvas.frame.reload")}><Icon name="reload" size={13} /></button>}
    </div>
  );
}

function FrameNotice({ icon, title, body, action }: { icon: "lock" | "file" | "alert" | "globe"; title: string; body: string; action?: ReactNode }) {
  return (
    <div className="cv-frame-notice">
      <Icon name={icon} size={22} />
      <strong>{title}</strong>
      <span>{body}</span>
      {action}
    </div>
  );
}

/** A frame where nothing may run: in a revision preview, which is a picture of the past. */
export function StillFrame({ node }: { node: CanvasNode }) {
  return (
    <>
      <FrameHeader node={node} />
      <div className="cv-frame-body"><FrameNotice icon="file" title={pageName(node)} body={t("canvas.frame.still")} /></div>
    </>
  );
}

/** The policy put first in a shared snapshot: it may load nothing from anywhere, whatever the copy says. */
export const SNAPSHOT_POLICY = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:">`;

/** A frame of a shared canvas: the still copy the board last took, in a frame with an empty sandbox
 *  (no scripts, an opaque origin) and a policy that loads nothing; never a live address. */
export function SnapshotFrame({ node, token, capturedAt }: { node: CanvasNode; token: string; capturedAt: string | undefined }) {
  const [html, setHtml] = useState<string | null>(null);
  useEffect(() => {
    if (!capturedAt) return;
    let active = true;
    fetch(`/api/public/canvases/${encodeURIComponent(token)}/snapshots/${encodeURIComponent(node.id)}`)
      .then((response) => (response.ok ? response.text() : ""))
      .then((text) => { if (active) setHtml(text); })
      .catch(() => { if (active) setHtml(""); });
    return () => { active = false; };
  }, [token, node.id, capturedAt]);
  const label = capturedAt ? t("canvas.frame.snapshot", { time: relTimeLong(capturedAt) }) : undefined;
  return (
    <>
      <FrameHeader node={node} label={label} />
      <div className="cv-frame-body">
        {!capturedAt || html === "" ? (
          <FrameNotice icon="globe" title={pageName(node)} body={t("canvas.frame.shared.none")} />
        ) : html === null ? (
          <div className="cv-frame-wait" aria-hidden="true" />
        ) : (
          <iframe className="cv-frame-page" sandbox="" srcDoc={SNAPSHOT_POLICY + html} title={t("canvas.frame.title", { page: pageName(node) })} referrerPolicy="no-referrer" style={{ width: node.width, height: node.height }} />
        )}
      </div>
    </>
  );
}

/** Memoised host value, so frames re-render only when something they read changes. */
export function useFrameHost(value: FrameHost): FrameHost {
  return useMemo(() => value, [value.canvasId, value.mode, value.shared, value.highlight, value.originsVersion, value.onPicked, value.onBlocked, value.onceFor, value.allowOnce, value.allowAlways]); // eslint-disable-line react-hooks/exhaustive-deps
}
