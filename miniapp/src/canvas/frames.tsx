// Live pages on the board. A frame node is a file node whose `daedalus.frame` says it is a page the
// agent showed; the board draws it as an iframe of that page, served by the services door on another
// origin under a signed address that expires.
//
// The iframe is the second wall. Its sandbox has no `allow-same-origin`, so the page runs with an opaque
// origin even if the door's own header were lost: no cookie, no storage of the app, no popups, no
// navigation of the board around it. Everything the page says arrives through `postMessage` from its
// bridge script and is untrusted text. A message is believed to be from a frame only because it came
// from that frame's window; its `origin` is "null" for every sandboxed page and proves nothing.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, ApiError } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { relTimeLong } from "../format";
import type { CanvasNode } from "./model";

export type FrameInfo = { device: string; rev: number };
export type Mode = "interact" | "comment";
export type Picked = { selector: string; tag: string; text: string; html: string; rect: { x: number; y: number; width: number; height: number } };
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
export function readFrameMessage(data: unknown): ({ type: "loaded"; title: string } | { type: "console"; level: "error" | "warning" | "exception"; text: string } | { type: "blocked"; origin: string } | ({ type: "picked" } & Picked) | { type: "snapshot"; html: string; width: number; height: number }) | null {
  if (!data || typeof data !== "object") return null;
  const message = data as Record<string, unknown>;
  if (message.daedalus !== "canvas-frame") return null;
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
      return { type: "picked", selector: clean(message.selector, 600), tag: clean(message.tag, 40), text: clean(message.text, 400), html: clean(message.html, 2000), rect: { x: number(rect.x), y: number(rect.y), width: number(rect.width), height: number(rect.height) } };
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
  /** The element a chip points at, outlined in its frame while the chip is hovered or focused. */
  highlight: { frameId: string; selector: string } | null;
  /** Bumped when the canvas's allowed origins change: every frame fetches a new address. */
  originsVersion: number;
  onPicked: (frameId: string, picked: Picked) => void;
  onBlocked: (frameId: string, origin: string) => void;
  /** Origins allowed for one load of one frame, until the board closes; stored nowhere. */
  onceFor: (frameId: string) => string[];
};

export const FrameContext = createContext<FrameHost | null>(null);

const SNAPSHOT_QUIET_MS = 2000;
const REPORT_DELAY_MS = 1000;
const LEFT_AFTER_MS = 3000;
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
  const live = useRef(false);
  live.current = state.kind === "live";
  const iframe = useRef<HTMLIFrameElement>(null);
  const counts = useRef({ loads: 0, bridged: 0 });
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
    counts.current = { loads: 0, bridged: 0 };
    api.post<AddressAnswer>(`/api/canvases/${canvasId}/frames/${encodeURIComponent(node.id)}/address`, { once: once ? once.split(" ") : [] })
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

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (!iframe.current || event.source !== iframe.current.contentWindow) return;
      const message = readFrameMessage(event.data);
      if (!message) return;
      if (message.type === "loaded") {
        counts.current.bridged += 1;
        queueReport((pending) => { pending.loaded = true; pending.title = message.title; });
        post({ type: "mode", mode: host?.mode ?? "interact" });
        window.clearTimeout(snapshotTimer.current);
        snapshotTimer.current = window.setTimeout(() => post({ type: "snapshot" }), SNAPSHOT_QUIET_MS);
      } else if (message.type === "console") {
        queueReport((pending) => { pending.lines.push({ level: message.level, text: message.text }); });
      } else if (message.type === "blocked") {
        queueReport((pending) => { if (!pending.blocked.includes(message.origin)) pending.blocked.push(message.origin); });
        host?.onBlocked(node.id, message.origin);
      } else if (message.type === "picked") {
        host?.onPicked(node.id, message);
      } else if (message.type === "snapshot") {
        const key = `${canvasId}:${node.id}`;
        if (sentSnapshots.get(key) === message.html) return;
        sentSnapshots.set(key, message.html);
        void api.put(`/api/canvases/${canvasId}/frames/${encodeURIComponent(node.id)}/snapshot`, { html: message.html, width: message.width, height: message.height }).catch(() => sentSnapshots.delete(key));
      }
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [canvasId, host, node.id, post, queueReport]);

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
        sandbox="allow-scripts allow-forms allow-modals allow-downloads"
        referrerPolicy="no-referrer"
        allow=""
        loading="lazy"
        onLoad={onLoad}
        style={{ width: node.width, height: node.height }}
      />
    );
  })();
  return (
    <>
      <FrameHeader node={node} onReload={reload} />
      <div className="cv-frame-body" data-mode={host?.mode ?? "interact"}>{body}</div>
    </>
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
  return useMemo(() => value, [value.canvasId, value.mode, value.highlight, value.originsVersion, value.onPicked, value.onBlocked, value.onceFor]); // eslint-disable-line react-hooks/exhaustive-deps
}
