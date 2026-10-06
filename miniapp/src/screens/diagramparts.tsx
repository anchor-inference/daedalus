// Pieces the diagram list, the editor and the shared page have in common: the scene's shape, the
// canvas's own theme, the app's language as Excalidraw names it, and the thumbnail that loads only once
// it is in view.

import { useEffect, useRef, useState } from "react";
import type { ExcalidrawImperativeAPI } from "@excalidraw/excalidraw/types";
import { api } from "../api";
import { Icon } from "../icons";
import { lang, useLang } from "../i18n";

export type Scene = { elements: any[]; appState: Record<string, unknown>; files: Record<string, unknown> };
export type DiagramItem = { id: string; title: string; version: number; created_at: string; updated_at: string; updated_by?: "user" | "agent"; shared?: boolean };
export type Diagram = DiagramItem & { scene: Scene; share_token?: string };
export type Head = { id: string; title: string; version: number; updated_at: string; updated_by: "user" | "agent" };

export type CanvasTheme = "light" | "dark";
const CANVAS_THEME_KEY = "daedalus.diagrams.theme";
const CANVAS_THEME_EVENT = "daedalus:canvas-theme";

function readCanvasTheme(): CanvasTheme {
  try {
    return localStorage.getItem(CANVAS_THEME_KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

/** The canvas's own theme: light unless the operator switched it in Excalidraw's menu, and never the
 *  app's. Following the app made every diagram dark for whoever used the app dark, and a drawing is a
 *  document with its own colours — a white page, as Excalidraw itself opens. Kept on this device and
 *  shared by the editor, the history preview, the shared page and the thumbnails. */
export function useCanvasTheme(): [CanvasTheme, (theme: CanvasTheme) => void] {
  const [theme, setTheme] = useState<CanvasTheme>(readCanvasTheme);
  useEffect(() => {
    const sync = () => setTheme(readCanvasTheme());
    window.addEventListener(CANVAS_THEME_EVENT, sync);
    window.addEventListener("storage", sync);
    return () => {
      window.removeEventListener(CANVAS_THEME_EVENT, sync);
      window.removeEventListener("storage", sync);
    };
  }, []);
  const choose = (next: CanvasTheme) => {
    if (next === readCanvasTheme()) return;
    try {
      localStorage.setItem(CANVAS_THEME_KEY, next);
    } catch {
      /* the choice lasts for this page */
    }
    setTheme(next);
    window.dispatchEvent(new Event(CANVAS_THEME_EVENT));
  };
  return [theme, choose];
}

/** The app's language as Excalidraw's locale table names it. */
export function useExcalidrawLang(): string {
  useLang();
  return lang() === "ru" ? "ru-RU" : "en";
}

/** The Excalidraw options every canvas here shares. Loading, saving and exporting are the app's own
 *  (the bar above the canvas does them with the diagram's name and version). The theme is the
 *  canvas's own, switched in Excalidraw's menu and remembered by useCanvasTheme. */
export const UI_OPTIONS = {
  canvasActions: { loadScene: false, saveToActiveFile: false, export: false as const, saveAsImage: false, toggleTheme: true, clearCanvas: true, changeViewBackgroundColor: true },
};

/** What makes a scene a different document. Excalidraw rewrites bookkeeping on every redraw, and a
 *  font that finishes loading re-measures every text box; counting either as an edit saved a new
 *  version each time a diagram was merely opened. A bound label's box follows its container, so only
 *  its words matter (the unwrapped ones: the wrapping is measured too); free text keeps its place but not
 *  its measured size. */
export function sceneSignature(elements: readonly any[], files: Record<string, unknown>): string {
  const live = elements.filter((element) => !element.isDeleted).map((element) => {
    const { version: _version, versionNonce: _nonce, updated: _updated, seed: _seed, ...rest } = element;
    if (element.type !== "text") return rest;
    const { width: _width, height: _height, baseline: _baseline, ...text } = rest;
    if (!element.containerId) return text;
    const { x: _x, y: _y, text: _wrapped, ...bound } = text;
    return bound;
  });
  return JSON.stringify({ elements: live, files: Object.keys(files ?? {}).sort() });
}

const previews = new Map<string, string>();

/** A diagram's (or one revision's) thumbnail, fetched when the card scrolls into view. The SVG is drawn
 *  by the server and shown through an image, so nothing in it can run on the page. */
export function DiagramThumb({ path, version, className }: { path: string; version: number; className?: string }) {
  const key = `${path}@${version}`;
  const [svg, setSvg] = useState<string | null>(previews.get(key) ?? null);
  const box = useRef<HTMLDivElement>(null);
  const [theme] = useCanvasTheme();
  useEffect(() => {
    if (previews.has(key)) {
      setSvg(previews.get(key) ?? "");
      return;
    }
    const element = box.current;
    if (!element) return;
    let active = true;
    const load = () => {
      api.get<{ svg: string }>(path).then((result) => {
        previews.set(key, result.svg);
        if (active) setSvg(result.svg);
      }).catch(() => { if (active) setSvg(""); });
    };
    if (typeof IntersectionObserver === "undefined") {
      load();
      return () => { active = false; };
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer.disconnect();
        load();
      }
    }, { rootMargin: "200px" });
    observer.observe(element);
    return () => { active = false; observer.disconnect(); };
  }, [key, path]);
  return (
    <div ref={box} className={`diagram-thumb ${theme} ${className ?? ""}`} aria-hidden="true">
      {svg ? <img src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`} alt="" draggable={false} /> : svg === "" ? <Icon name="pen" size={20} /> : <span className="diagram-thumb-wait" />}
    </div>
  );
}

/** A file name from a title, safe on every file system. */
export function fileName(title: string): string {
  return title.trim().replace(/[^\p{L}\p{N}._-]+/gu, "-").replace(/^-+|-+$/g, "").slice(0, 80) || "diagram";
}

export function download(data: Blob, name: string): void {
  const url = URL.createObjectURL(data);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** The space Excalidraw's own controls take over the canvas: the tool bar at the top and the zoom and
 *  undo bar at the bottom. A drawing fitted under them would open with its edges hidden. */
const FIT_PAD = { side: 24, top: 80, bottom: 72 };
const MIN_ZOOM = 0.1;

/** The drawing's extent in scene coordinates, lines and arrows by their points. */
function sceneBounds(elements: readonly any[]): [number, number, number, number] | null {
  let left = Infinity, top = Infinity, right = -Infinity, bottom = -Infinity;
  for (const element of elements) {
    if (element.isDeleted) continue;
    const points: [number, number][] = Array.isArray(element.points) && element.points.length ? element.points : [[0, 0], [element.width ?? 0, element.height ?? 0]];
    for (const [px, py] of points) {
      left = Math.min(left, element.x + px);
      right = Math.max(right, element.x + px);
      top = Math.min(top, element.y + py);
      bottom = Math.max(bottom, element.y + py);
    }
  }
  return Number.isFinite(left) ? [left, top, right, bottom] : null;
}

/** Show the whole drawing when a canvas opens, at any width. The zoom is the one that fits it inside
 *  the canvas less the space of the editor's controls, never above 100 % (a small drawing is not blown
 *  up) and never below 10 %; the drawing is centred in that space. Excalidraw's own fit only ever
 *  zoomed in here, so on a phone a wide diagram opened cut off at both sides. The scene and the
 *  canvas size arrive a few frames after the canvas does, so this waits for them, briefly. */
export function fitOnOpen(canvas: ExcalidrawImperativeAPI, frames = 60): void {
  window.requestAnimationFrame(() => {
    const box = sceneBounds(canvas.getSceneElements());
    const { width, height } = canvas.getAppState();
    if (!box || !width || !height) {
      if (frames > 0) fitOnOpen(canvas, frames - 1);
      return;
    }
    const [left, top, right, bottom] = box;
    const roomWidth = Math.max(1, width - 2 * FIT_PAD.side);
    const roomHeight = Math.max(1, height - FIT_PAD.top - FIT_PAD.bottom);
    const zoom = Math.max(MIN_ZOOM, Math.min(1, roomWidth / Math.max(1, right - left), roomHeight / Math.max(1, bottom - top)));
    // Excalidraw maps a scene point to the screen as (point + scroll) * zoom, so the scroll that puts
    // the drawing's centre at the centre of the free room is that centre, in scene units, less it.
    const centreX = width / 2;
    const centreY = FIT_PAD.top + roomHeight / 2;
    canvas.updateScene({ appState: { zoom: { value: zoom as any }, scrollX: centreX / zoom - (left + right) / 2, scrollY: centreY / zoom - (top + bottom) / 2 } });
  });
}

/** Write the canvas's zoom and scroll onto its container, where a browser check reads them to say
 *  whether the drawing is on screen; Excalidraw keeps them in state no page script can see. */
export function exposeView(canvas: ExcalidrawImperativeAPI, element: HTMLElement | null): void {
  if (!element) return;
  const write = (scrollX: number, scrollY: number, zoom: { value: number }) => { element.dataset.view = JSON.stringify({ scrollX, scrollY, zoom: zoom.value }); };
  const state = canvas.getAppState();
  write(state.scrollX, state.scrollY, state.zoom);
  canvas.onScrollChange(write);
}
