// The board: a canvas drawn as DOM cards and one SVG layer of edges, both inside a single transformed
// layer, so panning and zooming move one element and the browser composites the rest. No canvas or
// graph library: the format is small, and owning the drawing is what lets a card be real Markdown,
// selectable text and a focusable element rather than pixels.
//
// The board never changes the document itself. Every edit is a batch of operations handed to the
// owner (`onOps`), which applies it, keeps it for Undo and saves it; while a drag or a resize is under
// way the board shows the moving boxes from its own overlay and reports one batch when the pointer
// lifts, so a drag is one save and one Undo, not three hundred.

import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import { api } from "../api";
import { Icon, type IconName } from "../icons";
import { t } from "../i18n";
import { renderMarkdown } from "../md";
import { RawHtml } from "../rawhtml";
import {
  CARD_HEIGHT, CARD_WIDTH, MIN_HEIGHT, MIN_WIDTH, PRESETS, SIDES, arrowHead, attachment, bounds, boxFrom, colorValue, drawOrder, draftPath, edgeGeometry, facingSide,
  fitText, intersects, isImage, movingSet, nearestSide, newId, nodeLabel, safeUrl,
  type Box, type CanvasDoc, type CanvasEdge, type CanvasNode, type Point, type Side,
} from "./model";
import type { Op } from "./ops";
import { frameOf, type Mode } from "./frames";

type View = { x: number; y: number; zoom: number };
type Handle = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
type Editing = { id: string; kind: "text" | "label" | "url" | "edge" };
type Draft = { node: CanvasNode; edge?: CanvasEdge };

type Gesture =
  | { kind: "pan"; pointerId: number; start: Point; view: View }
  | { kind: "pinch"; start: { distance: number; center: Point; view: View } }
  | { kind: "move"; pointerId: number; start: Point; origin: Map<string, Box>; moved: boolean; target: string; additive: boolean; wasSelected: boolean }
  | { kind: "resize"; pointerId: number; id: string; handle: Handle; start: Point; origin: Box }
  | { kind: "connect"; pointerId: number; from: string; side: Side; point: Point }
  | { kind: "marquee"; pointerId: number; start: Point; current: Point; base: Set<string> };

export type BoardProps = {
  doc: CanvasDoc;
  label: string;
  readOnly?: boolean;
  /** Where an attachment of the canvas (a `files/<id>/<name>` file node) is fetched from. */
  fileUrl: (fileId: string, name: string) => string;
  onOps?: (ops: Op[]) => void;
  onUndo?: () => void;
  onRedo?: () => void;
  canUndo?: boolean;
  canRedo?: boolean;
  /** Items the last save could not merge, outlined until the operator decides. */
  marked?: ReadonlySet<string>;
  compact?: boolean;
  /** Told whenever the operator starts or ends something under way (typing in a card, a drag), so the
   *  owner holds back a newer version from elsewhere until it ends instead of swapping the card out
   *  from under a half-typed edit. */
  onBusy?: (busy: boolean) => void;
  /** How a live page node is drawn (a frame, a still copy, a placeholder); without it, a file card. */
  renderFrame?: (node: CanvasNode, near: boolean) => ReactNode;
  /** Interact: clicks reach pages and cards are edited. Comment: a click points at what it hits. */
  mode?: Mode;
  onModeChange?: (mode: Mode) => void;
  /** A card pointed at in comment mode. */
  onPoint?: (node: CanvasNode) => void;
  /** Drawn in board coordinates over everything, such as the note beside a picked element. */
  overlay?: ReactNode;
};

const ZOOM_MIN = 0.1;
const ZOOM_MAX = 3;
const DRAG_THRESHOLD = 4;
const NUDGE = 10;
const GRID = 24;
const GROUP_PAD = 40;
const READABLE = 0.55;

function clampZoom(zoom: number): number {
  return Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, zoom));
}

function typing(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  return !!element && (element.tagName === "INPUT" || element.tagName === "TEXTAREA" || element.isContentEditable);
}

