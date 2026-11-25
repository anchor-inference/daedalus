// The picture of a page: a canvas the live view draws on, letterboxed in whatever box it is given, the
// agent's cursor above it, and — while the operator drives — their mouse, keyboard or fingers going to
// the page.
//
// Input is the operator's only while this window holds control: the server drops anyone else's, and
// the viewer does not send it in the first place. A mouse sends where it is in the page's own pixels,
// read back through the frame's metadata; moves are coalesced to one per animation frame, so a fast
// hand does not queue hundreds of events behind the page. Text arrives through a hidden field, because
// that is where a browser hands over what an IME, a phone's keyboard or a paste composed (keys.ts).
// The clipboard crosses here too: a paste is the operator's clipboard typed onto the page, a copy the
// page's selection written to the operator's clipboard (clipboard.ts).
//
// On a phone a tap is a click, one finger dragged is a scroll, a long press is a right click, and two
// fingers zoom this picture (never the page, whose layout the agent reads).
//
// While the operator drives, the picture zooms: the buttons in its corner, Ctrl (⌘) with plus, minus
// and zero, Ctrl with the wheel (a trackpad's pinch) about the pointer, two fingers on a phone; the
// middle button drags it. Enlarging the live frame alone shows nothing new — it has the page's window's
// pixels at most, and a sign-in code drawn one pixel a module stays a grey blur however large — so a
// zoomed view asks for its frames whole at JPEG 92, and once the zoom settles lays a sharp frame over
// them: the zoomed part drawn again by the browser at the screen's own pixels (`/still`). Nothing
// changes on the page itself for any of this, so handing the page back, which resets the zoom, leaves
// the agent the page exactly as it was.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { t } from "../i18n";
import { Icon } from "../icons";
import { InputDeduper } from "../terminal/dedupe";
import { api } from "../api";
import { attachBox, boxToView, clampZoom, dragToWheel, fit, NO_ZOOM, panBy, stillScale, viewOf, viewToPage, visibleClip, zoomAbout, zoomed, ZOOM_MAX, ZOOM_STEP, type PictureQuality, type Rect, type Zoom } from "./geometry";
import { copyFromPage, pasteText } from "./clipboard";
import { clipboardChord, ESCAPE_TWICE_MS, isKeyPress, keyInput, modsOf, mouseButton, viewerChord, zoomChord, type ViewerChord, type ZoomChord } from "./keys";
import type { Box } from "./protocol";
import type { LiveSnapshot, LiveView } from "./live";
import { CursorOverlay } from "./overlay";
import "./panel.css";

/** A finger that moves less than this is still a tap. */
const TAP_SLOP_PX = 8;
/** Held this long without moving, a finger is a right click. */
const LONG_PRESS_MS = 520;
/** A resize settles before the daemon is asked for another size. */
const RESIZE_SETTLE_MS = 180;
/** The zoom, a pan or the operator's own input settles this long before a sharp frame is taken. */
const STILL_SETTLE_MS = 400;
/** A page that keeps changing under a sharp frame (a code that renews itself) gets a new one this often. */
const STILL_REFRESH_MS = 2500;

/** A sharp frame over the live picture: the part of the page it shows, and which tab. */
type Still = { url: string; clip: Box; tab: string; ts: number };

export type ViewerProps = {
  live: LiveView | null;
  snap: LiveSnapshot;
  tier: "live" | "thumb";
  /** This window holds control: input goes to the page. */
  interactive: boolean;
  /** Fingers rather than a mouse (a phone's takeover). */
  touch?: boolean;
  /** The thumbnail: a small cursor, no words, no placeholder text. */
  compact?: boolean;
  /** The agent's name on its cursor. */
  agent: string;
  saving?: boolean;
  /** The operator's choice between a fluid and a sharp picture. */
  quality?: PictureQuality;
  onChord?: (chord: ViewerChord) => void;
  /** Two presses of Escape: the keyboard goes back to the app. */
  onRelease?: () => void;
  /** Called with the viewer's element, for the handoff animation. */
  stageRef?: (el: HTMLDivElement | null) => void;
  className?: string;
  style?: CSSProperties;
  children?: ReactNode;
};

