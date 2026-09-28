// Where a point of the page is on the screen, and where a point of the screen is on the page.
//
// Three spaces meet in the viewer. The page's own: CSS pixels of its viewport, which is what the
// daemon's actions and the operator's input are written in. The frame's: pixels of the JPEG, `meta.w`
// by `meta.h`, which may be a scaled copy (a phone gets 640 px of a 1280 px page, a slow link half of
// that). And the element's: client pixels of the box the frame is drawn into, letterboxed to keep its
// proportions and, on a phone, zoomed by the operator's pinch.
//
// Every mapping goes through the frame's own metadata, never through a guess from the element's size:
// a frame at half size after the link slowed down still puts the agent's cursor on the right button.

import type { Box, FrameMeta, Point } from "./protocol";

/** Where the image is drawn, in client pixels relative to the viewer. */
export type Rect = { x: number; y: number; w: number; h: number };

/** The operator's pinch on a phone: a scale about the viewer's centre, then a pan. 1/0/0 is none. */
export type Zoom = { scale: number; panX: number; panY: number };
export const NO_ZOOM: Zoom = { scale: 1, panX: 0, panY: 0 };
export const ZOOM_MAX = 3;

/**
 * The image contained in the box, its proportions kept (`object-fit: contain`), and centred both ways.
 * The panel used to hold it to the top, as a browser window holds its page, but the remote browser's
 * proportions are fixed, so a tall panel showed the page and then a third of its height in plain
 * black above the action log, which read as a page that had failed to render.
 */
export function fit(boxW: number, boxH: number, imageW: number, imageH: number): Rect {
  if (boxW <= 0 || boxH <= 0 || imageW <= 0 || imageH <= 0) return { x: 0, y: 0, w: 0, h: 0 };
  const scale = Math.min(boxW / imageW, boxH / imageH);
  const w = imageW * scale;
  const h = imageH * scale;
  return { x: (boxW - w) / 2, y: (boxH - h) / 2, w, h };
}

/** The fitted rectangle with the pinch applied: scaled about the box's centre, then moved. */
export function zoomed(rect: Rect, boxW: number, boxH: number, zoom: Zoom): Rect {
  if (zoom.scale === 1 && zoom.panX === 0 && zoom.panY === 0) return rect;
  const cx = boxW / 2;
  const cy = boxH / 2;
  return {
    x: cx + (rect.x - cx) * zoom.scale + zoom.panX,
    y: cy + (rect.y - cy) * zoom.scale + zoom.panY,
    w: rect.w * zoom.scale,
    h: rect.h * zoom.scale,
  };
}

/** A pinch never zooms out past the fitted picture, and a pan never loses the picture off an edge. */
export function clampZoom(zoom: Zoom, rect: Rect, boxW: number, boxH: number): Zoom {
  const scale = Math.min(ZOOM_MAX, Math.max(1, zoom.scale));
  if (scale === 1) return NO_ZOOM;
  const w = rect.w * scale;
  const h = rect.h * scale;
  const limitX = Math.max(0, (w - boxW) / 2);
  const limitY = Math.max(0, (h - boxH) / 2);
  return { scale, panX: Math.min(limitX, Math.max(-limitX, zoom.panX)), panY: Math.min(limitY, Math.max(-limitY, zoom.panY)) };
}

/** Image pixels per CSS pixel of the page, counting the page's own zoom. */
function density(meta: FrameMeta): number {
  return (meta.w / Math.max(1, meta.vw)) * (meta.page_scale || 1);
}

/** A point in the page's viewport (CSS px) as client pixels in the viewer. */
export function pageToView(meta: FrameMeta, rect: Rect, p: Point): Point {
  const d = density(meta);
  const perImage = rect.w / Math.max(1, meta.w);
  const top = (meta.offset_top || 0) * (meta.w / Math.max(1, meta.vw));
  return { x: rect.x + p.x * d * perImage, y: rect.y + (p.y * d + top) * perImage };
}

/** An element's box in the page's viewport as a box in the viewer. */
export function boxToView(meta: FrameMeta, rect: Rect, b: Box): Box {
  const a = pageToView(meta, rect, { x: b.x, y: b.y });
  const z = pageToView(meta, rect, { x: b.x + b.w, y: b.y + b.h });
  return { x: a.x, y: a.y, w: z.x - a.x, h: z.y - a.y };
}

/**
 * A client point in the viewer as a point in the page's viewport, or null when it is on the
 * letterbox rather than the picture. Rounded to a tenth of a pixel: the daemon does not need more,
 * and a click that reads 311.49999 in one browser and 311.5 in another is the same click.
 */
export function viewToPage(meta: FrameMeta, rect: Rect, clientX: number, clientY: number): Point | null {
  if (rect.w <= 0 || rect.h <= 0) return null;
  if (clientX < rect.x || clientY < rect.y || clientX > rect.x + rect.w || clientY > rect.y + rect.h) return null;
  const d = density(meta);
  const perImage = rect.w / Math.max(1, meta.w);
  const top = (meta.offset_top || 0) * (meta.w / Math.max(1, meta.vw));
  const x = (clientX - rect.x) / perImage / d;
  const y = ((clientY - rect.y) / perImage - top) / d;
  const round = (v: number) => Math.round(v * 10) / 10;
  return { x: round(Math.min(meta.vw, Math.max(0, x))), y: round(Math.min(meta.vh, Math.max(0, y))) };
}

/** A drag of the finger on the picture as a wheel step in the page, in CSS pixels, the other way round. */
export function dragToWheel(meta: FrameMeta, rect: Rect, dxClient: number, dyClient: number): { dx: number; dy: number } {
  const perCss = (rect.w / Math.max(1, meta.w)) * density(meta);
  const round = (v: number) => Math.round(v * 10) / 10 || 0;
  return { dx: round(-dxClient / perCss), dy: round(-dyClient / perCss) };
}

/**
 * The box a live view asks the daemon for: the element's size in device pixels, never above what the
 * daemon sends (1600×1000), and smaller under data saving. Rounded, since the daemon wants integers.
 */
export function attachBox(cssW: number, cssH: number, dpr: number, saving: boolean): { max_w: number; max_h: number; dpr: number; quality?: number } {
  const cap = saving ? 640 : 1600;
  const capH = saving ? 400 : 1000;
  const ratio = Math.max(1, Math.min(dpr || 1, 3));
  const w = Math.max(64, Math.min(cap, Math.round(cssW * ratio)));
  const h = Math.max(64, Math.min(capH, Math.round(cssH * ratio)));
  return saving ? { max_w: w, max_h: h, dpr: ratio, quality: 45 } : { max_w: w, max_h: h, dpr: ratio };
}
