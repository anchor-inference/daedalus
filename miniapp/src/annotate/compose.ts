// Handing a file and a line of text to a conversation's composer from somewhere that does not hold it:
// the Browser tab and the Preview tab sit in the right panel, a sibling of the composer, and the
// browser's page of its own has no composer on screen at all.
//
// The composer is reached by a window event its screen listens for, carrying the session it is meant
// for. Nothing in the panel holds a reference to the composer, and a ref threaded through the panel's
// props would have to cross the Browser tab, the phone sheet and the drive mode's portal to arrive.
// When no screen took the event (the browser's own page, a chat that is still loading), the file and
// the line go into that session's saved draft, which its composer reads the next time it opens.

import { createContext } from "react";
import { readDraft, writeDraft } from "../composer";
import { loadDraftFiles, saveDraftFiles } from "../project/draftfiles";

export const COMPOSE_EVENT = "daedalus:compose";

/** `taken` is set by the screen that put the files into its composer, so no second one does too. */
export type ComposeRequest = { sessionId: string; files: File[]; text: string; taken: boolean };

/**
 * The conversation whose composer a snapshot from the panel goes to. Provided around the panel by the
 * session screen; React context crosses portals, so the phone's sheet and drive mode read it too.
 */
export const ComposeTarget = createContext<string | null>(null);

/** A line after whatever the draft holds, with the blank line the composer's own insert leaves. */
export function appendLine(draft: string, text: string): string {
  if (!text) return draft;
  return draft.trim() ? `${draft.trimEnd()}\n\n${text}` : text;
}

/** Where the files went: into a composer on screen, or into the session's saved draft. */
export async function handToComposer(sessionId: string, files: File[], text: string): Promise<"composer" | "draft"> {
  const request: ComposeRequest = { sessionId, files, text, taken: false };
  window.dispatchEvent(new CustomEvent<ComposeRequest>(COMPOSE_EVENT, { detail: request }));
  if (request.taken) return "composer";
  writeDraft(sessionId, appendLine(readDraft(sessionId), text));
  const saved = await loadDraftFiles(sessionId).catch(() => [] as File[]);
  await saveDraftFiles(sessionId, [...saved, ...files]);
  return "draft";
}
