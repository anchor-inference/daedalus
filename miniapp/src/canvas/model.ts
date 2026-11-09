// A canvas is a JSON Canvas 1.0 document (jsoncanvas.org, the format Obsidian writes): cards, files,
// links and groups placed on an endless plane, and edges between them. This module is the geometry and
// the shape of the data, with no React in it, so the board, the list, the shared view and the tests
// read the document the same way.
//
// Everything the format does not know survives: a node or edge is changed by spreading the old object,
// never by building a new one from the fields this app happens to draw, because a key Obsidian or a
// later version wrote is the operator's data as much as the text is.

export type Side = "top" | "right" | "bottom" | "left";
export type End = "none" | "arrow";
export type NodeType = "text" | "file" | "link" | "group";

export type CanvasNode = {
  id: string;
  type: NodeType;
  x: number;
  y: number;
  width: number;
  height: number;
  color?: string;
  text?: string;
  file?: string;
  subpath?: string;
  url?: string;
  label?: string;
  background?: string;
  backgroundStyle?: "cover" | "ratio" | "repeat";
  daedalus?: { shape?: "ellipse" | "diamond" | "text"; [key: string]: unknown };
  [key: string]: unknown;
};

export type CanvasEdge = {
  id: string;
  fromNode: string;
  toNode: string;
  fromSide?: Side;
  toSide?: Side;
  fromEnd?: End;
  toEnd?: End;
  color?: string;
  label?: string;
  [key: string]: unknown;
};

export type CanvasDoc = { nodes: CanvasNode[]; edges: CanvasEdge[]; [key: string]: unknown };

export type Scope = "chat" | "project";
export type Author = "operator" | "agent";

export type CanvasItem = {
  id: string;
  title: string;
  version: number;
  created_at: string;
  updated_at: string;
  updated_by: Author;
  shared: boolean;
  scope: Scope;
  scope_id: string;
  scope_name: string;
  owner_session_id: string | null;
  nodes: number;
  edges: number;
};

export type CanvasFile = { id: string; name: string; mime: string; size: number };
export type Canvas = CanvasItem & { doc: CanvasDoc; share_token: string; files: CanvasFile[] };
export type CanvasHead = { id: string; title: string; version: number; updated_at: string; updated_by: Author };
export type ScopeRef = { kind: Scope; id: string; name: string };
export type CanvasListing = { scopes: ScopeRef[]; items: CanvasItem[] };
export type SharedCanvasDoc = { title: string; version: number; updated_at: string; doc: CanvasDoc; files: CanvasFile[] };

export type Point = { x: number; y: number };
export type Box = { x: number; y: number; width: number; height: number };

export const SIDES: Side[] = ["top", "right", "bottom", "left"];
export const PRESETS = ["1", "2", "3", "4", "5", "6"] as const;
export const MIN_WIDTH = 60;
export const MIN_HEIGHT = 36;
export const CARD_WIDTH = 260;
export const CARD_HEIGHT = 120;

export function emptyDoc(): CanvasDoc {
  return { nodes: [], edges: [] };
}

/** A document as the host sent it, with the two arrays guaranteed, so nothing downstream checks again. */
export function normalizeDoc(raw: unknown): CanvasDoc {
  const doc = raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as CanvasDoc) : emptyDoc();
  return { ...doc, nodes: Array.isArray(doc.nodes) ? doc.nodes : [], edges: Array.isArray(doc.edges) ? doc.edges : [] };
}

const ID_CHARS = "0123456789abcdef";

/** Sixteen hex characters, the shape of the ids Obsidian itself makes, so a board saved here and opened
 *  there looks like one of its own. */
export function newId(taken?: Set<string>): string {
  for (;;) {
    const bytes = new Uint8Array(8);
    crypto.getRandomValues(bytes);
    let id = "";
    for (const byte of bytes) id += ID_CHARS[byte >> 4] + ID_CHARS[byte & 15];
    if (!taken || !taken.has(id)) return id;
  }
}

/** The CSS colour of a preset or a hex colour; empty for none. Presets are theme tokens rather than
 *  fixed colours, so a red card is the theme's red in light and in dark. */
export function colorValue(color: string | undefined): string {
  if (!color) return "";
  if ((PRESETS as readonly string[]).includes(color)) return `var(--cv-${color})`;
  return /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(color) ? color : "";
}

