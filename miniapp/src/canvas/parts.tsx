// Pieces the list, the editor and the shared view have in common: the thumbnail that loads once it
// is in view, the address an attachment is fetched from, and downloads.

import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Icon } from "../icons";

const previews = new Map<string, string>();

/** A canvas's (or one revision's) thumbnail, fetched when the card scrolls into view. The SVG is drawn
 *  by the host and shown through an image, so nothing in it can run on the page. */
export function CanvasThumb({ path, version, className }: { path: string; version: number; className?: string }) {
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
    <div ref={box} className={`cv-thumb ${className ?? ""}`} aria-hidden="true">
      {svg ? <img src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`} alt="" draggable={false} /> : svg === "" ? <Icon name="grid" size={20} /> : <span className="cv-thumb-wait" />}
    </div>
  );
}

/** The address of a canvas attachment. The board fetches it with the app's own credentials (see
 *  `FileImage` in Board.tsx), because a reader in the Telegram app is signed in by a header that a
 *  picture element cannot send. */
export function attachmentUrl(canvasId: string, fileId: string): string {
  return `/api/canvases/${encodeURIComponent(canvasId)}/files/${encodeURIComponent(fileId)}`;
}

export function publicAttachmentUrl(shareToken: string, fileId: string): string {
  return `/api/public/canvases/${encodeURIComponent(shareToken)}/files/${encodeURIComponent(fileId)}`;
}

/** A file name from a title, safe on every file system. */
export function fileName(title: string): string {
  return title.trim().replace(/[^\p{L}\p{N}._-]+/gu, "-").replace(/^-+|-+$/g, "").slice(0, 80) || "canvas";
}

export function download(data: Blob, name: string): void {
  const url = URL.createObjectURL(data);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Asks for one `.canvas` file and gives its text, or null when the reader cancelled. */
export function pickCanvasFile(): Promise<{ name: string; text: string } | null> {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".canvas,application/json";
    input.onchange = () => {
      const file = input.files?.[0];
      if (!file) return resolve(null);
      file.text().then((text) => resolve({ name: file.name.replace(/\.canvas$/i, ""), text }), () => resolve(null));
    };
    input.click();
  });
}
