// The files an operator's message carried, as the chat draws them above the bubble: a picture as a
// thumbnail, anything else as a card with a coloured type icon, its name and what kind of file it is.
// The host says what the files are (`MessageView.attachments`); this decides how each one looks and
// where its bytes are fetched from.

import type { MessageAttachment } from "./api";
import { keptBase } from "./keptfiles";
import { previewKind, sessionBase } from "./preview";

export type AttachmentKind = "image" | "pdf" | "document" | "sheet" | "slides" | "archive" | "audio" | "video" | "code" | "text" | "file";

const DOCUMENT_RE = /\.(docx?|odt|rtf|pages)$/i;
const SHEET_RE = /\.(xlsx|xlsm|xls|ods|csv|tsv|numbers)$/i;
const SLIDES_RE = /\.(pptx?|odp|key)$/i;
const ARCHIVE_RE = /\.(zip|rar|7z|tar|gz|tgz|bz2|xz|zst)$/i;
const CODE_RE = /\.(py|ts|tsx|js|jsx|mjs|cjs|sh|bash|zsh|go|rs|java|kt|c|h|cpp|hpp|cs|rb|php|swift|sql|css|scss|less|html?|json|jsonl|ya?ml|toml|xml|ipynb|diff|patch)$/i;

/** What the operator would call the file. The type the browser sent comes first, the name second: a
 *  pasted screenshot is named whatever the clipboard called it, and an unknown type says nothing. */
export function attachmentKind(item: Pick<MessageAttachment, "name" | "mime">): AttachmentKind {
  const name = item.name;
  const mime = (item.mime ?? "").toLowerCase();
  if (mime.startsWith("image/") || previewKind(name) === "image") return "image";
  if (mime === "application/pdf" || /\.pdf$/i.test(name)) return "pdf";
  if (DOCUMENT_RE.test(name) || mime.includes("wordprocessing") || mime === "application/msword") return "document";
  if (SHEET_RE.test(name) || mime.includes("spreadsheet") || mime === "text/csv") return "sheet";
  if (SLIDES_RE.test(name) || mime.includes("presentation")) return "slides";
  if (ARCHIVE_RE.test(name) || /zip|compressed|x-tar|x-7z|x-rar/.test(mime)) return "archive";
  if (mime.startsWith("audio/") || previewKind(name) === "audio") return "audio";
  if (mime.startsWith("video/") || previewKind(name) === "video") return "video";
  if (CODE_RE.test(name)) return "code";
  if (mime.startsWith("text/") || /\.(txt|md|markdown|log|rst)$/i.test(name)) return "text";
  return "file";
}

/** Where a file's bytes are served, in the shape the preview, the viewer and a download link take: a
 *  kept file by its id, an inbox file by its name there under the session's attachment route. The
 *  type rides along, so a picture named without an extension still opens as a picture. */
export function attachmentSource(sessionId: string, item: MessageAttachment): { base: string; path: string; mime?: string } {
  const mime = item.mime ? { mime: item.mime } : {};
  if (item.kept) return { base: keptBase(item.kept), path: item.name, ...mime };
  return { base: `${sessionBase(sessionId)}/attachments`, path: item.file || item.name, ...mime };
}

/** The message's pictures first, in the order sent, then its other files: the row of thumbnails sits above the cards. */
export function splitAttachments(items: readonly MessageAttachment[] | null | undefined): { images: MessageAttachment[]; files: MessageAttachment[] } {
  const images: MessageAttachment[] = [];
  const files: MessageAttachment[] = [];
  for (const item of items ?? []) (attachmentKind(item) === "image" ? images : files).push(item);
  return { images, files };
}
