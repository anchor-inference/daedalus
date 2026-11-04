// Replies to a passage of an agent's answer: the operator selects words in an answer, writes what they
// want to say about them, and the next message carries each passage as a quote with the reply under
// it. Nothing in here touches the screen: the wire form, its parse back for the bubble, and the
// replies waiting in a chat's composer, kept per chat the way the draft is.
//
// The wire form is plain markdown with one HTML comment per reply:
//
//     <!-- reply 1 msg=42 -->
//     > the quoted line
//     > the next quoted line
//
//     what the operator says about it
//
// The model reads a quote and a paragraph, which is what a person would have pasted by hand; the
// comment numbers the replies and names the answer they came from, so the app can draw each one as a
// quote that leads back to its passage. The host stores the words as they are and needs no field of
// its own for them.

import { useSyncExternalStore } from "react";

/** One reply waiting in the composer: the passage, what it came from, and what is said about it. */
export type QuoteReply = { id: string; quote: string; text: string; seq: number | null };

/** One reply read back out of a sent message. */
export type SentReply = { quote: string; text: string; seq: number | null };

const MARKER = /^<!-- reply (\d+)(?: msg=(\d+))? -->$/;

/**
 * The quote as it will be sent: the selection's own line breaks, without the empty lines a browser adds
 * between blocks in runs, and without the spaces a rendered table or list leaves at the line ends.
 */
