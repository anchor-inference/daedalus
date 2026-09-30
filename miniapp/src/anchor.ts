// A link to one message of a conversation is its address with `#m<seq>` after it: the "copy link"
// of a turn writes one, a search hit opens one, and a notification may carry one. These are the pure
// parts of opening such a link, kept apart from the screen so they can be read back without a browser.

/** The message a location's hash points at, or null when it points at none. */
export function parseAnchor(hash: string | null | undefined): number | null {
  const m = /^#?m(\d{1,15})$/.exec(hash ?? "");
  return m ? Number(m[1]) : null;
}

/** The hash that points at a message. */
export function anchorHash(seq: number): string {
  return `#m${seq}`;
}

/**
 * Which turn of the list holds a message, by the turns' keys.
 *
 * A turn's key is a letter and the seq of the message that opened it (`u120`, `a121`, `s300`), and
 * the turns are in the order of those seqs, so a message belongs to the last turn that opened at or
 * before it. That is what makes a hit on a tool's output or on a step's note land on the turn that
 * shows it, although no element carries that message's own seq. A message before every turn (the
 * tool results at the very start of a page) lands on the first one; an empty list gives -1.
 */
export function turnFor(keys: readonly string[], seq: number): number {
  let found = -1;
  for (let i = 0; i < keys.length; i++) {
    const m = /^[a-z](\d+)$/.exec(keys[i]);
    if (!m) continue;
    if (Number(m[1]) > seq) break;
    found = i;
  }
  return found < 0 && keys.length ? 0 : found;
}

/** A search snippet in its pieces: the host marks every matching word by putting it in [brackets]. */
export function snippetParts(snippet: string): { text: string; hit: boolean }[] {
  const parts: { text: string; hit: boolean }[] = [];
  const re = /\[([^\]]+)\]/g;
  let at = 0;
  for (let m = re.exec(snippet); m; m = re.exec(snippet)) {
    if (m.index > at) parts.push({ text: snippet.slice(at, m.index), hit: false });
    parts.push({ text: m[1], hit: true });
    at = m.index + m[0].length;
  }
  if (at < snippet.length) parts.push({ text: snippet.slice(at), hit: false });
  return parts;
}

/**
 * A fresh read of a conversation's newest page, with the older pages the screen already holds kept in
 * front of it.
 *
 * The whole conversation is read again whenever the event stream reconnects — a server restart, a
 * proxy's timeout, a phone waking up — and that read is only the newest page. Taken as it came, it
 * threw away every older page the reader had scrolled or been linked back to, and the list jumped
 * from the message they were reading to hundreds of turns later. What is kept is only what the new
 * page does not reach, and only when the two meet: a page that starts past the end of what is held
 * says nothing about what lies between, so the old pages are dropped then rather than joined across
 * a hole.
 */
export function withOlder<M extends { seq?: number | null; live?: boolean }>(known: readonly M[], page: M[]): M[] {
  const first = page[0]?.seq;
  if (first == null) return page;
  const settled = known.filter((m) => !m.live && m.seq != null);
  if (!settled.length || settled[0].seq! >= first) return page;
  if (first > settled[settled.length - 1].seq! + 1) return page;
  const before = settled.filter((m) => m.seq! < first);
  return before.length ? [...before, ...page] : page;
}