export function center(box: Box): Point {
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

/** Where an edge meets a node on one of its sides: the middle of that side. */
export function anchor(box: Box, side: Side): Point {
  switch (side) {
    case "top": return { x: box.x + box.width / 2, y: box.y };
    case "bottom": return { x: box.x + box.width / 2, y: box.y + box.height };
    case "left": return { x: box.x, y: box.y + box.height / 2 };
    case "right": return { x: box.x + box.width, y: box.y + box.height / 2 };
  }
}

/** The side of `box` that faces `toward`, for an edge whose side was not written: decided by which way
 *  the line between the two centres leaves the box, so a card above another connects bottom to top
 *  even when the two are far apart sideways. */
export function facingSide(box: Box, toward: Point): Side {
  const c = center(box);
  const dx = toward.x - c.x;
  const dy = toward.y - c.y;
  // Normalised by the box's own proportions: a wide card is left by its top or bottom more often.
  const nx = dx / Math.max(1, box.width);
  const ny = dy / Math.max(1, box.height);
  if (Math.abs(nx) >= Math.abs(ny)) return dx >= 0 ? "right" : "left";
  return dy >= 0 ? "bottom" : "top";
}

/** The side of `box` nearest to a point, for an edge dropped onto a node. */
export function nearestSide(box: Box, point: Point): Side {
  const distances: [Side, number][] = [
    ["top", Math.abs(point.y - box.y)],
    ["bottom", Math.abs(point.y - (box.y + box.height))],
    ["left", Math.abs(point.x - box.x)],
    ["right", Math.abs(point.x - (box.x + box.width))],
  ];
  distances.sort((a, b) => a[1] - b[1]);
  return distances[0][0];
}

const NORMAL: Record<Side, Point> = { top: { x: 0, y: -1 }, bottom: { x: 0, y: 1 }, left: { x: -1, y: 0 }, right: { x: 1, y: 0 } };

export type EdgeGeometry = { path: string; from: Point; to: Point; fromSide: Side; toSide: Side; label: Point; fromAngle: number; toAngle: number };

/** A cubic curve leaving each node straight out of its side, as Obsidian draws them. The handles grow
 *  with the distance, so a long edge bends gently and a short one does not loop. */
export function edgeGeometry(from: Box, to: Box, fromSide?: Side, toSide?: Side): EdgeGeometry {
  const sideA = fromSide ?? facingSide(from, center(to));
  const sideB = toSide ?? facingSide(to, center(from));
  const a = anchor(from, sideA);
  const b = anchor(to, sideB);
  const distance = Math.hypot(b.x - a.x, b.y - a.y);
  const reach = Math.max(40, Math.min(240, distance * 0.45));
  const c1 = { x: a.x + NORMAL[sideA].x * reach, y: a.y + NORMAL[sideA].y * reach };
  const c2 = { x: b.x + NORMAL[sideB].x * reach, y: b.y + NORMAL[sideB].y * reach };
  const path = `M ${round(a.x)} ${round(a.y)} C ${round(c1.x)} ${round(c1.y)}, ${round(c2.x)} ${round(c2.y)}, ${round(b.x)} ${round(b.y)}`;
  // The middle of the curve (t = 0.5 of a cubic Bézier) is where its label sits.
  const label = { x: (a.x + 3 * c1.x + 3 * c2.x + b.x) / 8, y: (a.y + 3 * c1.y + 3 * c2.y + b.y) / 8 };
  // The arrowhead points along the curve's tangent at each end, which is the line to its handle.
  const toAngle = Math.atan2(b.y - c2.y, b.x - c2.x);
  const fromAngle = Math.atan2(a.y - c1.y, a.x - c1.x);
  return { path, from: a, to: b, fromSide: sideA, toSide: sideB, label, fromAngle, toAngle };
}

/** The curve from a node's side to a loose point, while an edge is being dragged out. */
export function draftPath(from: Box, side: Side, to: Point): string {
  const a = anchor(from, side);
  const reach = Math.max(30, Math.min(200, Math.hypot(to.x - a.x, to.y - a.y) * 0.45));
  const c1 = { x: a.x + NORMAL[side].x * reach, y: a.y + NORMAL[side].y * reach };
  return `M ${round(a.x)} ${round(a.y)} C ${round(c1.x)} ${round(c1.y)}, ${round(to.x)} ${round(to.y)}, ${round(to.x)} ${round(to.y)}`;
}

/** An arrowhead's three points at `tip`, pointing along `angle`, sized for a stroke drawn at `scale`. */
export function arrowHead(tip: Point, angle: number, size = 11): string {
  const spread = Math.PI / 7;
  const left = { x: tip.x - size * Math.cos(angle - spread), y: tip.y - size * Math.sin(angle - spread) };
  const right = { x: tip.x - size * Math.cos(angle + spread), y: tip.y - size * Math.sin(angle + spread) };
  return `${round(tip.x)},${round(tip.y)} ${round(left.x)},${round(left.y)} ${round(right.x)},${round(right.y)}`;
}

function round(n: number): number {
  return Math.round(n * 10) / 10;
}

export function contains(outer: Box, inner: Box): boolean {
  return inner.x >= outer.x && inner.y >= outer.y && inner.x + inner.width <= outer.x + outer.width && inner.y + inner.height <= outer.y + outer.height;
}

export function intersects(a: Box, b: Box): boolean {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

/** The nodes a group carries when it moves: every node whose box lies wholly inside the group's, and
 *  so, through nested groups, theirs too. Containment is geometry, as in Obsidian: the format has no
 *  membership list, and inventing one would be lost the first time the file was opened there. */
export function groupContents(doc: CanvasDoc, groupId: string): string[] {
  const group = doc.nodes.find((node) => node.id === groupId);
  if (!group || group.type !== "group") return [];
  return doc.nodes.filter((node) => node.id !== groupId && contains(group, node)).map((node) => node.id);
}

/** What moves when `ids` are dragged: those nodes and everything the groups among them carry. */
export function movingSet(doc: CanvasDoc, ids: Iterable<string>): Set<string> {
  const result = new Set<string>();
  for (const id of ids) {
    result.add(id);
    for (const inner of groupContents(doc, id)) result.add(inner);
  }
  return result;
}

export function bounds(boxes: Box[]): Box | null {
  if (!boxes.length) return null;
  let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
  for (const box of boxes) {
    x1 = Math.min(x1, box.x);
    y1 = Math.min(y1, box.y);
    x2 = Math.max(x2, box.x + box.width);
    y2 = Math.max(y2, box.y + box.height);
  }
  return { x: x1, y: y1, width: x2 - x1, height: y2 - y1 };
}

/** A box from two corners in any order, for the marquee. */
export function boxFrom(a: Point, b: Point): Box {
  return { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y), width: Math.abs(a.x - b.x), height: Math.abs(a.y - b.y) };
}

/** Groups first, then the rest in document order: a group is a backdrop and drawing it over a card
 *  would hide what it holds, whatever order a hand-edited file put them in. */
export function drawOrder(doc: CanvasDoc): CanvasNode[] {
  return [...doc.nodes.filter((node) => node.type === "group"), ...doc.nodes.filter((node) => node.type !== "group")];
}

/** Only http and https become links. A `javascript:` or `data:` address in a file someone imported
 *  would otherwise run on the app's own origin when clicked. */
export function safeUrl(url: string | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
}

const ATTACHMENT = /^files\/([A-Za-z0-9_-]+)\/(.+)$/;

/** A file node naming one of the canvas's own attachments: its id and name; null for a workspace path. */
export function attachment(path: string | undefined): { id: string; name: string } | null {
  const match = ATTACHMENT.exec(path ?? "");
  return match ? { id: match[1], name: match[2] } : null;
}

export function isImage(name: string): boolean {
  return /\.(png|jpe?g|gif|webp|svg|avif)$/i.test(name);
}

/** Words a person reads for a node, for the conflict banner, the accessible name and the history. */
export function nodeLabel(node: CanvasNode | undefined, fallback = ""): string {
  if (!node) return fallback;
  const raw = node.type === "text" ? node.text : node.type === "group" ? node.label : node.type === "link" ? node.url : node.file;
  const words = (raw ?? "").replace(/[#*_`>[\]()-]+/g, " ").replace(/\s+/g, " ").trim();
  return words.length > 40 ? `${words.slice(0, 39)}…` : words || fallback;
}

/** A text card sized roughly to its words, so a new card from the agent's or the operator's hand does
 *  not open as a sliver or a billboard. A guess, not a measurement: the operator resizes what is off. */
export function fitText(text: string): { width: number; height: number } {
  const lines = text.split("\n");
  const longest = Math.max(0, ...lines.map((line) => line.length));
  const width = Math.max(160, Math.min(400, longest * 8 + 40));
  const perLine = Math.max(1, Math.floor((width - 40) / 8));
  const rows = lines.reduce((sum, line) => sum + Math.max(1, Math.ceil(line.length / perLine)), 0);
  return { width: Math.round(width / 10) * 10, height: Math.max(60, Math.round((rows * 24 + 32) / 10) * 10) };
}
