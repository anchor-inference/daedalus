// What the side panel decides without drawing anything: which conversation it shows, what a key on
// the approval card means, whether a stop reached the host, and how a page becomes an attachment.

import type { Current } from "./bridge";

export type PanelSession = { id: string; title: string; status: string; last_message_at: string; needs_you: boolean; group: string };
export type PanelListing = { sessions: PanelSession[]; consented: boolean; consent: string; chrome: { connected: boolean; name: string } };

/**
 * The conversation the panel shows. A notification's click wins; then the conversation that works
 * with the active tab's group, unless the operator pinned the panel to one; then whatever it showed
 * already; then the newest one using this Chrome. Null means the new-conversation view.
 */
export function pickSession(input: { show?: string | null; current: Current | null; sessions: PanelSession[]; shown: string | null; pinned: boolean }): string | null {
  const known = new Set(input.sessions.map((s) => s.id));
  if (input.show && known.has(input.show)) return input.show;
  const owner = input.current?.owner?.session;
  if (!input.pinned && owner && known.has(owner)) return owner;
  if (input.shown && known.has(input.shown)) return input.shown;
  return input.sessions[0]?.id ?? null;
}

export type CardKey = "allow" | "similar" | "deny" | null;

/** A key on the approval card, which takes the focus when it appears: Enter allows once,
 *  Ctrl+Enter (Cmd+Enter) allows the like for the conversation, Esc refuses. */
export function cardKey(e: { key: string; ctrlKey?: boolean; metaKey?: boolean; isComposing?: boolean }): CardKey {
  if (e.isComposing) return null;
  if (e.key === "Escape") return "deny";
  if (e.key !== "Enter") return null;
  return e.ctrlKey || e.metaKey ? "similar" : "allow";
}

/** How long a stop has to show in the stream before the panel says it did not get through. */
export const STOP_CONFIRM_MS = 3000;

/** Whether a stop asked at `askedAt` is still unconfirmed at `now`: the session kept running. */
export function stopUnconfirmed(askedAt: number | null, status: string, now: number): boolean {
  return askedAt !== null && now - askedAt >= STOP_CONFIRM_MS && (status === "running" || status === "compacting");
}

/** Whether the activity line carries a Stop of its own. The composer's round button is Stop while a run
 *  is on, and a second red Stop beside it made the panel's foot a block of two; the line keeps one only
 *  while the history is being compacted, when the composer's button is Send. */
export function lineStops(status: string): boolean {
  return status === "compacting";
}

/** The page's text as a file the conversation receives, headed by where it came from. */
export function pageFile(page: { url: string; title: string; text: string }): File {
  const head = [page.title, page.url].filter(Boolean).join("\n");
  return new File([`${head}\n\n${page.text}`], "page.txt", { type: "text/plain" });
}

/** The words the operator selected, quoted, with the page they came from. */
export function selectionQuote(page: { url: string; text: string }): string {
  const lines = page.text.trim().split(/\r?\n/).map((line) => `> ${line}`);
  return `${lines.join("\n")}\n${page.url ? `— ${page.url}\n` : ""}\n`;
}

export type Rect = { x: number; y: number; w: number; h: number };

/** A dragged rectangle in the shown picture's pixels, as the source image's pixels, kept inside it. */
export function cropRect(drag: Rect, shown: { w: number; h: number }, natural: { w: number; h: number }): Rect {
  const sx = natural.w / Math.max(1, shown.w);
  const sy = natural.h / Math.max(1, shown.h);
  const x = Math.max(0, Math.min(drag.x, drag.x + drag.w));
  const y = Math.max(0, Math.min(drag.y, drag.y + drag.h));
  const w = Math.min(Math.abs(drag.w), shown.w - x);
  const h = Math.min(Math.abs(drag.h), shown.h - y);
  return { x: Math.round(x * sx), y: Math.round(y * sy), w: Math.max(1, Math.round(w * sx)), h: Math.max(1, Math.round(h * sy)) };
}

/** A data URL as a file. */
export function dataFile(dataUrl: string, name: string): File {
  const [head, body] = dataUrl.split(",", 2);
  const type = /data:([^;]+)/.exec(head)?.[1] ?? "image/png";
  const bytes = Uint8Array.from(atob(body ?? ""), (c) => c.charCodeAt(0));
  return new File([bytes], name, { type });
}
