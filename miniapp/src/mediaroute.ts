// Where a click on a file in the conversation goes. A picture or a clip opens in the centred viewer,
// with the conversation's other media a swipe away; anything else opens in the files panel. Every
// thumbnail in the steps used to open the panel, which is where a code file belongs and not where
// anyone expects a screenshot to appear.

import type { SessionFolder } from "./api";
import { folderBase } from "./folders";
import { previewKind, sessionBase, type PreviewSource } from "./preview";
import { placeOf, turnFiles, type ChangedFile } from "./turnfiles";
import type { Activity, ToolItem, Turn } from "./turns";

/** A file under a session API root, the only kind the viewer can fetch: a composer file is not one. */
export type HostSource = { base: string; path: string };

type Folders = readonly Pick<SessionFolder, "id" | "path">[];

/** What the viewer makes of a file name: a still, a clip, or nothing it shows. A PDF stays a document
 *  for the panel, which pages it; the viewer only zooms a single picture. */
export function viewerKind(name: string): "image" | "video" | null {
  const kind = previewKind(name);
  return kind === "image" || kind === "video" ? kind : null;
}

/** The source as the viewer would take it, or null when it belongs to the panel. */
export function viewerSource(src: PreviewSource): HostSource | null {
  if ("file" in src || src.lines) return null;
  return viewerKind(src.path) ? { base: src.base, path: src.path } : null;
}

export const sourceKey = (src: HostSource) => `${src.base}\n${src.path}`;

/**
 * Where the panel would fetch a step's file: the workspace or a project folder, found the way the
 * changed files find theirs. A plain prefix test against the workspace missed a Windows path, a path
 * spelled from the home folder and every attached folder, and the step then showed no picture at all.
 */
export function toolSource(item: ToolItem, sessionId: string, workspace: string, folders: Folders): HostSource | null {
  const path = typeof item.args.path === "string" ? item.args.path : "";
  const place = path ? placeOf(path, workspace, folders) : null;
  if (!place || !place.rel) return null;
  return { base: folderBase(sessionBase(sessionId), place.folder, ""), path: place.rel };
}

/** A sent file is served by the call that sent it, so a path outside every folder opens too. */
export function sentSource(sessionId: string, callId: string, name: string): HostSource {
  return { base: `${sessionBase(sessionId)}/sent/${encodeURIComponent(callId)}`, path: name };
}

/** Where a changed file is served from: its folder's address and its path there. */
export function changedSource(sessionId: string, file: ChangedFile): HostSource {
  const base = sessionBase(sessionId);
  return file.place ? { base: folderBase(base, file.place.folder, ""), path: file.place.rel } : { base, path: file.path };
}

function stepMedia(activity: readonly Activity[], sessionId: string, workspace: string, folders: Folders): HostSource[] {
  const found: HostSource[] = [];
  for (const a of activity) {
    if (a.kind !== "tool" || a.running || a.name !== "ImageView") continue;
    const src = toolSource(a, sessionId, workspace, folders);
    if (src && viewerKind(src.path)) found.push(src);
  }
  return found;
}

/**
 * Every picture and clip of the conversation in reading order, each once: within a turn the images
 * its steps looked at, then the files it sent, then the files it changed — the order they are drawn.
 */
export function conversationMedia(turns: readonly Turn[], sessionId: string, workspace: string, folders: Folders): HostSource[] {
  const seen = new Set<string>();
  const out: HostSource[] = [];
  const add = (src: HostSource) => {
    const key = sourceKey(src);
    if (seen.has(key)) return;
    seen.add(key);
    out.push(src);
  };
  for (const turn of turns) {
    stepMedia(turn.activity, sessionId, workspace, folders).forEach(add);
    const { sent, changed } = turnFiles(turn.activity, workspace, folders);
    for (const file of sent) if (viewerKind(file.name)) add(sentSource(sessionId, file.callId, file.name));
    for (const file of changed) if (viewerKind(file.name)) add(changedSource(sessionId, file));
  }
  return out;
}

/** The list the viewer pages through and where it starts. A file the list does not hold — a kept
 *  attachment, a cited path — opens alone rather than dropping the reader at someone else's picture. */
export function galleryFor(all: readonly HostSource[], src: HostSource): { items: HostSource[]; index: number } {
  const index = all.findIndex((item) => sourceKey(item) === sourceKey(src));
  return index < 0 ? { items: [src], index: 0 } : { items: [...all], index };
}
