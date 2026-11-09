// The geometry and the drawing of a marked-up picture, kept apart from React so the arithmetic can be
// tested without a canvas. Every coordinate here is in the picture's own pixels, never the screen's: the
// picture is shown scaled to the window, and a mark kept in screen pixels would land in the wrong place
// once the file is drawn at the picture's natural size.

export type Point = { x: number; y: number };
/** What a drag on the picture does. Crop is a tool too: it is drawn like a box and then applied. */
export type Tool = "arrow" | "box" | "ellipse" | "pen" | "text" | "blur" | "crop";
export type DrawingTool = Exclude<Tool, "crop">;

export type Mark =
  | { tool: "arrow" | "box" | "ellipse"; color: string; width: number; from: Point; to: Point }
  | { tool: "blur"; from: Point; to: Point; block: number }
  | { tool: "pen"; color: string; width: number; points: Point[] }
  | { tool: "text"; color: string; size: number; at: Point; text: string };

/** Vivid on a page of any colour, and fixed rather than themed: the file outlives the theme it was drawn in. */
export const SWATCHES = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#0a84ff", "#ffffff", "#111111"] as const;

/** The three line weights, as factors of the picture's own line: thin for a detail, thick for a phone photo. */
export const WEIGHTS = [0.6, 1, 1.8] as const;

/** The largest a box may be drawn into while keeping the picture's proportions. */
export function fitBox(width: number, height: number, boxWidth: number, boxHeight: number): { width: number; height: number } {
  if (width <= 0 || height <= 0 || boxWidth <= 0 || boxHeight <= 0) return { width: 0, height: 0 };
  const scale = Math.min(boxWidth / width, boxHeight / height);
  return { width: Math.floor(width * scale), height: Math.floor(height * scale) };
}