export function BrowserViewer({ live, snap, tier, interactive, touch = false, compact = false, agent, saving = false, quality = "auto", onChord, onRelease, stageRef, className, style, children }: ViewerProps) {
  const box = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [zoom, setZoom] = useState<Zoom>(NO_ZOOM);
  const [still, setStill] = useState<Still | null>(null);
  const meta = snap.meta;
  const zoomable = interactive && !compact;

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => setSize((s) => (s.w === el.clientWidth && s.h === el.clientHeight ? s : { w: el.clientWidth, h: el.clientHeight }));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    live?.attach(canvas.current);
    return () => live?.attach(null);
  }, [live]);

  // A live picture follows the box it is drawn in; the thumbnail's size is the daemon's. Zoomed, it
  // asks for more pixels: in half steps, so a pinch does not send a VIEW for every frame it moves.
  const zoomLevel = Math.min(ZOOM_MAX, Math.round(zoom.scale * 2) / 2);
  const asked = useRef("");
  useEffect(() => {
    if (!live || tier !== "live" || size.w <= 0 || snap.state.kind !== "live") return;
    const timer = window.setTimeout(() => {
      const want = viewOf(attachBox(size.w, size.h, window.devicePixelRatio || 1, saving, quality, zoomLevel));
      const key = `${want.max_w}x${want.max_h} q${want.quality} f${want.fps} ${want.adaptive}`;
      if (key === asked.current) return;
      asked.current = key;
      live.view(want);
    }, RESIZE_SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [live, tier, size.w, size.h, saving, quality, snap.state.kind, zoomLevel]);

  const base = useMemo<Rect>(() => (meta ? fit(size.w, size.h, meta.w, meta.h) : { x: 0, y: 0, w: 0, h: 0 }), [meta, size.w, size.h]);
  const rect = useMemo(() => zoomed(base, size.w, size.h, zoom), [base, size.w, size.h, zoom]);
  const rectRef = useRef(rect);
  rectRef.current = rect;
  const geometry = useRef({ base, w: size.w, h: size.h });
  geometry.current = { base, w: size.w, h: size.h };
  const zoomRef = useRef(zoom);
  zoomRef.current = zoom;

  // ── the zoom ────────────────────────────────────────────────────────────────────────────────
  const zoomBy = useCallback((factor: number, at?: { x: number; y: number }) => {
    const g = geometry.current;
    setZoom((z) => zoomAbout(z, g.base, g.w, g.h, at?.x ?? g.w / 2, at?.y ?? g.h / 2, factor));
  }, []);
  const onZoomChord = useCallback((chord: ZoomChord) => {
    if (chord === "reset") setZoom(NO_ZOOM);
    else zoomBy(chord === "in" ? ZOOM_STEP : 1 / ZOOM_STEP);
  }, [zoomBy]);

  // The sharp frame. Each request is numbered, so an answer that comes after the picture moved again
  // is thrown away rather than laid over the wrong part of the page.
  const stillSeq = useRef(0);
  const stillBusy = useRef(false);
  // Asked for while one was on its way: taken when that one is back, or the settled picture would
  // wait for the next change to get its sharp frame.
  const stillAgain = useRef(false);
  const stillTimer = useRef(0);
  const dropStill = useCallback(() => {
    stillSeq.current++;
    window.clearTimeout(stillTimer.current);
    setStill((old) => {
      if (old) URL.revokeObjectURL(old.url);
      return null;
    });
  }, []);
  const takeStill = useCallback(async () => {
    const m = live?.latest ?? meta;
    const g = geometry.current;
    if (!live || !m) return;
    if (stillBusy.current) {
      stillAgain.current = true;
      return;
    }
    const clip = visibleClip(m, rectRef.current, g.w, g.h);
    if (!clip) return;
    const seq = ++stillSeq.current;
    stillBusy.current = true;
    try {
      const url = await api.browserStill(live.options.group, { tab_id: m.tab, ...clip, scale: stillScale(m, rectRef.current, window.devicePixelRatio || 1) });
      if (seq !== stillSeq.current) {
        URL.revokeObjectURL(url);
        return;
      }
      setStill((old) => {
        if (old) URL.revokeObjectURL(old.url);
        return { url, clip, tab: m.tab, ts: m.ts };
      });
    } catch {
      // The live picture stays; the operator can ask again from the zoom's corner.
    } finally {
      stillBusy.current = false;
    }
    if (stillAgain.current) {
      stillAgain.current = false;
      void takeStillRef.current();
    }
  }, [live, meta]);
  const takeStillRef = useRef(takeStill);
  takeStillRef.current = takeStill;
  const settleStill = useCallback(() => {
    dropStill();
    if (zoomRef.current.scale > 1) stillTimer.current = window.setTimeout(() => void takeStill(), STILL_SETTLE_MS);
  }, [dropStill, takeStill]);

  // Every change of what the box shows puts the sharp frame out of place: it goes, and a new one is
  // taken once the picture stands still.
  useEffect(() => {
    if (!zoomable) return;
    settleStill();
  }, [zoomable, zoom, size.w, size.h, meta?.tab, settleStill]);
  // A page that changes under the sharp frame — a code that renews itself — gets a fresh one.
  useEffect(() => {
    if (!zoomable || zoom.scale <= 1 || !still) return;
    const timer = window.setInterval(() => {
      const newest = live?.latest;
      if (newest && newest.ts !== still.ts) void takeStill();
    }, STILL_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [zoomable, zoom.scale, still, live, takeStill]);
  useEffect(() => () => {
    window.clearTimeout(stillTimer.current);
  }, []);

  // Losing control leaves the zoom, the sharp frame and the keyboard behind.
  useEffect(() => {
    if (!interactive) {
      setZoom(NO_ZOOM);
      dropStill();
      field.current?.blur();
    }
  }, [interactive, dropStill]);

  const send = useCallback((message: Parameters<LiveView["input"]>[0]) => {
    if (!interactive || !live) return false;
    // What the operator does on the page may change it under the sharp frame; a pointer passing over
    // it does not, or the frame would never stay.
    if (still && !(message.t === "mouse" && message.type === "move")) settleStill();
    return live.input(message);
  }, [interactive, live, still, settleStill]);

  /** A client point as the page's, from the newest frame (a scroll moves nothing we map by). */
  const pagePoint = useCallback((clientX: number, clientY: number) => {
    const el = box.current;
    const m = live?.latest ?? meta;
    if (!el || !m) return null;
    const r = el.getBoundingClientRect();
    return viewToPage(m, rectRef.current, clientX - r.left, clientY - r.top);
  }, [live, meta]);

  // ── the mouse ───────────────────────────────────────────────────────────────────────────────
  const moveQueued = useRef<{ x: number; y: number; buttons: number; mods: number } | null>(null);
  const moveFrame = useRef(0);
  const flushMove = useCallback(() => {
    moveFrame.current = 0;
    const m = moveQueued.current;
    moveQueued.current = null;
    if (!m) return;
    send({ t: "mouse", type: "move", x: m.x, y: m.y, button: m.buttons & 1 ? "left" : m.buttons & 2 ? "right" : "none", clicks: 0, mods: m.mods });
  }, [send]);
  const wheelQueued = useRef<{ x: number; y: number; dx: number; dy: number; mods: number } | null>(null);
  const wheelFrame = useRef(0);
  const flushWheel = useCallback(() => {
    wheelFrame.current = 0;
    const w = wheelQueued.current;
    wheelQueued.current = null;
    if (w && (w.dx || w.dy)) send({ t: "wheel", x: w.x, y: w.y, dx: Math.round(w.dx), dy: Math.round(w.dy), mods: w.mods });
  }, [send]);
  useEffect(() => () => {
    cancelAnimationFrame(moveFrame.current);
    cancelAnimationFrame(wheelFrame.current);
  }, []);

  // The wheel must be a non-passive listener to keep the app's own column from scrolling.
  useEffect(() => {
    const el = box.current;
    if (!el || !interactive || touch) return;
    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) {
        // A trackpad's pinch arrives as a wheel with Ctrl held. Sent on, either would zoom the remote
        // page itself, which changes its layout under the agent; it zooms the picture instead.
        e.preventDefault();
        const r = el.getBoundingClientRect();
        const step = e.deltaMode === 1 ? 0.05 : e.deltaMode === 2 ? 1 : 0.0025;
        zoomBy(Math.exp(-e.deltaY * step), { x: e.clientX - r.left, y: e.clientY - r.top });
        return;
      }
      const p = pagePoint(e.clientX, e.clientY);
      if (!p) return;
      e.preventDefault();
      const scale = e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? 800 : 1;
      const q = wheelQueued.current ?? { x: p.x, y: p.y, dx: 0, dy: 0, mods: modsOf(e) };
      q.dx += e.deltaX * scale;
      q.dy += e.deltaY * scale;
      wheelQueued.current = q;
      if (!wheelFrame.current) wheelFrame.current = requestAnimationFrame(flushWheel);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [interactive, touch, pagePoint, flushWheel, zoomBy]);

  const panning = useRef<{ x: number; y: number; zoom0: Zoom } | null>(null);

  // ── fingers ─────────────────────────────────────────────────────────────────────────────────
  const fingers = useRef(new Map<number, { x: number; y: number; x0: number; y0: number; t0: number }>());
  const gesture = useRef<{ kind: "tap" | "drag" | "pinch" | "done"; timer: number; dist0: number; mid0: { x: number; y: number }; zoom0: Zoom }>({ kind: "tap", timer: 0, dist0: 0, mid0: { x: 0, y: 0 }, zoom0: NO_ZOOM });

  const click = useCallback((clientX: number, clientY: number, button: "left" | "right", clicks: number, mods = 0) => {
    const p = pagePoint(clientX, clientY);
    if (!p) return;
    send({ t: "mouse", type: "move", x: p.x, y: p.y, button: "none", clicks: 0, mods });
    send({ t: "mouse", type: "down", x: p.x, y: p.y, button, clicks, mods });
    send({ t: "mouse", type: "up", x: p.x, y: p.y, button, clicks, mods });
  }, [pagePoint, send]);

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!interactive) return;
    if (e.pointerType === "touch" || touch) {
      e.currentTarget.setPointerCapture?.(e.pointerId);
      fingers.current.set(e.pointerId, { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, t0: performance.now() });
      const g = gesture.current;
      window.clearTimeout(g.timer);
      if (fingers.current.size === 1) {
        g.kind = "tap";
        g.timer = window.setTimeout(() => {
          if (g.kind !== "tap") return;
          g.kind = "done";
          const f = fingers.current.get(e.pointerId);
          if (f) click(f.x, f.y, "right", 1);
        }, LONG_PRESS_MS);
      } else if (fingers.current.size === 2) {
        const [a, b] = [...fingers.current.values()];
        g.kind = "pinch";
        g.dist0 = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        g.mid0 = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        g.zoom0 = zoom;
      }
      return;
    }
    // The middle button drags a zoomed picture rather than clicking the page.
    if (e.button === 1 && zoomable && zoomRef.current.scale > 1) {
      e.preventDefault();
      e.currentTarget.setPointerCapture?.(e.pointerId);
      panning.current = { x: e.clientX, y: e.clientY, zoom0: zoomRef.current };
      return;
    }
    // A mouse: the keyboard follows the click, as it would into a page.
    field.current?.focus({ preventScroll: true });
    const p = pagePoint(e.clientX, e.clientY);
    if (!p) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture?.(e.pointerId);
    flushMove();
    send({ t: "mouse", type: "down", x: p.x, y: p.y, button: mouseButton(e.button), clicks: Math.max(1, e.detail || 1), mods: modsOf(e) });
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!interactive) return;
    if (panning.current) {
      // From where the drag began, not from the last move: a move the browser merged or dropped
      // would otherwise leave the picture behind the pointer for the rest of the drag.
      const g = geometry.current;
      const start = panning.current;
      setZoom(panBy(start.zoom0, g.base, g.w, g.h, e.clientX - start.x, e.clientY - start.y));
      return;
    }
    if (e.pointerType === "touch" || touch) {
      const f = fingers.current.get(e.pointerId);
      if (!f) return;
      const dx = e.clientX - f.x;
      const dy = e.clientY - f.y;
      f.x = e.clientX;
      f.y = e.clientY;
      const g = gesture.current;
      if (g.kind === "pinch" && fingers.current.size >= 2) {
        const [a, b] = [...fingers.current.values()];
        const dist = Math.hypot(a.x - b.x, a.y - b.y) || 1;
        const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
        setZoom(clampZoom({ scale: g.zoom0.scale * (dist / g.dist0), panX: g.zoom0.panX + (mid.x - g.mid0.x), panY: g.zoom0.panY + (mid.y - g.mid0.y) }, base, size.w, size.h));
        return;
      }
      if (g.kind === "tap" && Math.hypot(e.clientX - f.x0, e.clientY - f.y0) > TAP_SLOP_PX) {
        g.kind = "drag";
        window.clearTimeout(g.timer);
      }
      if (g.kind === "drag") {
        const p = pagePoint(f.x0, f.y0);
        const m = live?.latest ?? meta;
        if (!p || !m) return;
        const step = dragToWheel(m, rectRef.current, dx, dy);
        const q = wheelQueued.current ?? { x: p.x, y: p.y, dx: 0, dy: 0, mods: 0 };
        q.dx += step.dx;
        q.dy += step.dy;
        wheelQueued.current = q;
        if (!wheelFrame.current) wheelFrame.current = requestAnimationFrame(flushWheel);
      }
      return;
    }
    const p = pagePoint(e.clientX, e.clientY);
    if (!p) return;
    moveQueued.current = { x: p.x, y: p.y, buttons: e.buttons, mods: modsOf(e) };
    if (!moveFrame.current) moveFrame.current = requestAnimationFrame(flushMove);
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!interactive) return;
    if (panning.current) {
      panning.current = null;
      return;
    }
    if (e.pointerType === "touch" || touch) {
      const f = fingers.current.get(e.pointerId);
      fingers.current.delete(e.pointerId);
      const g = gesture.current;
      window.clearTimeout(g.timer);
      if (f && g.kind === "tap" && fingers.current.size === 0) click(f.x0, f.y0, "left", 1);
      if (g.kind === "drag") flushWheel();
      if (fingers.current.size === 0) g.kind = "tap";
      else if (g.kind === "pinch") g.kind = "done";
      return;
    }
    const p = pagePoint(e.clientX, e.clientY);
    flushMove();
    if (!p) return;
    send({ t: "mouse", type: "up", x: p.x, y: p.y, button: mouseButton(e.button), clicks: Math.max(1, e.detail || 1), mods: modsOf(e) });
  };

  const onPointerCancel = (e: React.PointerEvent<HTMLDivElement>) => {
    panning.current = null;
    fingers.current.delete(e.pointerId);
    window.clearTimeout(gesture.current.timer);
    if (fingers.current.size === 0) gesture.current.kind = "tap";
  };

  // ── the keyboard ────────────────────────────────────────────────────────────────────────────
  const composing = useRef(false);
  const lastEscape = useRef(0);
  const deduper = useRef(new InputDeduper());
  const mac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

  const sendText = (text: string) => {
    if (!text) return;
    // A phone's keyboard can hand the same word over twice (terminal/dedupe.ts); a person at a
    // hardware keyboard never does, so the filter is only in the way there.
    if (touch && !deduper.current.accept(text, performance.now())) return;
    send({ t: "text", text });
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (!interactive || e.nativeEvent.isComposing || composing.current) return;
    const zooming = zoomable ? zoomChord(e, mac) : null;
    if (zooming) {
      e.preventDefault();
      onZoomChord(zooming);
      return;
    }
    const chord = viewerChord(e, mac);
    if (chord) {
      e.preventDefault();
      if (chord !== "swallow") onChord?.(chord);
      return;
    }
    if (e.key === "Escape") {
      const now = performance.now();
      if (now - lastEscape.current < ESCAPE_TWICE_MS) {
        lastEscape.current = 0;
        e.preventDefault();
        field.current?.blur();
        onRelease?.();
        return;
      }
      lastEscape.current = now;
    }
    const clip = clipboardChord(e, mac);
    if (clip === "paste") {
      // Not prevented, and not sent: the browser's own paste then fires on the hidden field with the
      // operator's clipboard (onPaste). Sent as keys, the page's browser pasted its own clipboard,
      // which is empty, and a password had to be typed by hand.
      flushField();
      return;
    }
    if (!isKeyPress(e)) return;
    e.preventDefault();
    // A key must not overtake the text typed just before it.
    flushField();
    // The copy is asked for before the key goes: a cut's key deletes the selection it reads.
    if ((clip === "copy" || clip === "cut") && !e.repeat && interactive) void copyFromPage(live);
    send(keyInput(e, "down"));
  };

  const onKeyUp = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (!interactive || composing.current || !isKeyPress(e) || clipboardChord(e, mac) === "paste") return;
    // The zoom's chord went nowhere on its way down, so its release does not go either.
    if (zoomable && zoomChord(e, mac)) {
      e.preventDefault();
      return;
    }
    e.preventDefault();
    send(keyInput(e, "up"));
  };

  /** Whatever the hidden field holds goes to the page as text, and the field is emptied. */
  const flushField = () => {
    const el = field.current;
    if (!el || composing.current || !el.value) return;
    const text = el.value;
    el.value = "";
    sendText(text);
  };

  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (!interactive) return;
    const text = e.clipboardData.getData("text/plain");
    e.preventDefault();
    if (text) pasteText(live, text);
  };

  const state = snap.state.kind;
  const parked = snap.control?.owner === "human";
  const placeholder = !snap.painted && !compact;
  return (
    <div
      ref={(el) => {
        box.current = el;
        stageRef?.(el);
      }}
      className={`bv ${interactive ? "driving" : ""} ${touch ? "touch" : ""} ${compact ? "compact" : ""} ${className ?? ""}`}
      data-state={state}
      data-owner={snap.control?.owner ?? ""}
      data-holder={snap.control?.holder ?? ""}
      data-zoom={zoom.scale.toFixed(2)}
      data-still={still ? "shown" : ""}
      style={style}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onContextMenu={(e) => { if (interactive) e.preventDefault(); }}
    >
      <canvas
        ref={canvas}
        className={`bv-canvas ${snap.painted ? "painted" : ""}`}
        style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }}
        data-rect={`${rect.x.toFixed(1)},${rect.y.toFixed(1)},${rect.w.toFixed(1)},${rect.h.toFixed(1)}`}
      />
      {still && meta && still.tab === meta.tab && <StillFrame still={still} meta={meta} rect={rect} />}
      <CursorOverlay action={snap.action} meta={meta} rect={rect} parked={parked} compact={compact} agent={agent} />
      {zoomable && snap.painted && (
        <ZoomBar
          scale={zoom.scale}
          sharp={!!still}
          mac={mac}
          onChord={onZoomChord}
          onSharp={() => {
            dropStill();
            void takeStill();
          }}
        />
      )}
      {placeholder && (
        <div className="bv-placeholder">
          <Icon name="globe" size={22} />
          <span>{state === "unavailable" ? t("browser.state.gone") : state === "reconnecting" ? t("browser.state.reconnecting") : t("browser.state.connecting")}</span>
        </div>
      )}
      {interactive && (
        <textarea
          ref={field}
          className="bv-ime"
          aria-label={t("browser.keyboard")}
          autoCapitalize="off"
          autoComplete="off"
          spellCheck={false}
          onKeyDown={onKeyDown}
          onKeyUp={onKeyUp}
          onInput={() => flushField()}
          onCompositionStart={() => { composing.current = true; }}
          onCompositionEnd={() => {
            composing.current = false;
            flushField();
          }}
          onPaste={onPaste}
          onBlur={() => deduper.current.reset()}
        />
      )}
      {children}
    </div>
  );
}

