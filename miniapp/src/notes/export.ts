// A note leaving the panel: copied as Markdown, downloaded as a `.md` (or a zip with the files it shows,
// laid out at the very paths its references name), or handed to the composer as an attachment the
// agent reads with the message.

import { handToComposer } from "../annotate/compose";
import { fileBytes } from "./api";
import { archivePath, fileNameOf, isImage, noteMarkdown, references, zip, type Note, type NoteFile } from "./model";

/** Files the composer gets beside the note itself: the pictures it shows, so a model that sees images
 *  sees them too. More than this and the message is mostly attachments. */
const ATTACHED_PICTURES = 6;

export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

export async function copyMarkdown(note: Pick<Note, "title" | "body">): Promise<void> {
  await navigator.clipboard.writeText(noteMarkdown(note));
}

/** The note as a file: a `.md` alone, or a zip of the `.md` and every file it shows. */
export async function downloadNote(note: Pick<Note, "title" | "body">, files: Map<string, NoteFile>, fallbackName: string): Promise<"md" | "zip"> {
  const name = fileNameOf(note.title, fallbackName);
  const markdown = new TextEncoder().encode(noteMarkdown(note));
  const shown = references(note.body).map((id) => files.get(id)).filter((f): f is NoteFile => !!f);
  if (!shown.length) {
    saveBlob(new Blob([markdown], { type: "text/markdown;charset=utf-8" }), `${name}.md`);
    return "md";
  }
  const entries: { name: string; data: Uint8Array }[] = [{ name: `${name}.md`, data: markdown }];
  for (const file of shown) entries.push({ name: archivePath(file), data: await fileBytes(file.id) });
  saveBlob(zip(entries), `${name}.zip`);
  return "zip";
}

/** The note into this chat's composer: the Markdown as a file, and the pictures it shows. */
export async function attachToMessage(sessionId: string, note: Pick<Note, "title" | "body">, files: Map<string, NoteFile>, fallbackName: string): Promise<"composer" | "draft"> {
  const name = fileNameOf(note.title, fallbackName);
  const attached = [new File([noteMarkdown(note)], `${name}.md`, { type: "text/markdown" })];
  const pictures = references(note.body).map((id) => files.get(id)).filter((f): f is NoteFile => !!f && isImage(f)).slice(0, ATTACHED_PICTURES);
  for (const picture of pictures) attached.push(new File([(await fileBytes(picture.id)) as BlobPart], picture.name, { type: picture.mime }));
  return handToComposer(sessionId, attached, "");
}