export function Board({ doc, label, readOnly = false, fileUrl, onOps, onUndo, onRedo, canUndo, canRedo, marked, compact, onBusy, renderFrame, mode = "interact", onModeChange, onPoint, overlay }: BoardProps) {
  const root = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<View>({ x: 0, y: 0, zoom: 1 });
  const viewRef = useRef(view);
  viewRef.current = view;
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [live, setLive] = useState<Map<string, Box> | null>(null);
  const [connecting, setConnecting] = useState<{ from: string; side: Side; point: Point } | null>(null);
  const [marquee, setMarquee] = useState<Box | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [palette, setPalette] = useState(false);
  const gesture = useRef<Gesture | null>(null);
  const pointers = useRef(new Map<number, Point>());
  const spaceHeld = useRef(false);
  const fitted = useRef(false);
  const docRef = useRef(doc);
  docRef.current = doc;

  // What is drawn: the document, the draft card being typed, and the boxes a gesture is moving.
  const shown = useMemo(() => {
    const nodes = draft ? [...doc.nodes, draft.node] : doc.nodes;
    const edges = draft?.edge ? [...doc.edges, draft.edge] : doc.edges;
    return { ...doc, nodes, edges };
  }, [doc, draft]);
  const boxes = useMemo(() => {
    const map = new Map<string, Box>();
    for (const node of shown.nodes) map.set(node.id, live?.get(node.id) ?? node);
    return map;
  }, [shown, live]);

  // Selection follows the document: an item someone else deleted stops being selected.
  useEffect(() => {
    const present = new Set([...doc.nodes.map((node) => node.id), ...doc.edges.map((edge) => edge.id)]);
    setSelected((current) => {
      const kept = [...current].filter((id) => present.has(id) || id === draft?.node.id);
      return kept.length === current.size ? current : new Set(kept);
    });
  }, [doc, draft]);

  const busy = !!(editing || draft || live || connecting);
  useEffect(() => { onBusy?.(busy); }, [busy, onBusy]);

  const emit = useCallback((ops: Op[]) => {
    if (ops.length && onOps && !readOnly) onOps(ops);
  }, [onOps, readOnly]);

  const toWorld = useCallback((clientX: number, clientY: number): Point => {
    const rect = root.current?.getBoundingClientRect();
    const current = viewRef.current;
    return { x: (clientX - (rect?.left ?? 0) - current.x) / current.zoom, y: (clientY - (rect?.top ?? 0) - current.y) / current.zoom };
  }, []);

  const fit = useCallback((whole = true) => {
    const element = root.current;
    // A group's label and a page's header sit above their boxes; fitted without them, the board's
    // own toolbar covered the header of a page at the top.
    const box = bounds(docRef.current.nodes.map((node) => (node.type === "group" || frameOf(node) ? { ...node, y: node.y - 40, height: node.height + 40 } : node)));
    if (!element) return;
    const width = element.clientWidth;
    const height = element.clientHeight;
    if (!box || !width || !height) {
      setView({ x: width / 2, y: height / 2, zoom: 1 });
      return;
    }
    const pad = compact ? 24 : 56;
    const zoom = clampZoom(Math.min(1, (width - pad * 2) / Math.max(1, box.width), (height - pad * 2) / Math.max(1, box.height)));
    if (!whole && zoom < READABLE) {
      // Opened on a narrow screen, a wide board fitted whole is a strip of unreadable cards. It opens
      // at a size the words can be read, at its start, and "Show everything" still fits it whole.
      setView({ zoom: READABLE, x: pad - box.x * READABLE, y: height / 2 - (box.y + box.height / 2) * READABLE });
      return;
    }
    setView({ zoom, x: width / 2 - (box.x + box.width / 2) * zoom, y: height / 2 - (box.y + box.height / 2) * zoom });
  }, [compact]);

  // Opened fitted, once: later changes from the agent must not yank the view from under the operator.
  useLayoutEffect(() => {
    if (fitted.current || !root.current?.clientWidth) return;
    fitted.current = true;
    fit(!compact);
  });

  const zoomAt = useCallback((factor: number, clientX?: number, clientY?: number) => {
    const element = root.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    const px = (clientX ?? rect.left + rect.width / 2) - rect.left;
    const py = (clientY ?? rect.top + rect.height / 2) - rect.top;
    setView((current) => {
      const zoom = clampZoom(current.zoom * factor);
      // The point under the pointer stays under the pointer.
      return { zoom, x: px - ((px - current.x) / current.zoom) * zoom, y: py - ((py - current.y) / current.zoom) * zoom };
    });
  }, []);

  // A wheel listener of our own, not React's: React's is passive, and a zoom that also scrolls the page
  // behind the board is a page that jumps.
  useEffect(() => {
    const element = root.current;
    if (!element) return;
    const onWheel = (event: WheelEvent) => {
      const target = event.target as HTMLElement;
      const body = target.closest?.(".cv-card-body") as HTMLElement | null;
      // A long card scrolls its own words first, as a page inside the page would.
      if (body && !event.ctrlKey && body.scrollHeight > body.clientHeight + 1) {
        const atTop = body.scrollTop <= 0 && event.deltaY < 0;
        const atBottom = body.scrollTop + body.clientHeight >= body.scrollHeight - 1 && event.deltaY > 0;
        if (!atTop && !atBottom) return;
      }
      if (target.closest?.("[data-ui]") || typing(target)) return;
      event.preventDefault();
      // A mouse wheel arrives in whole notches with no sideways part and zooms; a trackpad's two
      // fingers arrive as small mixed deltas and pan, and its pinch arrives with ctrlKey and zooms.
      const notch = event.deltaMode === 1 || (Math.abs(event.deltaX) < 0.5 && Math.abs(event.deltaY) >= 50 && Number.isInteger(event.deltaY));
      if (event.ctrlKey || event.metaKey || notch) {
        const scale = event.deltaMode === 1 ? 0.05 : 0.0022;
        zoomAt(Math.exp(-event.deltaY * scale), event.clientX, event.clientY);
      } else {
        setView((current) => ({ ...current, x: current.x - event.deltaX, y: current.y - event.deltaY }));
      }
    };
    element.addEventListener("wheel", onWheel, { passive: false });
    return () => element.removeEventListener("wheel", onWheel);
  }, [zoomAt]);

  const nodeAt = (clientX: number, clientY: number, except?: string): CanvasNode | null => {
    const hits = document.elementsFromPoint(clientX, clientY);
    for (const hit of hits) {
      const id = (hit as HTMLElement).closest?.("[data-node]")?.getAttribute("data-node");
      if (id && id !== except) return shown.nodes.find((node) => node.id === id) ?? null;
    }
    return null;
  };

  const select = (ids: Iterable<string>) => setSelected(new Set(ids));

  function startEditing(id: string) {
    const node = shown.nodes.find((item) => item.id === id);
    const edge = shown.edges.find((item) => item.id === id);
    if (readOnly) return;
    if (node) setEditing({ id, kind: node.type === "group" ? "label" : node.type === "link" ? "url" : node.type === "text" ? "text" : "label" });
    else if (edge) setEditing({ id, kind: "edge" });
    if (node && node.type === "file") setEditing(null);
  }

  function newCard(at: Point, text = ""): CanvasNode {
    const taken = new Set(shown.nodes.map((node) => node.id));
    return { id: newId(taken), type: "text", text, x: Math.round(at.x - CARD_WIDTH / 2), y: Math.round(at.y - CARD_HEIGHT / 2), width: CARD_WIDTH, height: CARD_HEIGHT };
  }

  function viewportCenter(): Point {
    const element = root.current;
    const rect = element?.getBoundingClientRect();
    return toWorld((rect?.left ?? 0) + (rect?.width ?? 0) / 2, (rect?.top ?? 0) + (rect?.height ?? 0) / 2);
  }

  function addCard(at?: Point) {
    if (readOnly) return;
    const node = newCard(at ?? viewportCenter());
    setDraft({ node });
    select([node.id]);
    setEditing({ id: node.id, kind: "text" });
  }

  function addLink() {
    if (readOnly) return;
    const center = viewportCenter();
    const taken = new Set(shown.nodes.map((node) => node.id));
    const node: CanvasNode = { id: newId(taken), type: "link", url: "", x: Math.round(center.x - 180), y: Math.round(center.y - 40), width: 360, height: 80 };
    setDraft({ node });
    select([node.id]);
    setEditing({ id: node.id, kind: "url" });
  }

  /** Ends an edit. A draft that ends empty was never made; one with words is put with its edge. */
  function commitEdit(value: string | null) {
    const current = editing;
    setEditing(null);
    if (!current) return;
    if (draft && draft.node.id === current.id) {
      const text = (value ?? "").trim();
      setDraft(null);
      if (!text) {
        select([]);
        return;
      }
      const node: CanvasNode = draft.node.type === "link" ? { ...draft.node, url: normaliseUrl(text) } : { ...draft.node, text: value ?? "", ...fitGrow(draft.node, value ?? "") };
      emit([{ op: "put", kind: "node", item: node }, ...(draft.edge ? [{ op: "put" as const, kind: "edge" as const, item: draft.edge }] : [])]);
      return;
    }
    if (value === null) return;
    const node = doc.nodes.find((item) => item.id === current.id);
    if (node) {
      if (current.kind === "text" && value !== (node.text ?? "")) emit([{ op: "put", kind: "node", item: { ...node, text: value } }]);
      if (current.kind === "label" && value !== (node.label ?? "")) emit([{ op: "put", kind: "node", item: withOptional(node, "label", value.trim()) }]);
      if (current.kind === "url" && normaliseUrl(value) !== (node.url ?? "")) emit([{ op: "put", kind: "node", item: { ...node, url: normaliseUrl(value) } }]);
      return;
    }
    const edge = doc.edges.find((item) => item.id === current.id);
    if (edge && value.trim() !== (edge.label ?? "")) emit([{ op: "put", kind: "edge", item: withOptional(edge, "label", value.trim()) }]);
  }

  function removeSelection() {
    if (readOnly || !selected.size) return;
    const ops: Op[] = [];
    // Edges first: a node's deletion takes its edges along, and deleting one of those again would be
    // an operation on nothing.
    const nodes = new Set(doc.nodes.filter((node) => selected.has(node.id)).map((node) => node.id));
    for (const edge of doc.edges) if (selected.has(edge.id) && !nodes.has(edge.fromNode) && !nodes.has(edge.toNode)) ops.push({ op: "delete", id: edge.id });
    for (const id of nodes) ops.push({ op: "delete", id });
    emit(ops);
    select([]);
  }

  function nudge(dx: number, dy: number) {
    const ids = movingSet(doc, [...selected].filter((id) => doc.nodes.some((node) => node.id === id)));
    const ops: Op[] = doc.nodes.filter((node) => ids.has(node.id)).map((node) => ({ op: "put", kind: "node", item: { ...node, x: node.x + dx, y: node.y + dy } }));
    emit(ops);
  }

  function groupSelection() {
    const nodes = doc.nodes.filter((node) => selected.has(node.id));
    const box = bounds(nodes);
    if (readOnly || !box) return;
    const node: CanvasNode = { id: newId(new Set(doc.nodes.map((item) => item.id))), type: "group", label: "", x: box.x - GROUP_PAD, y: box.y - GROUP_PAD, width: box.width + GROUP_PAD * 2, height: box.height + GROUP_PAD * 2 };
    emit([{ op: "put", kind: "node", item: node, at: "back" }]);
    select([node.id]);
    setEditing({ id: node.id, kind: "label" });
  }

  function paint(color: string) {
    const ops: Op[] = [];
    for (const node of doc.nodes) if (selected.has(node.id)) ops.push({ op: "put", kind: "node", item: withOptional(node, "color", color) });
    for (const edge of doc.edges) if (selected.has(edge.id)) ops.push({ op: "put", kind: "edge", item: withOptional(edge, "color", color) });
    emit(ops);
    setPalette(false);
  }

  function toggleEnd(end: "fromEnd" | "toEnd") {
    const ops: Op[] = [];
    for (const edge of doc.edges) {
      if (!selected.has(edge.id)) continue;
      const current = edge[end] ?? (end === "toEnd" ? "arrow" : "none");
      const next = current === "arrow" ? "none" : "arrow";
      const fallback = end === "toEnd" ? "arrow" : "none";
      // The format's default is written as an absence, so a file round-trips without new keys.
      const item: CanvasEdge = { ...edge };
      if (next === fallback) delete item[end];
      else item[end] = next;
      ops.push({ op: "put", kind: "edge", item });
    }
    emit(ops);
  }

  // -- pointers ----------------------------------------------------------------------------------

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement;
    if (target.closest("[data-ui]") || typing(target)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    root.current?.focus({ preventScroll: true });
    setPalette(false);
    if (pointers.current.size === 2) {
      // A second finger turns whatever the first one started into a pinch, and nothing is moved.
      setLive(null);
      setConnecting(null);
      setMarquee(null);
      const [a, b] = [...pointers.current.values()];
      gesture.current = { kind: "pinch", start: { distance: Math.hypot(a.x - b.x, a.y - b.y), center: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, view: viewRef.current } };
      return;
    }
    if (pointers.current.size > 2) return;
    const point = toWorld(event.clientX, event.clientY);
    const panning = event.button === 1 || spaceHeld.current;
    const handle = target.closest("[data-handle]")?.getAttribute("data-handle") as Handle | null;
    const side = target.closest("[data-side]")?.getAttribute("data-side") as Side | null;
    const nodeId = target.closest("[data-node]")?.getAttribute("data-node") ?? null;
    const edgeId = target.closest("[data-edge]")?.getAttribute("data-edge") ?? null;
    if (event.button === 2) return;
    root.current?.setPointerCapture(event.pointerId);
    if (!panning && !readOnly && handle && nodeId) {
      const box = boxes.get(nodeId);
      if (box) gesture.current = { kind: "resize", pointerId: event.pointerId, id: nodeId, handle, start: point, origin: box };
      return;
    }
    if (!panning && !readOnly && side && nodeId) {
      gesture.current = { kind: "connect", pointerId: event.pointerId, from: nodeId, side, point };
      setConnecting({ from: nodeId, side, point });
      return;
    }
    if (!panning && edgeId && !readOnly) {
      select(event.shiftKey ? toggled(selected, edgeId) : [edgeId]);
      gesture.current = null;
      return;
    }
    if (!panning && nodeId && mode === "comment" && onPoint) {
      const node = shown.nodes.find((item) => item.id === nodeId);
      // A page is pointed at element by element inside its frame; its header still moves it.
      if (node && !frameOf(node)) {
        onPoint(node);
        gesture.current = null;
        return;
      }
    }
    if (!panning && nodeId && !readOnly && !(editing && editing.id === nodeId)) {
      const wasSelected = selected.has(nodeId);
      const additive = event.shiftKey || event.metaKey || event.ctrlKey;
      let next = selected;
      if (additive) next = toggled(selected, nodeId);
      else if (!wasSelected) next = new Set([nodeId]);
      select(next);
      const moving = movingSet(doc, [...next].filter((id) => boxes.has(id)));
      const origin = new Map<string, Box>();
      for (const id of moving) {
        const box = boxes.get(id);
        if (box) origin.set(id, { x: box.x, y: box.y, width: box.width, height: box.height });
      }
      gesture.current = { kind: "move", pointerId: event.pointerId, start: point, origin, moved: false, target: nodeId, additive, wasSelected };
      return;
    }
    if (!panning && !readOnly && event.shiftKey && event.pointerType !== "touch") {
      gesture.current = { kind: "marquee", pointerId: event.pointerId, start: point, current: point, base: new Set(selected) };
      return;
    }
    if (!nodeId && !edgeId && !event.shiftKey) select([]);
    gesture.current = { kind: "pan", pointerId: event.pointerId, start: { x: event.clientX, y: event.clientY }, view: viewRef.current };
  }

  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const current = gesture.current;
    if (!current) return;
    if (current.kind === "pinch") {
      const [a, b] = [...pointers.current.values()];
      if (!a || !b) return;
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      const center = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const rect = root.current!.getBoundingClientRect();
      const start = current.start;
      const zoom = clampZoom(start.view.zoom * (distance / Math.max(1, start.distance)));
      const worldX = (start.center.x - rect.left - start.view.x) / start.view.zoom;
      const worldY = (start.center.y - rect.top - start.view.y) / start.view.zoom;
      setView({ zoom, x: center.x - rect.left - worldX * zoom, y: center.y - rect.top - worldY * zoom });
      return;
    }
    if (current.pointerId !== event.pointerId) return;
    if (current.kind === "pan") {
      setView({ ...current.view, x: current.view.x + event.clientX - current.start.x, y: current.view.y + event.clientY - current.start.y });
      return;
    }
    const point = toWorld(event.clientX, event.clientY);
    if (current.kind === "move") {
      const dx = point.x - current.start.x;
      const dy = point.y - current.start.y;
      if (!current.moved && Math.hypot(dx, dy) * viewRef.current.zoom < DRAG_THRESHOLD) return;
      current.moved = true;
      const next = new Map<string, Box>();
      for (const [id, box] of current.origin) next.set(id, { ...box, x: Math.round(box.x + dx), y: Math.round(box.y + dy) });
      setLive(next);
    } else if (current.kind === "resize") {
      setLive(new Map([[current.id, resized(current.origin, current.handle, point.x - current.start.x, point.y - current.start.y)]]));
    } else if (current.kind === "connect") {
      current.point = point;
      setConnecting({ from: current.from, side: current.side, point });
    } else if (current.kind === "marquee") {
      current.current = point;
      const area = boxFrom(current.start, point);
      setMarquee(area);
      const hit = shown.nodes.filter((node) => intersects(area, boxes.get(node.id) ?? node)).map((node) => node.id);
      select(new Set([...current.base, ...hit]));
    }
  }

  function onPointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    pointers.current.delete(event.pointerId);
    const current = gesture.current;
    if (!current) return;
    if (current.kind === "pinch") {
      if (pointers.current.size < 2) gesture.current = null;
      return;
    }
    if (current.pointerId !== event.pointerId) return;
    gesture.current = null;
    if (current.kind === "move") {
      if (current.moved && live) {
        const ops: Op[] = [];
        for (const node of doc.nodes) {
          const box = live.get(node.id);
          if (box && (box.x !== node.x || box.y !== node.y)) ops.push({ op: "put", kind: "node", item: { ...node, x: box.x, y: box.y } });
        }
        emit(ops);
      } else if (!current.moved && current.wasSelected && !current.additive) {
        // A click on one of several selected cards narrows the selection to it, but only once it is
        // clear the click was not the start of dragging them all.
        select([current.target]);
      }
      setLive(null);
    } else if (current.kind === "resize") {
      const box = live?.get(current.id);
      const node = doc.nodes.find((item) => item.id === current.id);
      if (box && node && (box.width !== node.width || box.height !== node.height || box.x !== node.x || box.y !== node.y)) emit([{ op: "put", kind: "node", item: { ...node, ...box } }]);
      setLive(null);
    } else if (current.kind === "connect") {
      setConnecting(null);
      const from = boxes.get(current.from);
      const target = nodeAt(event.clientX, event.clientY, current.from);
      if (!from) return;
      const taken = new Set(shown.edges.map((edge) => edge.id));
      if (target) {
        const toSide = dropSide(boxes.get(target.id) ?? target, from, current.point);
        emit([{ op: "put", kind: "edge", item: { id: newId(taken), fromNode: current.from, fromSide: current.side, toNode: target.id, toSide } }]);
      } else if (Math.hypot(current.point.x - from.x, current.point.y - from.y) > 40) {
        // Let go over empty board: a new card there, already connected, waiting for its words.
        const node = newCard(current.point);
        const opposite: Record<Side, Side> = { top: "bottom", bottom: "top", left: "right", right: "left" };
        if (current.side === "right") node.x = Math.round(current.point.x);
        if (current.side === "left") node.x = Math.round(current.point.x - node.width);
        if (current.side === "bottom") node.y = Math.round(current.point.y);
        if (current.side === "top") node.y = Math.round(current.point.y - node.height);
        setDraft({ node, edge: { id: newId(taken), fromNode: current.from, fromSide: current.side, toNode: node.id, toSide: opposite[current.side] } });
        select([node.id]);
        setEditing({ id: node.id, kind: "text" });
      }
    } else if (current.kind === "marquee") {
      setMarquee(null);
    }
  }

  function onPointerCancel(event: ReactPointerEvent<HTMLDivElement>) {
    pointers.current.delete(event.pointerId);
    gesture.current = null;
    setLive(null);
    setConnecting(null);
    setMarquee(null);
  }

  function onDoubleClick(event: React.MouseEvent<HTMLDivElement>) {
    if (readOnly) return;
    // Not event.target: the board holds pointer capture from the press, and a click under capture is
    // reported on the board itself, so a double click on a card read as one on empty board.
    const target = (document.elementFromPoint(event.clientX, event.clientY) ?? event.target) as HTMLElement;
    if (target.closest("[data-ui]") || typing(target)) return;
    const nodeId = target.closest("[data-node]")?.getAttribute("data-node");
    const edgeId = target.closest("[data-edge]")?.getAttribute("data-edge");
    if (nodeId) {
      const node = shown.nodes.find((item) => item.id === nodeId);
      // A double click inside a group's empty middle makes a card in the group, as on the board.
      if (node?.type === "group" && !target.closest(".cv-group-label")) addCard(toWorld(event.clientX, event.clientY));
      else startEditing(nodeId);
      return;
    }
    if (edgeId) {
      select([edgeId]);
      startEditing(edgeId);
      return;
    }
    addCard(toWorld(event.clientX, event.clientY));
  }

  // -- keys --------------------------------------------------------------------------------------

  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (typing(event.target)) return;
    const mod = event.ctrlKey || event.metaKey;
    const key = event.key.toLowerCase();
    if (event.key === " ") {
      spaceHeld.current = true;
      return;
    }
    if (mod && (key === "z" || key === "я")) {
      event.preventDefault();
      if (event.shiftKey) onRedo?.();
      else onUndo?.();
      return;
    }
    if (mod && (key === "y" || key === "н")) {
      event.preventDefault();
      onRedo?.();
      return;
    }
    if (mod && (key === "a" || key === "ф")) {
      event.preventDefault();
      select(doc.nodes.map((node) => node.id));
      return;
    }
    if (mod && (key === "g" || key === "п") && !readOnly) {
      event.preventDefault();
      groupSelection();
      return;
    }
    if (event.key === "Escape") {
      select([]);
      return;
    }
    if ((event.key === "Delete" || event.key === "Backspace") && !readOnly) {
      event.preventDefault();
      removeSelection();
      return;
    }
    if (event.key === "Enter" && selected.size === 1 && !readOnly) {
      event.preventDefault();
      startEditing([...selected][0]);
      return;
    }
    const arrows: Record<string, Point> = { ArrowLeft: { x: -1, y: 0 }, ArrowRight: { x: 1, y: 0 }, ArrowUp: { x: 0, y: -1 }, ArrowDown: { x: 0, y: 1 } };
    const step = arrows[event.key];
    if (step && selected.size && !readOnly) {
      event.preventDefault();
      const size = event.shiftKey ? NUDGE * 5 : NUDGE;
      nudge(step.x * size, step.y * size);
      return;
    }
    if (step && !selected.size) {
      event.preventDefault();
      setView((current) => ({ ...current, x: current.x - step.x * 60, y: current.y - step.y * 60 }));
      return;
    }
    if (event.key === "+" || event.key === "=") zoomAt(1.2);
    else if (event.key === "-") zoomAt(1 / 1.2);
    else if (event.key === ")" || (event.key === "0" && event.shiftKey)) fit();
  }

  function onKeyUp(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key === " ") spaceHeld.current = false;
  }

  // -- drawing -----------------------------------------------------------------------------------

  const ordered = drawOrder(shown);
  const groups = ordered.filter((node) => node.type === "group");
  const others = ordered.filter((node) => node.type !== "group");
  const extent = bounds([...boxes.values(), ...(connecting ? [{ ...connecting.point, width: 1, height: 1 }] : [])]) ?? { x: 0, y: 0, width: 1, height: 1 };
  const margin = 400;
  const svgBox = { x: extent.x - margin, y: extent.y - margin, width: extent.width + margin * 2, height: extent.height + margin * 2 };
  const single = selected.size === 1 ? [...selected][0] : null;
  // The part of the plane on screen, with a screen's margin round it: a live page far outside it is a
  // placeholder, so a board of many pages, or a phone, runs only the ones in reach.
  const rootWidth = root.current?.clientWidth ?? 1600;
  const rootHeight = root.current?.clientHeight ?? 1000;
  const reach: Box = { x: (-view.x - rootWidth) / view.zoom, y: (-view.y - rootHeight) / view.zoom, width: (rootWidth * 3) / view.zoom, height: (rootHeight * 3) / view.zoom };
  const frameBody = (node: CanvasNode) => (renderFrame && frameOf(node) ? renderFrame(node, intersects(reach, boxes.get(node.id) ?? node)) : null);
  const selectedNodes = shown.nodes.filter((node) => selected.has(node.id));
  const selectedEdges = shown.edges.filter((edge) => selected.has(edge.id));
  const style = {
    "--cv-inv": String(1 / view.zoom),
    backgroundSize: `${GRID * view.zoom}px ${GRID * view.zoom}px`,
    backgroundPosition: `${view.x}px ${view.y}px`,
  } as CSSProperties;

  return (
    <div
      ref={root}
      className={`cv-board ${readOnly ? "read-only" : ""} ${connecting ? "connecting" : ""} ${busy ? "busy" : ""} mode-${mode}`}
      role="application"
      aria-label={label}
      aria-roledescription={t("canvas.board")}
      tabIndex={0}
      style={style}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onDoubleClick={onDoubleClick}
      onKeyDown={onKeyDown}
      onKeyUp={onKeyUp}
      // The board never scrolls: it moves by its view. Focusing a field on the plane (a card being typed,
      // a note beside a picked element) would otherwise scroll this clipped box to bring it in, and
      // shift every card and control on it out of place.
      onScroll={(event) => { event.currentTarget.scrollTop = 0; event.currentTarget.scrollLeft = 0; }}
      onContextMenu={(event) => { if (!(event.target as HTMLElement).closest("a")) event.preventDefault(); }}
    >
      <div className="cv-layer" style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})` }}>
        {groups.map((node) => (
          <NodeFrame key={node.id} node={node} box={boxes.get(node.id)!} selected={selected.has(node.id)} single={single === node.id && !readOnly} marked={marked?.has(node.id)} editing={editing?.id === node.id ? editing.kind : null} onCommit={commitEdit} fileUrl={fileUrl} />
        ))}
        <svg className="cv-edges" style={{ left: svgBox.x, top: svgBox.y, width: svgBox.width, height: svgBox.height }} viewBox={`${svgBox.x} ${svgBox.y} ${svgBox.width} ${svgBox.height}`} aria-hidden="true">
          {shown.edges.map((edge) => {
            const from = boxes.get(edge.fromNode);
            const to = boxes.get(edge.toNode);
            if (!from || !to) return null;
            return <EdgePath key={edge.id} edge={edge} from={from} to={to} selected={selected.has(edge.id)} marked={marked?.has(edge.id)} />;
          })}
          {connecting && boxes.get(connecting.from) && <path className="cv-edge-draft" d={draftPath(boxes.get(connecting.from)!, connecting.side, connecting.point)} />}
        </svg>
        {shown.edges.map((edge) => {
          const from = boxes.get(edge.fromNode);
          const to = boxes.get(edge.toNode);
          if (!from || !to) return null;
          const editingThis = editing?.kind === "edge" && editing.id === edge.id;
          if (!edge.label && !editingThis) return null;
          const geometry = edgeGeometry(from, to, edge.fromSide, edge.toSide);
          return (
            <div key={edge.id} className={`cv-edge-label ${selected.has(edge.id) ? "selected" : ""}`} data-edge={edge.id} style={{ left: geometry.label.x, top: geometry.label.y, ...(edge.color ? { "--cv-ink": colorValue(edge.color) } as CSSProperties : {}) }}>
              {editingThis ? <InlineField value={edge.label ?? ""} label={t("canvas.edge.label")} onDone={commitEdit} /> : edge.label}
            </div>
          );
        })}
        {others.map((node) => (
          <NodeFrame key={node.id} node={node} box={boxes.get(node.id)!} selected={selected.has(node.id)} single={single === node.id && !readOnly} marked={marked?.has(node.id)} editing={editing?.id === node.id ? editing.kind : null} onCommit={commitEdit} fileUrl={fileUrl} frame={frameBody(node)} />
        ))}
        {overlay}
        {marquee && <div className="cv-marquee" style={{ left: marquee.x, top: marquee.y, width: marquee.width, height: marquee.height }} />}
      </div>
      {!shown.nodes.length && !readOnly && (
        <div className="cv-empty-hint" aria-hidden="true">{t(compact ? "canvas.empty.hint.touch" : "canvas.empty.hint")}</div>
      )}
      {!readOnly && (
        <div className="cv-toolbar" data-ui role="toolbar" aria-label={t("canvas.tools")}>
          {onModeChange && (
            <>
              <div className="cv-mode" role="group" aria-label={t("canvas.mode")}>
                <button type="button" className={mode === "interact" ? "on" : ""} aria-pressed={mode === "interact"} onClick={() => onModeChange("interact")} title={t("canvas.mode.interact.hint")}>{t("canvas.mode.interact")}</button>
                <button type="button" className={mode === "comment" ? "on" : ""} aria-pressed={mode === "comment"} onClick={() => onModeChange("comment")} title={t("canvas.mode.comment.hint")}>{t("canvas.mode.comment")}</button>
              </div>
              <span className="cv-toolbar-sep" />
            </>
          )}
          <ToolButton icon="note" label={t("canvas.add.card")} onClick={() => addCard()} />
          <ToolButton icon="link" label={t("canvas.add.link")} onClick={addLink} />
          {selected.size > 0 && <span className="cv-toolbar-sep" />}
          {single && <ToolButton icon="pen" label={t("canvas.edit")} onClick={() => startEditing(single)} disabled={!!selectedNodes[0] && selectedNodes[0].type === "file"} />}
          {selected.size > 0 && <ToolButton icon="sliders" label={t("canvas.color")} onClick={() => setPalette((open) => !open)} pressed={palette} />}
          {selectedNodes.length > 0 && <ToolButton icon="region" label={t("canvas.group")} onClick={groupSelection} />}
          {selectedEdges.length > 0 && selectedNodes.length === 0 && (
            <>
              <ToolButton icon="back" label={t("canvas.edge.start")} onClick={() => toggleEnd("fromEnd")} pressed={selectedEdges.every((edge) => edge.fromEnd === "arrow")} />
              <ToolButton icon="forward" label={t("canvas.edge.end")} onClick={() => toggleEnd("toEnd")} pressed={selectedEdges.every((edge) => (edge.toEnd ?? "arrow") === "arrow")} />
            </>
          )}
          {selected.size > 0 && <ToolButton icon="trash" label={t("canvas.delete")} onClick={removeSelection} />}
          <span className="cv-toolbar-sep" />
          <ToolButton icon="undo" label={t("canvas.undo")} onClick={() => onUndo?.()} disabled={!canUndo} />
          <ToolButton icon="undo" label={t("canvas.redo")} onClick={() => onRedo?.()} disabled={!canRedo} className="cv-redo" />
          {palette && (
            <div className="cv-palette" role="group" aria-label={t("canvas.color")}>
              <button type="button" className="cv-swatch none" aria-label={t("canvas.color.none")} title={t("canvas.color.none")} onClick={() => paint("")} />
              {PRESETS.map((preset) => (
                <button key={preset} type="button" className="cv-swatch" style={{ background: colorValue(preset) }} aria-label={t(`canvas.color.${preset}`)} title={t(`canvas.color.${preset}`)} onClick={() => paint(preset)} />
              ))}
            </div>
          )}
        </div>
      )}
      <div className="cv-zoom" data-ui role="group" aria-label={t("canvas.zoom")}>
        <ToolButton icon="minus" label={t("canvas.zoom.out")} onClick={() => zoomAt(1 / 1.25)} />
        <button type="button" className="cv-zoom-level" onClick={() => setView((current) => ({ ...current, zoom: 1 }))} aria-label={t("canvas.zoom.reset")} title={t("canvas.zoom.reset")}>{Math.round(view.zoom * 100)}%</button>
        <ToolButton icon="plus" label={t("canvas.zoom.in")} onClick={() => zoomAt(1.25)} />
        <ToolButton icon="expand" label={t("canvas.fit")} onClick={() => fit()} />
      </div>
    </div>
  );
}

/** The side an edge dropped on a card arrives at: the side the pointer was let go near, when it was
 *  near one; otherwise the side facing where the edge comes from. Dropped in the middle, the nearest
 *  side is a coin toss between top and bottom, and the curve looped round the card to reach it. */
function dropSide(target: Box, from: Box, point: Point): Side {
  const near = Math.min(Math.abs(point.y - target.y), Math.abs(point.y - target.y - target.height), Math.abs(point.x - target.x), Math.abs(point.x - target.x - target.width));
  if (near < Math.min(target.width, target.height) * 0.2) return nearestSide(target, point);
  return facingSide(target, { x: from.x + from.width / 2, y: from.y + from.height / 2 });
}

function toggled(set: Set<string>, id: string): Set<string> {
  const next = new Set(set);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/** Sets a key, or removes it when the value is empty, so an unset colour or label is not written as "". */
function withOptional<T extends Record<string, unknown>>(item: T, key: string, value: string): T {
  const next: Record<string, unknown> = { ...item };
  if (value) next[key] = value;
  else delete next[key];
  return next as T;
}

function normaliseUrl(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) return "";
  return /^[a-z][a-z0-9+.-]*:/i.test(trimmed) ? trimmed : `https://${trimmed}`;
}

