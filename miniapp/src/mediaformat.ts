import type { MediaPresentation } from "./api";

export const MEDIA_REF = /!\[(?<alt>[^\]\n]{0,500})\]\(daedalus-media:(?<id>[0-9a-f-]{36})\)/g;

export type AnswerPart = { kind: "text"; text: string } | { kind: "media"; presentation: MediaPresentation };

/**
 * The text with its references read as their alt words, for wherever a file is not drawn: an answer
 * still streaming, or a line the host did not recognise. A reference left in was printed as raw
 * Markdown, ``![…](daedalus-media:…)``, which means nothing to the reader.
 */
export function withoutMediaRefs(text: string): string {
  return text.replace(MEDIA_REF, (_, alt: string) => alt);
}

export function splitMediaAnswer(text: string, presentations: MediaPresentation[]): AnswerPart[] {
  const byId = new Map(presentations.map((item) => [item.id, item]));
  const parts: AnswerPart[] = [];
  let at = 0;
  for (const match of text.matchAll(MEDIA_REF)) {
    const presentation = byId.get(match.groups?.id ?? "");
    if (!presentation || match.index === undefined) continue;
    const before = withoutMediaRefs(text.slice(at, match.index)).trim();
    if (before) parts.push({ kind: "text", text: before });
    parts.push({ kind: "media", presentation });
    at = match.index + match[0].length;
  }
  const after = withoutMediaRefs(text.slice(at)).trim();
  if (after) parts.push({ kind: "text", text: after });
  return parts.length ? parts : [{ kind: "text", text: withoutMediaRefs(text) }];
}

export function mediaCopyText(text: string, presentations: MediaPresentation[]): string {
  const labels = new Map(presentations.map((p) => [p.id, p.items.map((i) => i.caption || i.alt || i.filename).join(", ")]));
  return text.replace(MEDIA_REF, (_, _alt: string, id: string) => labels.get(id) ? `[${labels.get(id)}]` : "").replace(/\n{3,}/g, "\n\n").trim();
}
