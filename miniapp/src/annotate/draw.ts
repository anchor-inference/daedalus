// The geometry and the drawing of an annotated snapshot, kept apart from React so the arithmetic can be
// tested without a canvas. Every coordinate here is in the picture's own pixels, never the screen's: the
// picture is shown scaled to the window, and a mark kept in screen pixels would land in the wrong place
// once the attached file is drawn at the picture's natural size.

export type Point = { x: number; y: number };
export type Tool = "arrow" | "box" | "pen" | "text";

export type Mark =
  | { tool: "arrow" | "box"; color: string; width: number; from: Point; to: Point }
  | { tool: "pen"; color: string; width: number; points: Point[] }
  | { tool: "text"; color: string; size: number; at: Point; text: string };

/** Vivid on a page of any colour, and fixed rather than themed: the file outlives the theme it was drawn in. */
export const SWATCHES = ["#ff3b30", "#ffcc00", "#34c759", "#0a84ff", "#ffffff", "#111111"] as const;

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

/** Two corners as a rectangle with a positive width and height, whichever way the drag went. */
export function rectOf(from: Point, to: Point): { x: number; y: number; w: number; h: number } {
  return { x: Math.min(from.x, to.x), y: Math.min(from.y, to.y), w: Math.abs(to.x - from.x), h: Math.abs(to.y - from.y) };
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
  return mark.tool === "box" ? r.w >= least && r.h >= least : Math.hypot(r.w, r.h) >= least;
}

/** The marks and what Undo steps back through. Clear is a step too, so a slip of the finger is undone. */
export type Board = { marks: Mark[]; past: Mark[][] };
export type BoardAction = { kind: "add"; mark: Mark } | { kind: "undo" } | { kind: "clear" };

export const EMPTY_BOARD: Board = { marks: [], past: [] };

export function boardReducer(board: Board, action: BoardAction): Board {
  switch (action.kind) {
    case "add":
      return meaningful(action.mark) ? { marks: [...board.marks, action.mark], past: [...board.past, board.marks] } : board;
    case "undo":
      return board.past.length ? { marks: board.past[board.past.length - 1], past: board.past.slice(0, -1) } : board;
    case "clear":
      return board.marks.length ? { marks: [], past: [...board.past, board.marks] } : board;
  }
}

/** The part of a 2D context the marks need: a test hands in a recorder, the app a canvas. */
export type Pen = Pick<CanvasRenderingContext2D,
  "beginPath" | "moveTo" | "lineTo" | "closePath" | "stroke" | "fill" | "strokeRect" | "fillText" | "strokeText" | "save" | "restore"
> & { strokeStyle: unknown; fillStyle: unknown; lineWidth: number; lineCap: CanvasLineCap; lineJoin: CanvasLineJoin; font: string; textBaseline: CanvasTextBaseline };

/** A text mark's outline: dark under a light colour and light under a dark one, so it reads on any page. */
export function outlineOf(color: string): string {
  const hex = color.replace("#", "");
  if (hex.length !== 6) return "#000000";
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return 0.299 * r + 0.587 * g + 0.114 * b > 140 ? "#000000" : "#ffffff";
}

export function drawMark(pen: Pen, mark: Mark): void {
  pen.save();
  pen.lineCap = "round";
  pen.lineJoin = "round";
  if (mark.tool === "text") {
    pen.font = `600 ${mark.size}px -apple-system, "Segoe UI", Roboto, sans-serif`;
    pen.textBaseline = "top";
    pen.lineWidth = Math.max(2, mark.size / 6);
    pen.strokeStyle = outlineOf(mark.color);
    pen.fillStyle = mark.color;
    // One line per line typed: the field is a single line, but a pasted text may carry breaks.
    mark.text.split("\n").forEach((line, i) => {
      const y = mark.at.y + i * mark.size * 1.2;
      pen.strokeText(line, mark.at.x, y);
      pen.fillText(line, mark.at.x, y);
    });
    pen.restore();
    return;
  }
  pen.strokeStyle = mark.color;
  pen.fillStyle = mark.color;
  pen.lineWidth = mark.width;
  if (mark.tool === "box") {
    const r = rectOf(mark.from, mark.to);
    pen.strokeRect(r.x, r.y, r.w, r.h);
  } else if (mark.tool === "pen") {
    pen.beginPath();
    mark.points.forEach((p, i) => (i ? pen.lineTo(p.x, p.y) : pen.moveTo(p.x, p.y)));
    pen.stroke();
  } else {
    const [left, right] = arrowHead(mark.from, mark.to, mark.width * 5);
    pen.beginPath();
    pen.moveTo(mark.from.x, mark.from.y);
    pen.lineTo(mark.to.x, mark.to.y);
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
