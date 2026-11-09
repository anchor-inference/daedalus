// Changes to a canvas travel as operations on single items, never as the whole document. That is what
// lets the host merge: a save made on an older version is still applied when nobody else touched the
// same cards, so the operator dragging one card and the agent rewriting another do not refuse each
// other the way whole-file saves did.
//
// The board says what the operator did as a batch of operations; the editor applies the batch here,
// remembers its inverse for Undo, and queues it for the next save.

import type { CanvasDoc, CanvasEdge, CanvasNode } from "./model";

export type PutOp = { op: "put"; kind: "node"; item: CanvasNode; at?: "front" | "back" } | { op: "put"; kind: "edge"; item: CanvasEdge; at?: "front" | "back" };
export type DeleteOp = { op: "delete"; id: string };
export type Op = PutOp | DeleteOp;

function place<T extends { id: string }>(list: T[], item: T, at?: "front" | "back"): T[] {
  const index = list.findIndex((existing) => existing.id === item.id);
  if (index >= 0 && !at) {
    const next = list.slice();
    next[index] = item;
    return next;
  }
  const rest = index >= 0 ? list.filter((existing) => existing.id !== item.id) : list;
  return at === "back" ? [item, ...rest] : [...rest, item];
}

/** The document after `ops`, as the host applies them: a put replaces by id in place (or moves the
 *  item to the front or back), a delete of a node takes its edges along. A put of an edge whose node is
 *  gone is dropped, which is what the host would refuse, rather than drawn hanging. */
export function applyOps(doc: CanvasDoc, ops: readonly Op[]): CanvasDoc {
  let nodes = doc.nodes;
  let edges = doc.edges;
  for (const op of ops) {
    if (op.op === "delete") {
      if (nodes.some((node) => node.id === op.id)) {
        nodes = nodes.filter((node) => node.id !== op.id);
        edges = edges.filter((edge) => edge.fromNode !== op.id && edge.toNode !== op.id);
      } else {
        edges = edges.filter((edge) => edge.id !== op.id);
      }
    } else if (op.kind === "node") {
      nodes = place(nodes, op.item, op.at);
    } else {
      const ids = new Set(nodes.map((node) => node.id));
      if (ids.has(op.item.fromNode) && ids.has(op.item.toNode)) edges = place(edges, op.item, op.at);
    }
  }
  return { ...doc, nodes, edges };
}

/** The operations that undo `ops` on `doc`, in the order they must run. */
export function invertOps(doc: CanvasDoc, ops: readonly Op[]): Op[] {
  const inverse: Op[][] = [];
  let current = doc;
  for (const op of ops) {
    const step: Op[] = [];
    if (op.op === "delete") {
      const node = current.nodes.find((item) => item.id === op.id);
      if (node) {
        step.push({ op: "put", kind: "node", item: node, at: node.type === "group" ? "back" : undefined });
        for (const edge of current.edges) if (edge.fromNode === op.id || edge.toNode === op.id) step.push({ op: "put", kind: "edge", item: edge });
      } else {
        const edge = current.edges.find((item) => item.id === op.id);
        if (edge) step.push({ op: "put", kind: "edge", item: edge });
      }
    } else {
      const list: { id: string }[] = op.kind === "node" ? current.nodes : current.edges;
      const before = list.find((item) => item.id === op.item.id);
      if (before) step.push(op.kind === "node" ? { op: "put", kind: "node", item: before as CanvasNode } : { op: "put", kind: "edge", item: before as CanvasEdge });
      else step.push({ op: "delete", id: op.item.id });
    }
    inverse.push(step);
    current = applyOps(current, [op]);
  }
  return inverse.reverse().flat();
}

/** Every id a batch touches, including the edges a node's deletion takes with it. */
export function touchedIds(doc: CanvasDoc, ops: readonly Op[]): Set<string> {
  const ids = new Set<string>();
  for (const op of ops) {
    const id = op.op === "delete" ? op.id : op.item.id;
    ids.add(id);
    if (op.op === "delete") for (const edge of doc.edges) if (edge.fromNode === id || edge.toNode === id) ids.add(edge.id);
  }
  return ids;
}

/** Folds a queue of batches into what the host needs: the last put of an item replaces earlier ones,
 *  so dragging a card across the board in thirty steps is saved as one put. Order is kept otherwise,
 *  because a delete then a re-create of the same id must stay in that order. */
