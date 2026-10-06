// Pieces the diagram list, the editor and the shared page have in common: the scene's shape, the app's
// theme and language as Excalidraw names them, and the thumbnail that loads only once it is in view.

import { useEffect, useRef, useState } from "react";
import type { ExcalidrawImperativeAPI } from "@excalidraw/excalidraw/types";
import { api } from "../api";
import { Icon } from "../icons";
import { lang, useLang } from "../i18n";

export type Scene = { elements: any[]; appState: Record<string, unknown>; files: Record<string, unknown> };
export type DiagramItem = { id: string; title: string; version: number; created_at: string; updated_at: string; updated_by?: "user" | "agent"; shared?: boolean };
export type Diagram = DiagramItem & { scene: Scene; share_token?: string };
export type Head = { id: string; title: string; version: number; updated_at: string; updated_by: "user" | "agent" };

/** The app's colour scheme, followed live: the appearance settings and Telegram both rewrite it on the
 *  document while a diagram is open, and a light canvas inside a dark app glared like a lamp. */
export function useScheme(): "light" | "dark" {
  const read = () => (document.documentElement.dataset.scheme === "light" ? "light" : "dark");
  const [scheme, setScheme] = useState<"light" | "dark">(read);
  useEffect(() => {
    const observer = new MutationObserver(() => setScheme(read()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-scheme"] });
    setScheme(read());
    return () => observer.disconnect();
  }, []);
  return scheme;
}

/** The app's language as Excalidraw's locale table names it. */
export function useExcalidrawLang(): string {
  useLang();
  return lang() === "ru" ? "ru-RU" : "en";
}

/** The Excalidraw options every canvas here shares. Loading, saving and exporting are the app's own
 *  (the bar above the canvas does them with the diagram's name and version), and the theme follows
 *  the app rather than a toggle of its own that the next page would not know about. */
export const UI_OPTIONS = {
  canvasActions: { loadScene: false, saveToActiveFile: false, export: false as const, saveAsImage: false, toggleTheme: false, clearCanvas: true, changeViewBackgroundColor: true },
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
    <div ref={box} className={`diagram-thumb ${className ?? ""}`} aria-hidden="true">
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

/** Show the whole drawing when a canvas opens: on a phone a diagram wider than the screen opened cut
 *  off at both sides. A small drawing is centred at its own size rather than blown up to fill. The
 *  scene arrives a few frames after the canvas does, so this waits for it, briefly. */
export function fitOnOpen(canvas: ExcalidrawImperativeAPI, frames = 30): void {
  window.requestAnimationFrame(() => {
    if (!canvas.getSceneElements().length) {
      if (frames > 0) fitOnOpen(canvas, frames - 1);
      return;
    }
    canvas.scrollToContent(undefined, { fitToViewport: true, viewportZoomFactor: 0.9, animate: false });
    // The zoom it chose is in the state a frame later, not on return.
    window.requestAnimationFrame(() => {
      if (canvas.getAppState().zoom.value <= 1) return;
      canvas.updateScene({ appState: { zoom: { value: 1 as any } } });
      window.requestAnimationFrame(() => canvas.scrollToContent(undefined, { animate: false }));
    });
  });
}
