export type DiagramChange = { id: string; type: string };
export type DiagramDiff = { added: DiagramChange[]; removed: DiagramChange[]; changed: DiagramChange[] };

type Element = { id: string; type: string; [key: string]: unknown };

function content(element: Element): string {
  // Excalidraw changes these bookkeeping fields on a redraw. A version diff should say
  // what changed on the canvas, rather than reporting every element as edited.
  const { version: _version, versionNonce: _nonce, updated: _updated, seed: _seed, ...visible } = element;
  return JSON.stringify(visible);
}

export function diagramDiff(before: readonly Element[], after: readonly Element[]): DiagramDiff {
  const old = new Map(before.map((element) => [element.id, element]));
  const next = new Map(after.map((element) => [element.id, element]));
  const added = after.filter((element) => !old.has(element.id)).map(({ id, type }) => ({ id, type }));
  const removed = before.filter((element) => !next.has(element.id)).map(({ id, type }) => ({ id, type }));
  const changed = after.filter((element) => old.has(element.id) && content(old.get(element.id)!) !== content(element)).map(({ id, type }) => ({ id, type }));
  return { added, removed, changed };
}
