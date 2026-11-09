// What the editor did to a picture, as a list of steps replayed over the untouched original: a mark, a
// crop, a quarter turn. A crop that cut the picture for good could not be undone, and a turn applied to
// the pixels would blur them on every press; replayed from the original, Undo after a crop brings back
// what was cut, and four turns give back the very same pixels.
//
// Each step's coordinates are in the picture as it stood when the step was taken, after the crops and
// turns before it, which is the only frame the operator saw while drawing it.

import { blurArea, drawMark, meaningful, type Mark } from "./draw";

export type Rect = { x: number; y: number; w: number; h: number };
export type Op = { kind: "mark"; mark: Mark } | { kind: "crop"; rect: Rect } | { kind: "rotate" };

/** The steps, and what Undo and Redo walk through. Clear is a step too, so a slip of the finger is undone. */
export type History = { ops: Op[]; past: Op[][]; future: Op[][] };
export type HistoryAction = { kind: "add"; op: Op } | { kind: "undo" } | { kind: "redo" } | { kind: "clear" };

export const EMPTY_HISTORY: History = { ops: [], past: [], future: [] };

/** A history that opens on steps taken before, with Undo able to take each of them back. */
export function historyOf(ops: Op[]): History {
  return { ops, past: ops.map((_, i) => ops.slice(0, i)), future: [] };
}

/** The smallest crop kept: a tap with the crop tool must not cut the picture down to a speck. */
export const LEAST_CROP = 8;

export function worth(op: Op): boolean {
  if (op.kind === "mark") return meaningful(op.mark);
  if (op.kind === "crop") return op.rect.w >= LEAST_CROP && op.rect.h >= LEAST_CROP;
  return true;
}

export function historyReducer(state: History, action: HistoryAction): History {
  switch (action.kind) {
    case "add":
      return worth(action.op) ? { ops: [...state.ops, action.op], past: [...state.past, state.ops], future: [] } : state;
    case "undo":
      return state.past.length
        ? { ops: state.past[state.past.length - 1], past: state.past.slice(0, -1), future: [state.ops, ...state.future] }
        : state;
    case "redo":
      return state.future.length ? { ops: state.future[0], past: [...state.past, state.ops], future: state.future.slice(1) } : state;
    case "clear":
      return state.ops.length ? { ops: [], past: [...state.past, state.ops], future: [] } : state;
  }
}

/** A crop held inside the picture and to whole pixels, since a canvas cannot be a fraction of one wide. */
export function cropRect(rect: Rect, width: number, height: number): Rect {
  const x = Math.max(0, Math.round(rect.x));
  const y = Math.max(0, Math.round(rect.y));
  return { x, y, w: Math.min(width, Math.round(rect.x + rect.w)) - x, h: Math.min(height, Math.round(rect.y + rect.h)) - y };
}

/** The picture's size after the steps: a crop takes its rectangle's, a turn swaps the sides. */
export function sizeAfter(width: number, height: number, ops: Op[]): { width: number; height: number } {
  let size = { width, height };
  for (const op of ops) {
    if (op.kind === "crop") size = { width: op.rect.w, height: op.rect.h };
    else if (op.kind === "rotate") size = { width: size.height, height: size.width };
  }
  return size;
}

/** Whether `head` is where `ops` began, step for step, so only the steps after it need drawing. */
export function startsWith(ops: Op[], head: Op[]): boolean {
  return head.length <= ops.length && head.every((op, i) => op === ops[i]);
}

/**
 * The largest size, at the picture's proportions, that a canvas may safely be. Safari refuses a
 * canvas over sixteen million pixels and draws nothing, without an error; a phone keeps every such
 * canvas in memory at four bytes a pixel. A larger photo is drawn smaller: a model shrinks any picture
 * well below this before it reads it, so nothing it could see is lost.
 */
export const MAX_PIXELS = 16_000_000;

export function fitPixels(width: number, height: number, most = MAX_PIXELS): { width: number; height: number } {
  if (width * height <= most) return { width, height };
  const scale = Math.sqrt(most / (width * height));
  return { width: Math.max(1, Math.floor(width * scale)), height: Math.max(1, Math.floor(height * scale)) };
}

/** A fresh canvas of a size, or null where there is no 2D context (a test's document). */
function sheet(width: number, height: number): { canvas: HTMLCanvasElement; ctx: CanvasRenderingContext2D | null } {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return { canvas, ctx: canvas.getContext("2d") };
}

/** Draw the original on a canvas: the picture, or a white sheet that stands for a page when there is none. */
export function drawOriginal(canvas: HTMLCanvasElement, image: CanvasImageSource | null): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (image) ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
  else {
    // A blank sheet is white whatever the theme: it stands for a page, and most pages are light.
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
}

/**
 * One step drawn onto the canvas that holds the steps before it. A mark draws in place; a crop or a turn
 * needs a canvas of another size, so it answers a new one and the old one is left to be collected.
 */
export function applyOp(canvas: HTMLCanvasElement, op: Op): HTMLCanvasElement {
  if (op.kind === "mark") {
    const ctx = canvas.getContext("2d");
    if (!ctx) return canvas;
    if (op.mark.tool === "blur") blurArea(ctx, op.mark);
    else drawMark(ctx, op.mark);
    return canvas;
  }
  if (op.kind === "crop") {
    const r = cropRect(op.rect, canvas.width, canvas.height);
    const next = sheet(r.w, r.h);
    next.ctx?.drawImage(canvas, r.x, r.y, r.w, r.h, 0, 0, r.w, r.h);
    return next.canvas;
  }
  const next = sheet(canvas.height, canvas.width);
  if (next.ctx) {
    // A quarter turn clockwise: the old left edge becomes the top.
    next.ctx.translate(canvas.height, 0);
    next.ctx.rotate(Math.PI / 2);
    next.ctx.drawImage(canvas, 0, 0);
  }
  return next.canvas;
}