export function cleanQuote(selected: string): string {
  return selected
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map((line) => line.replace(/[ \t ]+$/, ""))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/**
 * The reply text as one paragraph. A blank line is what ends a reply in the wire form, so one inside
 * the text would hand the rest of it to the next reply or to the message; it becomes a plain line break.
 */
export function cleanReplyText(text: string): string {
  return text.replace(/\r\n?/g, "\n").split("\n").map((line) => line.trim()).filter((line) => line !== "").join("\n");
}

/** The message the agent receives: every reply in the order it was added, then the composer's words. */
export function serialiseReplies(replies: readonly Pick<QuoteReply, "quote" | "text" | "seq">[], text: string): string {
  const blocks = replies
    .filter((reply) => cleanQuote(reply.quote) !== "" && cleanReplyText(reply.text) !== "")
    .map((reply, index) => {
      const quoted = cleanQuote(reply.quote).split("\n").map((line) => (line ? `> ${line}` : ">")).join("\n");
      const ref = reply.seq != null ? ` msg=${reply.seq}` : "";
      return `<!-- reply ${index + 1}${ref} -->\n${quoted}\n\n${cleanReplyText(reply.text)}\n\n`;
    });
  return `${blocks.join("")}${text.trim()}`.trim();
}

/**
 * A sent message taken apart again: the replies it opens with and the words after them. A message that
 * does not open with a reply marker is returned whole, so a quote the operator typed by hand, or a
 * comment that only looks like one further down, is never read as a reply.
 */
export function parseReplies(message: string): { replies: SentReply[]; rest: string } {
  const lines = message.replace(/\r\n?/g, "\n").split("\n");
  const replies: SentReply[] = [];
  let at = 0;
  for (;;) {
    while (at < lines.length && lines[at].trim() === "" && replies.length > 0) at++;
    const marker = at < lines.length ? MARKER.exec(lines[at].trim()) : null;
    if (!marker) break;
    let cursor = at + 1;
    const quoted: string[] = [];
    while (cursor < lines.length && lines[cursor].startsWith(">")) {
      quoted.push(lines[cursor].replace(/^> ?/, ""));
      cursor++;
    }
    while (cursor < lines.length && lines[cursor].trim() === "") cursor++;
    const said: string[] = [];
    while (cursor < lines.length && lines[cursor].trim() !== "" && !MARKER.test(lines[cursor].trim())) {
      said.push(lines[cursor]);
      cursor++;
    }
    if (!quoted.length) break;
    replies.push({ quote: quoted.join("\n"), text: said.join("\n"), seq: marker[2] ? Number(marker[2]) : null });
    at = cursor;
  }
  if (!replies.length) return { replies, rest: message };
  return { replies, rest: lines.slice(at).join("\n").trim() };
}

/** A message's words with each reply reduced to its quote's first line, for a one-line preview. */
export function replyPreview(message: string): string {
  const { replies, rest } = parseReplies(message);
  if (!replies.length) return message;
  return [...replies.map((reply) => `«${reply.quote.split("\n")[0]}» ${reply.text}`), rest].filter(Boolean).join(" · ");
}

// ── the replies waiting in a chat's composer ─────────────────────────────────────────────────

const PREFIX = "daedalus.replies.";

type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function safeStorage(): StorageLike | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function repliesKey(sessionId: string): string {
  return `${PREFIX}${sessionId}`;
}

/** What storage holds for a chat, guarded: a partial old write or a hand edit reads as nothing. */
export function readReplies(sessionId: string, storage: StorageLike | null = safeStorage()): QuoteReply[] {
  try {
    const raw = JSON.parse(storage?.getItem(repliesKey(sessionId)) ?? "[]") as unknown;
    if (!Array.isArray(raw)) return [];
    return raw.filter((item): item is QuoteReply => !!item && typeof item === "object"
      && typeof (item as QuoteReply).id === "string" && typeof (item as QuoteReply).quote === "string" && typeof (item as QuoteReply).text === "string"
      && ((item as QuoteReply).seq === null || typeof (item as QuoteReply).seq === "number"));
  } catch {
    return [];
  }
}

/** None left is no key at all, as an empty draft is. */
export function writeReplies(sessionId: string, replies: readonly QuoteReply[], storage: StorageLike | null = safeStorage()): void {
  try {
    if (replies.length) storage?.setItem(repliesKey(sessionId), JSON.stringify(replies));
    else storage?.removeItem(repliesKey(sessionId));
  } catch {
    /* private mode, full storage: the replies live as long as the page */
  }
}

// Outside React state, as the orchestrator's reply chip is: the pill that adds a reply sits over the
// conversation and the cards inside the composer, and neither should re-render the other's tree. The
// list per chat is read from storage once and kept, so the snapshot keeps its identity between reads.
const cache = new Map<string, QuoteReply[]>();
const listeners = new Set<() => void>();

function notify(): void {
  for (const listener of [...listeners]) listener();
}

export function quoteReplies(sessionId: string): QuoteReply[] {
  let list = cache.get(sessionId);
  if (!list) {
    list = readReplies(sessionId);
    cache.set(sessionId, list);
  }
  return list;
}

function setQuoteReplies(sessionId: string, next: QuoteReply[]): void {
  cache.set(sessionId, next);
  writeReplies(sessionId, next);
  notify();
}

export function addQuoteReply(sessionId: string, reply: Omit<QuoteReply, "id">): QuoteReply {
  const made: QuoteReply = { ...reply, quote: cleanQuote(reply.quote), text: cleanReplyText(reply.text), id: crypto.randomUUID() };
  setQuoteReplies(sessionId, [...quoteReplies(sessionId), made]);
  return made;
}

export function updateQuoteReply(sessionId: string, id: string, text: string): void {
  setQuoteReplies(sessionId, quoteReplies(sessionId).map((reply) => (reply.id === id ? { ...reply, text: cleanReplyText(text) } : reply)));
}

export function removeQuoteReply(sessionId: string, id: string): void {
  setQuoteReplies(sessionId, quoteReplies(sessionId).filter((reply) => reply.id !== id));
}

export function clearQuoteReplies(sessionId: string): void {
  if (quoteReplies(sessionId).length) setQuoteReplies(sessionId, []);
}

/** Forget what was read, so the next read is storage's: a test, or another tab's write. */
export function forgetQuoteReplies(): void {
  cache.clear();
  notify();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  // Another tab of the app adding or sending a reply: its write is the truth for every tab.
  const fromOtherTab = (event: StorageEvent) => {
    if (event.key?.startsWith(PREFIX)) {
      cache.delete(event.key.slice(PREFIX.length));
      listener();
    }
  };
  window.addEventListener("storage", fromOtherTab);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", fromOtherTab);
  };
}

export function useQuoteReplies(sessionId: string): QuoteReply[] {
  return useSyncExternalStore(subscribe, () => quoteReplies(sessionId));
}
