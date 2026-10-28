// Rich answers: the cards and citations an answer may carry, and the sources a turn read.
//
// The model writes them inside its ordinary markdown, in a form that still reads as text wherever
// nothing renders it (Telegram, an export, a copy):
//
//   :::card                         one item of a ranked list; `:::entity` is a company, a project,
//   title: 1. Pantum P2500NW        a person — the same keys, drawn with a larger logo
//   url: https://…
//   image: https://…/thumb.jpg
//   badge: My pick
//   meta: ≈ 45 000 ₸ · mono laser
//   - any markdown body
//   links: [Kaspi](https://…) · [DNS](https://…)
//   note: a muted last line
//   :::
//
// and a citation is a link whose text starts with a caret, `[^](https://…)` or `[^GitHub](https://…)`,
// put right after the sentence it backs. A key line may stand anywhere in the block: the order the
// model happens to write them in is not something the reader should pay for with a broken card.

export type CardKind = "card" | "entity";

export type Card = {
  kind: CardKind;
  title: string;
  url: string;
  image: string;
  badge: string;
  meta: string;
  links: string;
  note: string;
  body: string;
};

export const CARD_OPEN_RE = /^\s*:::\s*(card|entity)\b\s*(.*)$/i;
export const CARD_CLOSE_RE = /^\s*:::\s*$/;
const FIELD_RE = /^\s*(title|url|image|badge|meta|links|note)\s*:\s?(.*)$/i;
const FENCE_RE = /^\s*```/;

/** The lines between `:::card` and `:::` as a card; `rest` is what followed the opener on its line. */
export function parseCard(kind: string, rest: string, lines: readonly string[]): Card {
  const card: Card = { kind: kind.toLowerCase() === "entity" ? "entity" : "card", title: rest.trim(), url: "", image: "", badge: "", meta: "", links: "", note: "", body: "" };
  const body: string[] = [];
  let fenced = false;
  for (const line of lines) {
    if (FENCE_RE.test(line)) fenced = !fenced;
    const field = fenced ? null : line.match(FIELD_RE);
    if (field) {
      const key = field[1].toLowerCase() as Exclude<keyof Card, "kind" | "body">;
      card[key] = field[2].trim();
    } else body.push(line);
  }
  card.body = body.join("\n").trim();
  return card;
}

/** An address the page may load or link to: http or https and nothing else, so a card cannot carry
 *  a `javascript:` link or reach into the app's own API. */
export function webUrl(raw: string): string {
  const value = raw.trim().replace(/^<|>$/g, "");
  return /^https?:\/\/[^\s"'<>]+$/i.test(value) ? value : "";
}

/** The site a link is on, as a reader names it: `kaspi.kz`, not `www.kaspi.kz`. */
export function siteOf(url: string): string {
  const match = /^https?:\/\/([^/?#:]+)/i.exec(url);
  return match ? match[1].toLowerCase().replace(/^(?:www\d?|m)\./, "") : "";
}

/** One address as a key, so the same page cited with and without `www.` or a trailing slash is one source. */
export function sourceKey(url: string): string {
  const match = /^https?:\/\/([^/?#]+)([^#]*)/i.exec(url.trim());
  if (!match) return url.trim();
  const host = match[1].toLowerCase().replace(/^(?:www\d?|m)\./, "");
  return host + (match[2].replace(/\/+$/, "") || "");
}

export type Citation = { label: string; url: string };

/** A citation: `[^](url)` or `[^label](url)`. */
export const CITE_ONE_SRC = String.raw`\[\^([^\]\n]{0,40})\]\((https?:\/\/[^)\s]+)\)`;
/** Citations side by side — `[^](a) [^](b)` — are one chip with "+1", as the reader expects. */
export const CITE_RUN_RE = new RegExp(`${CITE_ONE_SRC}(?:[ \\t]*${CITE_ONE_SRC})*`, "g");

export function citationsIn(run: string): Citation[] {
  const out: Citation[] = [];
  for (const m of run.matchAll(new RegExp(CITE_ONE_SRC, "g"))) out.push({ label: m[1].trim(), url: m[2] });
  return out;
}

/** Every citation and card address of an answer, in the order they appear, once each. */
export function citedIn(answer: string): Citation[] {
  const seen = new Set<string>();
  const out: Citation[] = [];
  const add = (label: string, url: string) => {
    const key = sourceKey(url);
    if (!webUrl(url) || seen.has(key)) return;
    seen.add(key);
    out.push({ label, url });
  };
  let fenced = false;
  let inCard = false;
  for (const line of answer.split("\n")) {
    if (FENCE_RE.test(line)) fenced = !fenced;
    if (fenced) continue;
    if (CARD_OPEN_RE.test(line)) inCard = true;
    else if (inCard && CARD_CLOSE_RE.test(line)) inCard = false;
    const field = inCard ? line.match(/^\s*url\s*:\s*(\S+)/i) : null;
    if (field) add("", field[1]);
    for (const m of line.matchAll(new RegExp(CITE_ONE_SRC, "g"))) add(m[1].trim(), m[2]);
  }
  return out;
}

// ── what a turn read ────────────────────────────────────────────────────────────────────────

/** One page the agent saw: a search hit, a page it fetched, a page its browser opened. */
export type WebSource = { url: string; title?: string; snippet?: string; image?: string };

/** The tool calls this module reads; a turn's activity holds more, and the rest are passed over. */
export type SourceTool = { kind: string; name?: string; args?: Record<string, unknown>; result?: string; error?: boolean; sources?: WebSource[] };

export type Search = { query: string; count: number };

export type TurnSources = {
  /** What the answer cites, enriched with what the tools said about each page. */
  cited: WebSource[];
  /** What the turn read and the answer does not cite. */
  more: WebSource[];
  searches: Search[];
};

/** The hits of a WebSearch result as the tool writes them: `- title (date)`, then the address and the snippet indented. */
export function searchHits(text: string): WebSource[] {
  const out: WebSource[] = [];
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const head = /^- (.+)$/.exec(lines[i]);
    const url = head ? /^\s{2}(https?:\/\/\S+)\s*$/.exec(lines[i + 1] ?? "") : null;
    if (!head || !url) continue;
    const hit: WebSource = { url: url[1], title: head[1].replace(/\s\(\d{4}-[^)]*\)$/, "").trim() };
    let j = i + 2;
    for (; j < lines.length && /^\s{2}\S/.test(lines[j]); j++) {
      const line = lines[j].trim();
      const image = /^image:\s*(https?:\/\/\S+)$/.exec(line);
      if (image) hit.image = image[1];
      else if (!hit.snippet) hit.snippet = line;
    }
    out.push(hit);
    i = j - 1;
  }
  return out;
}

/** A WebFetch result: `HTTP 200 <url>`, then the page's title and picture when it had them. */
export function fetchedPage(text: string): WebSource | null {
  const head = /^HTTP \d{3} (https?:\/\/\S+)/.exec(text);
  if (!head) return null;
  const page: WebSource = { url: head[1] };
  const title = /^title: (.+)$/m.exec(text.slice(0, 600));
  const image = /^image: (https?:\/\/\S+)$/m.exec(text.slice(0, 600));
  if (title) page.title = title[1].trim();
  if (image) page.image = image[1];
  return page;
}

function seenBy(tool: SourceTool): WebSource[] {
  if (tool.error) return [];
  if (tool.sources?.length) return tool.sources;
  const text = tool.result ?? "";
  if (tool.name === "WebSearch") return searchHits(text);
  if (tool.name === "WebFetch") {
    const page = fetchedPage(text);
    return page ? [page] : [];
  }
  if (tool.name === "BrowserOpen" || tool.name === "BrowserNavigate") {
    const url = webUrl(String(tool.args?.url ?? ""));
    return url ? [{ url }] : [];
  }
  return [];
}

/** The sources of one turn: what its answer cites, what else it read, and the searches it made. */
export function turnSources(answer: string, activity: readonly SourceTool[]): TurnSources {
  const seen = new Map<string, WebSource>();
  const searches: Search[] = [];
  for (const item of activity) {
    if (item.kind !== "tool") continue;
    const found = seenBy(item);
    if (item.name === "WebSearch" && !item.error && item.result !== undefined) {
      const query = String(item.args?.query ?? "").trim();
      if (query) searches.push({ query, count: found.length });
    }
    for (const source of found) {
      const key = sourceKey(source.url);
      const known = seen.get(key);
      // A page met twice keeps what each meeting told: the search's snippet and the fetch's picture.
      seen.set(key, known ? { ...source, ...known, image: known.image || source.image, title: known.title || source.title, snippet: known.snippet || source.snippet } : source);
    }
  }
  const cited: WebSource[] = [];
  const citedKeys = new Set<string>();
  for (const citation of citedIn(answer)) {
    const key = sourceKey(citation.url);
    citedKeys.add(key);
    cited.push({ ...(seen.get(key) ?? {}), url: citation.url, title: seen.get(key)?.title || citation.label || undefined });
  }
  const more = [...seen.entries()].filter(([key]) => !citedKeys.has(key)).map(([, source]) => source);
  return { cited, more, searches };
}

export function sourceCount(sources: TurnSources): number {
  return sources.cited.length + sources.more.length;
}

// What the answers on screen know about each page, for a citation's popover: a chip carries only
// addresses, and the title a search gave belongs to the turn, not to the markup. Written by the
// turns as they render, read when a chip is opened.
const known = new Map<string, WebSource>();
const KNOWN_MAX = 2000;

export function rememberSources(sources: TurnSources): void {
  for (const source of [...sources.cited, ...sources.more]) {
    if (!source.title && !source.snippet) continue;
    known.set(sourceKey(source.url), source);
  }
  while (known.size > KNOWN_MAX) {
    const oldest = known.keys().next();
    if (oldest.done) break;
    known.delete(oldest.value);
  }
}

export function knownSource(url: string): WebSource {
  return known.get(sourceKey(url)) ?? { url };
}