/** A pointer on the shown picture, in the picture's pixels; a drag that leaves the picture is held at its edge. */
export function toPicture(clientX: number, clientY: number, rect: { left: number; top: number; width: number; height: number }, width: number, height: number): Point {
  if (rect.width <= 0 || rect.height <= 0) return { x: 0, y: 0 };
  const x = ((clientX - rect.left) / rect.width) * width;
  const y = ((clientY - rect.top) / rect.height) * height;
  return { x: clamp(x, 0, width), y: clamp(y, 0, height) };
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/** A line as thick on a phone's small frame as on a desktop's large one, relative to the picture. */
export function strokeWidth(width: number, height: number): number {
  return Math.max(2, Math.round(Math.max(width, height) / 400));
}

/** Text that reads at a glance when the picture is shown at about half its size. */
export function textSize(width: number, height: number): number {
  return Math.max(14, Math.round(Math.max(width, height) / 48));
}

/**
 * The side of one square of the blur, in the picture's pixels. Coarse on purpose: a fine mosaic over a
 * line of text can be undone by trying fonts until the squares match, while a square taller than the
 * line and several letters wide averages whole words into one colour and leaves nothing to match.
 */
export function blurBlock(width: number, height: number): number {
  return Math.max(12, Math.round(Math.max(width, height) / 48));
}

/** Two corners as a rectangle with a positive width and height, whichever way the drag went. */
export function rectOf(from: Point, to: Point): { x: number; y: number; w: number; h: number } {
  return { x: Math.min(from.x, to.x), y: Math.min(from.y, to.y), w: Math.abs(to.x - from.x), h: Math.abs(to.y - from.y) };
}

/** A drag's far corner held to a square, for the box, the ellipse and the crop while Shift is down. */
export function squareOf(from: Point, to: Point): Point {
  const side = Math.max(Math.abs(to.x - from.x), Math.abs(to.y - from.y));
  return { x: from.x + Math.sign(to.x - from.x || 1) * side, y: from.y + Math.sign(to.y - from.y || 1) * side };
}

/**
 * An arrow's tip, turned to the nearest straight direction. A hand-drawn arrow meant to be level is
 * always a degree or two off, and a slightly tilted arrow on a screenshot of straight lines reads as
 * careless; so within a few degrees of level, upright or diagonal it snaps, and with Shift held it
 * keeps to steps of fifteen degrees whatever the angle. The length is kept, so the tip stays under the
 * pointer's distance and the drag still feels direct.
 */
export function snapArrow(from: Point, to: Point, free: boolean, steps = false): Point {
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const length = Math.hypot(dx, dy);
  if (free || length === 0) return to;
  const angle = Math.atan2(dy, dx);
  const step = steps ? Math.PI / 12 : Math.PI / 4;
  const nearest = Math.round(angle / step) * step;
  if (!steps && Math.abs(nearest - angle) > (4 * Math.PI) / 180) return to;
  return { x: from.x + Math.cos(nearest) * length, y: from.y + Math.sin(nearest) * length };
}

/** The two far corners of an arrow's head, swept back from the tip at thirty degrees either side. */
export function arrowHead(from: Point, to: Point, size: number): [Point, Point] {
  const angle = Math.atan2(to.y - from.y, to.x - from.x);
  const spread = Math.PI / 6;
  return [
    { x: to.x - size * Math.cos(angle - spread), y: to.y - size * Math.sin(angle - spread) },
    { x: to.x - size * Math.cos(angle + spread), y: to.y - size * Math.sin(angle + spread) },
  ];
}

/**
 * Whether a finished mark is worth keeping. A tap with the arrow or the box tool draws a dot nobody
 * meant, and it would sit in the undo history as a step that changes nothing visible.
 */
export function meaningful(mark: Mark, least = 4): boolean {
  if (mark.tool === "text") return mark.text.trim().length > 0;
  if (mark.tool === "pen") return mark.points.length > 1;
  const r = rectOf(mark.from, mark.to);
  return mark.tool === "arrow" ? Math.hypot(r.w, r.h) >= least : r.w >= least && r.h >= least;
}

/** The part of a 2D context the marks need: a test hands in a recorder, the app a canvas. */
export type Pen = Pick<CanvasRenderingContext2D,
  "beginPath" | "moveTo" | "lineTo" | "quadraticCurveTo" | "closePath" | "stroke" | "fill" | "strokeRect" | "fillRect" | "ellipse"
  | "fillText" | "measureText" | "save" | "restore"
> & { strokeStyle: unknown; fillStyle: unknown; lineWidth: number; lineCap: CanvasLineCap; lineJoin: CanvasLineJoin; font: string; textBaseline: CanvasTextBaseline };

/** The colour that reads on top of `color`: black on a light one, white on a dark one. */
export function contrastOf(color: string): string {
  const hex = color.replace("#", "");
  if (hex.length !== 6) return "#000000";
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return 0.299 * r + 0.587 * g + 0.114 * b > 140 ? "#000000" : "#ffffff";
}

/** The font a text mark is drawn in; the field it is typed into uses the same one. */
export const TEXT_FONT = `-apple-system, "Segoe UI", Roboto, sans-serif`;

/**
 * A text mark's label: the words on a filled plate of the chosen colour, in the colour that reads on it.
 * Outlined letters straight on a screenshot were the old way, and they turned to noise over busy
 * pages; a plate reads over anything and says plainly that the words are the operator's, not the page's.
 */
export function labelBox(size: number, widths: number[]): { pad: number; line: number; w: number; h: number; radius: number } {
  const pad = Math.round(size * 0.3);
  const line = Math.round(size * 1.2);
  const w = Math.max(0, ...widths) + pad * 2;
  const h = line * widths.length + pad * 2 - (line - size);
  return { pad, line, w, h, radius: Math.round(size * 0.25) };
}

export function drawMark(pen: Pen, mark: Mark): void {
  if (mark.tool === "blur") return; // a blur reads the pixels under it, which a pen cannot; see blurArea
  pen.save();
  pen.lineCap = "round";
  pen.lineJoin = "round";
  if (mark.tool === "text") {
    pen.font = `600 ${mark.size}px ${TEXT_FONT}`;
    pen.textBaseline = "top";
    const lines = mark.text.split("\n");
    const box = labelBox(mark.size, lines.map((line) => pen.measureText(line).width));
    pen.fillStyle = mark.color;
    roundRect(pen, mark.at.x, mark.at.y, box.w, box.h, box.radius);
    pen.fill();
    pen.fillStyle = contrastOf(mark.color);
    lines.forEach((line, i) => pen.fillText(line, mark.at.x + box.pad, mark.at.y + box.pad + i * box.line));
    pen.restore();
    return;
  }
  pen.strokeStyle = mark.color;
  pen.fillStyle = mark.color;
  pen.lineWidth = mark.width;
  if (mark.tool === "box") {
    const r = rectOf(mark.from, mark.to);
    pen.strokeRect(r.x, r.y, r.w, r.h);
  } else if (mark.tool === "ellipse") {
    const r = rectOf(mark.from, mark.to);
    pen.beginPath();
    pen.ellipse(r.x + r.w / 2, r.y + r.h / 2, r.w / 2, r.h / 2, 0, 0, Math.PI * 2);
    pen.stroke();
  } else if (mark.tool === "pen") {
    smoothPath(pen, mark.points);
    pen.stroke();
  } else {
    // The head grows with the line but never past a third of the shaft, so a short arrow is not all head.
    const length = Math.hypot(mark.to.x - mark.from.x, mark.to.y - mark.from.y);
    const size = Math.min(mark.width * 5, length / 3 + mark.width * 2);
    const [left, right] = arrowHead(mark.from, mark.to, size);
    // The shaft stops inside the head: a round cap at the very tip would blunt the point.
    const back = Math.min(1, (size * 0.6) / Math.max(1, length));
    pen.beginPath();
    pen.moveTo(mark.from.x, mark.from.y);
    pen.lineTo(mark.to.x - (mark.to.x - mark.from.x) * back, mark.to.y - (mark.to.y - mark.from.y) * back);
    pen.stroke();
    pen.beginPath();
    pen.moveTo(mark.to.x, mark.to.y);
    pen.lineTo(left.x, left.y);
    pen.lineTo(right.x, right.y);
    pen.closePath();
    pen.fill();
  }
  pen.restore();
}

function roundRect(pen: Pen, x: number, y: number, w: number, h: number, r: number): void {
  pen.beginPath();
  pen.moveTo(x + r, y);
  pen.lineTo(x + w - r, y);
  pen.quadraticCurveTo(x + w, y, x + w, y + r);
  pen.lineTo(x + w, y + h - r);
  pen.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  pen.lineTo(x + r, y + h);
  pen.quadraticCurveTo(x, y + h, x, y + h - r);
  pen.lineTo(x, y + r);
  pen.quadraticCurveTo(x, y, x + r, y);
  pen.closePath();
}

/**
 * A pen stroke through the midpoints of its samples, each sample a curve's control point. Straight
 * segments between pointer samples show their corners on a fast stroke, most of all from a finger,
 * whose events arrive sparser than a mouse's.
 */
export function smoothPath(pen: Pick<Pen, "beginPath" | "moveTo" | "lineTo" | "quadraticCurveTo">, points: Point[]): void {
  pen.beginPath();
  if (!points.length) return;
  pen.moveTo(points[0].x, points[0].y);
  if (points.length < 3) {
    for (const p of points.slice(1)) pen.lineTo(p.x, p.y);
    return;
  }
  for (let i = 1; i < points.length - 1; i++) {
    const mid = { x: (points[i].x + points[i + 1].x) / 2, y: (points[i].y + points[i + 1].y) / 2 };
    pen.quadraticCurveTo(points[i].x, points[i].y, mid.x, mid.y);
  }
  const last = points[points.length - 1];
  pen.lineTo(last.x, last.y);
}

/**
 * The mosaic of an area: each square of `block` pixels replaced by the mean of every pixel in it. Not
 * a sample of a few pixels per square, which is what a scaled-down drawImage takes and which keeps
 * single pixels of the original recoverable; the mean of all of them keeps nothing but the square's tone.
 * `data` is RGBA, `width` by `height`; it is changed in place.
 */
export function mosaic(data: Uint8ClampedArray, width: number, height: number, block: number): void {
  for (let top = 0; top < height; top += block) {
    for (let left = 0; left < width; left += block) {
      const bottom = Math.min(height, top + block);
      const right = Math.min(width, left + block);
      let r = 0, g = 0, b = 0, a = 0;
      for (let y = top; y < bottom; y++) {
        for (let x = left, i = (y * width + left) * 4; x < right; x++, i += 4) {
          r += data[i]; g += data[i + 1]; b += data[i + 2]; a += data[i + 3];
        }
      }
      const n = (bottom - top) * (right - left);
      r = Math.round(r / n); g = Math.round(g / n); b = Math.round(b / n); a = Math.round(a / n);
      for (let y = top; y < bottom; y++) {
        for (let x = left, i = (y * width + left) * 4; x < right; x++, i += 4) {
          data[i] = r; data[i + 1] = g; data[i + 2] = b; data[i + 3] = a;
        }
      }
    }
  }
}

/**
 * Blur an area of what is drawn so far. The squares are laid on the picture's grid, not the drag's, so
 * two blurs side by side meet without a seam.
 */
export function blurArea(ctx: CanvasRenderingContext2D, mark: Extract<Mark, { tool: "blur" }>): void {
  const r = rectOf(mark.from, mark.to);
  const block = mark.block;
  const x = Math.max(0, Math.floor(r.x / block) * block);
  const y = Math.max(0, Math.floor(r.y / block) * block);
  const w = Math.min(ctx.canvas.width, Math.ceil((r.x + r.w) / block) * block) - x;
  const h = Math.min(ctx.canvas.height, Math.ceil((r.y + r.h) / block) * block) - y;
  if (w <= 0 || h <= 0) return;
  const image = ctx.getImageData(x, y, w, h);
  mosaic(image.data, w, h, block);
  ctx.putImageData(image, x, y);
}

/** A file name that says where the picture came from and when, so two snapshots in one message differ. */
export function snapshotName(where: string, at: Date): string {
  let host = "";
  try {
    host = new URL(where).hostname;
  } catch {
    host = where.split("/").pop() ?? "";
  }
  const slug = host.replace(/^www\./, "").replace(/[^a-z0-9.-]+/gi, "-").replace(/^-+|-+$/g, "").slice(0, 40);
  const pad = (n: number) => String(n).padStart(2, "0");
  const stamp = `${at.getFullYear()}${pad(at.getMonth() + 1)}${pad(at.getDate())}-${pad(at.getHours())}${pad(at.getMinutes())}${pad(at.getSeconds())}`;
  return `snapshot-${slug ? `${slug}-` : ""}${stamp}.png`;
}