export function compact(ops: readonly Op[]): Op[] {
  const result: Op[] = [];
  for (const op of ops) {
    if (op.op === "put") {
      const earlier = result.findIndex((item) => item.op === "put" && item.item.id === op.item.id && item.kind === op.kind);
      const between = earlier >= 0 && result.slice(earlier + 1).some((item) => (item.op === "delete" ? item.id : item.item.id) === op.item.id);
      if (earlier >= 0 && !between && !op.at) {
        const prior = result[earlier] as PutOp;
        result[earlier] = { ...op, at: prior.at } as PutOp;
        continue;
      }
    }
    result.push(op);
  }
  return result;
}

/** The operations that take `before` to `after`, item by item. Used where a change is easiest to make
 *  on a whole document (a restore, a template): the result is still a batch of puts and deletes. */
export function diffDocs(before: CanvasDoc, after: CanvasDoc): Op[] {
  const ops: Op[] = [];
  const afterNodes = new Map(after.nodes.map((node) => [node.id, node]));
  const afterEdges = new Map(after.edges.map((edge) => [edge.id, edge]));
  for (const edge of before.edges) if (!afterEdges.has(edge.id) && afterNodes.has(edge.fromNode) && afterNodes.has(edge.toNode)) ops.push({ op: "delete", id: edge.id });
  for (const node of before.nodes) if (!afterNodes.has(node.id)) ops.push({ op: "delete", id: node.id });
  const beforeNodes = new Map(before.nodes.map((node) => [node.id, node]));
  const beforeEdges = new Map(before.edges.map((edge) => [edge.id, edge]));
  for (const node of after.nodes) {
    const old = beforeNodes.get(node.id);
    if (!old || JSON.stringify(old) !== JSON.stringify(node)) ops.push({ op: "put", kind: "node", item: node });
  }
  for (const edge of after.edges) {
    const old = beforeEdges.get(edge.id);
    if (!old || JSON.stringify(old) !== JSON.stringify(edge)) ops.push({ op: "put", kind: "edge", item: edge });
  }
  return ops;
}

type Entry = { forward: Op[]; backward: Op[] };

/** Undo and redo over batches of operations. Each batch is one thing the operator did (a drag, a typed
 *  card, a deletion), so one press of Undo takes back one action and not one pixel of a drag. */
export class UndoStack {
  private done: Entry[] = [];
  private undone: Entry[] = [];
  constructor(private readonly limit = 200) {}

  record(doc: CanvasDoc, ops: Op[]): void {
    if (!ops.length) return;
    this.done.push({ forward: ops, backward: invertOps(doc, ops) });
    if (this.done.length > this.limit) this.done.shift();
    this.undone = [];
  }

  /** The batch that undoes the last action, given the document as it is now (another writer may have
   *  changed it since): an inverse put of an item that someone else deleted would resurrect it, so
   *  only items that still exist, or that this action deleted, are put back. */
  undo(doc: CanvasDoc): Op[] | null {
    const entry = this.done.pop();
    if (!entry) return null;
    this.undone.push(entry);
    return reconcile(doc, entry.backward, entry.forward);
  }

  redo(doc: CanvasDoc): Op[] | null {
    const entry = this.undone.pop();
    if (!entry) return null;
    this.done.push(entry);
    return reconcile(doc, entry.forward, entry.backward);
  }

  get canUndo(): boolean {
    return this.done.length > 0;
  }

  get canRedo(): boolean {
    return this.undone.length > 0;
  }

  clear(): void {
    this.done = [];
    this.undone = [];
  }
}

function reconcile(doc: CanvasDoc, ops: Op[], counterpart: Op[]): Op[] {
  const present = new Set([...doc.nodes.map((node) => node.id), ...doc.edges.map((edge) => edge.id)]);
  const removedByCounterpart = new Set(counterpart.filter((op): op is DeleteOp => op.op === "delete").map((op) => op.id));
  const createdByCounterpart = new Set(counterpart.filter((op): op is PutOp => op.op === "put").map((op) => op.item.id));
  return ops.filter((op) => {
    const id = op.op === "delete" ? op.id : op.item.id;
    if (op.op === "delete") return present.has(id);
    return present.has(id) || removedByCounterpart.has(id) || !createdByCounterpart.has(id);
  });
}