/** Put the keyboard on the page: the hidden field inside a viewer that is driving. */
export function focusViewer(root: HTMLElement | null): void {
  root?.querySelector<HTMLTextAreaElement>(".bv-ime")?.focus({ preventScroll: true });
}

/** The sharp frame laid exactly over the part of the live picture it was taken of. */
function StillFrame({ still, meta, rect }: { still: Still; meta: NonNullable<LiveSnapshot["meta"]>; rect: Rect }) {
  const at = boxToView(meta, rect, still.clip);
  return <img className="bv-still" src={still.url} alt="" draggable={false} style={{ left: at.x, top: at.y, width: at.w, height: at.h }} data-clip={`${still.clip.x},${still.clip.y},${still.clip.w},${still.clip.h}`} />;
}

/**
 * The zoom's corner: out, the scale (which puts it back), in, and a sharp frame taken now. Its pointer
 * events stop here, or a press on a button would also be a click on the page beneath it.
 */
function ZoomBar({ scale, sharp, mac, onChord, onSharp }: { scale: number; sharp: boolean; mac: boolean; onChord: (chord: ZoomChord) => void; onSharp: () => void }) {
  const mod = mac ? "⌘" : "Ctrl";
  const stop = (e: React.SyntheticEvent) => e.stopPropagation();
  return (
    <div className="bv-zoom" role="toolbar" aria-label={t("browser.zoom")} onPointerDown={stop} onPointerUp={stop} onPointerMove={stop} onWheel={stop} onContextMenu={stop}>
      <button type="button" className="iconbtn small" aria-label={t("browser.zoom.out", { mod })} title={t("browser.zoom.out", { mod })} disabled={scale <= 1} onClick={() => onChord("out")}>
        <Icon name="minus" size={16} />
      </button>
      <button type="button" className="bv-zoom-level" aria-label={t("browser.zoom.reset", { mod })} title={t("browser.zoom.reset", { mod })} onClick={() => onChord("reset")}>
        {Math.round(scale * 100)}%
      </button>
      <button type="button" className="iconbtn small" aria-label={t("browser.zoom.in", { mod })} title={t("browser.zoom.in", { mod })} disabled={scale >= ZOOM_MAX} onClick={() => onChord("in")}>
        <Icon name="plus" size={16} />
      </button>
      <button type="button" className={`iconbtn small bv-zoom-sharp ${sharp ? "on" : ""}`} aria-pressed={sharp} aria-label={t("browser.zoom.sharp")} title={t("browser.zoom.sharp")} onClick={onSharp}>
        <Icon name="camera" size={16} />
      </button>
    </div>
  );
}