/** A new card grows to its words; it never shrinks below the default it was made with. */
function fitGrow(node: CanvasNode, text: string): { width: number; height: number } {
  const size = fitText(text);
  return { width: Math.max(node.width, size.width), height: Math.max(node.height, size.height) };
}

function resized(origin: Box, handle: Handle, dx: number, dy: number): Box {
  let { x, y, width, height } = origin;
  if (handle.includes("e")) width = Math.max(MIN_WIDTH, origin.width + dx);
  if (handle.includes("s")) height = Math.max(MIN_HEIGHT, origin.height + dy);
  if (handle.includes("w")) {
    width = Math.max(MIN_WIDTH, origin.width - dx);
    x = origin.x + origin.width - width;
  }
  if (handle.includes("n")) {
    height = Math.max(MIN_HEIGHT, origin.height - dy);
    y = origin.y + origin.height - height;
  }
  return { x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height) };
}

function ToolButton({ icon, label, onClick, disabled, pressed, className }: { icon: IconName | "minus"; label: string; onClick: () => void; disabled?: boolean; pressed?: boolean; className?: string }) {
  return (
    <button type="button" className={`cv-tool ${className ?? ""} ${pressed ? "on" : ""}`} onClick={onClick} disabled={disabled} aria-label={label} title={label} aria-pressed={pressed === undefined ? undefined : pressed}>
      {icon === "minus" ? <svg className="ic" viewBox="0 0 24 24" width={16} height={16} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true"><path d="M5 12h14" /></svg> : <Icon name={icon} size={16} />}
    </button>
  );
}

const HANDLES: Handle[] = ["nw", "n", "ne", "e", "se", "s", "sw", "w"];

type FrameProps = { node: CanvasNode; box: Box; selected: boolean; single: boolean; marked?: boolean; editing: Editing["kind"] | null; onCommit: (value: string | null) => void; fileUrl: (fileId: string, name: string) => string; frame?: ReactNode };

/** One node at its place. The place changes on every frame of a drag; the content (`NodeBody`) is
 *  memoised on the node itself, so Markdown is not rendered again while the card merely moves. */
function NodeFrame({ node, box, selected, single, marked, editing, onCommit, fileUrl, frame }: FrameProps) {
  const ink = colorValue(node.color);
  const shape = node.daedalus?.shape;
  return (
    <div
      className={`cv-node cv-${node.type} ${frame ? "cv-live" : ""} ${selected ? "selected" : ""} ${marked ? "marked" : ""} ${ink ? "tinted" : ""} ${shape ? `shape-${shape}` : ""}`}
      data-node={node.id}
      role="group"
      aria-label={nodeLabel(node, t(`canvas.kind.${node.type}`))}
      style={{ left: box.x, top: box.y, width: box.width, height: box.height, ...(ink ? { "--cv-ink": ink } as CSSProperties : {}) }}
    >
      {frame ? frame : node.type === "group" ? (
        <div className="cv-group-label">{editing === "label" ? <InlineField value={node.label ?? ""} label={t("canvas.group.label")} onDone={onCommit} /> : node.label || (selected ? t("canvas.group.unnamed") : "")}</div>
      ) : editing === "text" ? (
        <TextField value={node.text ?? ""} onDone={onCommit} />
      ) : editing === "url" ? (
        <div className="cv-card-body"><InlineField value={node.url ?? ""} label={t("canvas.link.url")} placeholder="https://" onDone={onCommit} /></div>
      ) : (
        <NodeBody node={node} fileUrl={fileUrl} />
      )}
      {single && (
        <>
          {HANDLES.map((handle) => <span key={handle} className={`cv-handle ${handle}`} data-handle={handle} aria-hidden="true" />)}
        </>
      )}
      {node.type !== "group" && SIDES.map((side) => <span key={side} className={`cv-side ${side}`} data-side={side} aria-hidden="true" />)}
    </div>
  );
}

const NodeBody = memo(function NodeBody({ node, fileUrl }: { node: CanvasNode; fileUrl: (fileId: string, name: string) => string }) {
  if (node.type === "text") {
    return <RawHtml className="cv-card-body md" html={renderMarkdown(node.text ?? "")} />;
  }
  if (node.type === "link") {
    const href = safeUrl(node.url);
    let host = node.url ?? "";
    try { if (href) host = new URL(href).host; } catch { /* shown as written */ }
    return (
      <div className="cv-card-body cv-link-body">
        <Icon name="globe" size={18} />
        <span className="cv-link-text">
          <strong>{host || t("canvas.link.empty")}</strong>
          {href ? <a href={href} target="_blank" rel="noopener noreferrer" onPointerDown={(event) => event.stopPropagation()}>{node.url}</a> : <span>{node.url}</span>}
        </span>
      </div>
    );
  }
  const file = attachment(node.file);
  if (file && isImage(file.name)) {
    return <FileImage src={fileUrl(file.id, file.name)} name={file.name} />;
  }
  const name = file?.name ?? (node.file ?? "").split("/").pop() ?? "";
  return (
    <div className="cv-card-body cv-file-body">
      <Icon name="file" size={20} />
      <span className="cv-link-text">
        <strong>{name}</strong>
        {file ? <a href={fileUrl(file.id, file.name)} download={file.name} onPointerDown={(event) => event.stopPropagation()} onClick={(event) => { event.preventDefault(); void saveFile(fileUrl(file.id, file.name), file.name); }}>{t("canvas.file.download")}</a> : <span className="cv-file-path">{node.file}{node.subpath ?? ""}</span>}
      </span>
    </div>
  );
});

/** An authenticated route is fetched with the app's credentials and shown from memory; a public one
 *  (the shared view) is an ordinary picture. */
function FileImage({ src, name }: { src: string; name: string }) {
  const authed = src.startsWith("/api/") && !src.startsWith("/api/public/");
  const [url, setUrl] = useState<string | null>(authed ? null : src);
  useEffect(() => {
    if (!authed) return;
    let made = "";
    let active = true;
    api.fetch(src).then((response) => (response.ok ? response.blob() : null)).then((blob) => {
      if (!blob || !active) return;
      made = URL.createObjectURL(blob);
      setUrl(made);
    }).catch(() => undefined);
    return () => { active = false; if (made) URL.revokeObjectURL(made); };
  }, [authed, src]);
  return url ? <img className="cv-image" src={url} alt={name} draggable={false} /> : <div className="cv-image cv-image-wait" role="img" aria-label={name} />;
}

async function saveFile(src: string, name: string): Promise<void> {
  const authed = src.startsWith("/api/") && !src.startsWith("/api/public/");
  const response = authed ? await api.fetch(src) : await fetch(src);
  if (!response.ok) return;
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function EdgePath({ edge, from, to, selected, marked }: { edge: CanvasEdge; from: Box; to: Box; selected: boolean; marked?: boolean }) {
  const geometry = edgeGeometry(from, to, edge.fromSide, edge.toSide);
  const ink = colorValue(edge.color);
  const style = ink ? ({ "--cv-ink": ink } as CSSProperties) : undefined;
  return (
    <g className={`cv-edge ${selected ? "selected" : ""} ${marked ? "marked" : ""}`} data-edge={edge.id} style={style}>
      <path className="cv-edge-hit" d={geometry.path} />
      <path className="cv-edge-line" d={geometry.path} />
      {(edge.toEnd ?? "arrow") === "arrow" && <polygon className="cv-edge-head" points={arrowHead(geometry.to, geometry.toAngle)} />}
      {edge.fromEnd === "arrow" && <polygon className="cv-edge-head" points={arrowHead(geometry.from, geometry.fromAngle)} />}
    </g>
  );
}

/** The Markdown of a card being typed. Ctrl+Enter or Escape or leaving the field ends the edit and keeps
 *  the words: Escape throwing them away would be the one key that loses work. */
function TextField({ value, onDone }: { value: string; onDone: (value: string | null) => void }) {
  const field = useRef<HTMLTextAreaElement>(null);
  const done = useRef(false);
  useEffect(() => {
    const element = field.current;
    if (!element) return;
    element.focus({ preventScroll: true });
    element.setSelectionRange(element.value.length, element.value.length);
  }, []);
  const finish = (next: string | null) => {
    if (done.current) return;
    done.current = true;
    onDone(next);
  };
  return (
    <textarea
      ref={field}
      className="cv-text-field"
      defaultValue={value}
      aria-label={t("canvas.card.text")}
      onPointerDown={(event) => event.stopPropagation()}
      onKeyDown={(event) => {
        event.stopPropagation();
        if (event.key === "Escape" || (event.key === "Enter" && (event.ctrlKey || event.metaKey))) {
          event.preventDefault();
          finish(event.currentTarget.value);
          (event.currentTarget.closest(".cv-board") as HTMLElement | null)?.focus({ preventScroll: true });
        }
      }}
      onBlur={(event) => finish(event.currentTarget.value)}
    />
  );
}

function InlineField({ value, label, placeholder, onDone }: { value: string; label: string; placeholder?: string; onDone: (value: string | null) => void }): ReactNode {
  const done = useRef(false);
  const finish = (next: string | null) => {
    if (done.current) return;
    done.current = true;
    onDone(next);
  };
  return (
    <input
      ref={(element) => { if (element && !done.current && document.activeElement !== element) element.focus({ preventScroll: true }); }}
      className="cv-inline-field"
      defaultValue={value}
      placeholder={placeholder}
      aria-label={label}
      onPointerDown={(event) => event.stopPropagation()}
      onFocus={(event) => event.currentTarget.select()}
      onKeyDown={(event) => {
        event.stopPropagation();
        if (event.key === "Enter" || event.key === "Escape") {
          event.preventDefault();
          finish(event.key === "Escape" && !event.currentTarget.value.trim() && value ? null : event.currentTarget.value);
          (event.currentTarget.closest(".cv-board") as HTMLElement | null)?.focus({ preventScroll: true });
        }
      }}
      onBlur={(event) => finish(event.currentTarget.value)}
    />
  );
}
