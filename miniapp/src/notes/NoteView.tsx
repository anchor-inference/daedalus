// A note drawn for reading: the answer renderer in note mode (md.ts `renderNote`), with the controls
// only a note has wired here once for the whole drawing — a task box ticks its own line, a picture
// opens the viewer, a file chip opens or downloads the file, a heading's # copies a link to it, and a
// link to `#heading` scrolls there.

import { memo, useCallback, useMemo, useRef } from "react";
import { renderNote } from "../md";
import { RawHtml } from "../rawhtml";
import { hydrateRich } from "../sources";
import { fileUrl } from "./api";
import { isImage, type NoteFile } from "./model";

export type NoteViewProps = {
  body: string;
  files: Map<string, NoteFile>;
  className?: string;
  /** A task's box was pressed: the source line it stands on. Absent, the boxes are drawn but inert. */
  onToggleTask?: (line: number) => void;
  /** A picture was pressed: every picture of the note in order, and the one pressed. */
  onImage?: (images: NoteFile[], index: number) => void;
  onFile?: (file: NoteFile) => void;
  /** The # beside a heading was pressed. */
  onHeading?: (slug: string) => void;
};

/** Fill the pictures of markup just written: each one's bytes come once per file (api.ts fileUrl). */
export function hydrateNote(root: HTMLElement): void {
  hydrateRich(root);
  root.querySelectorAll<HTMLImageElement>("img[data-note-src]:not([src])").forEach((img) => {
    const id = img.dataset.noteSrc as string;
    fileUrl(id).then(
      (url) => {
        if (!img.isConnected) return;
        img.onload = () => img.closest(".note-img")?.classList.add("loaded");
        img.src = url;
      },
      () => img.closest(".note-img")?.classList.add("failed"),
    );
  });
}

export const NoteView = memo(function NoteView({ body, files, className, onToggleTask, onImage, onFile, onHeading }: NoteViewProps) {
  const html = useMemo(() => renderNote(body, { file: (id) => files.get(id) ?? null }), [body, files]);
  const root = useRef<HTMLDivElement>(null);
  const onClick = useCallback((e: React.MouseEvent) => {
    const target = e.target as HTMLElement;
    const box = target.closest<HTMLElement>("[data-task-line]");
    if (box) {
      e.preventDefault();
      if (onToggleTask) {
        // The tick shows at once; the drawing that follows the save agrees with it.
        box.classList.toggle("on");
        box.setAttribute("aria-checked", box.classList.contains("on") ? "true" : "false");
        box.textContent = box.classList.contains("on") ? "✓" : "";
        onToggleTask(Number(box.dataset.taskLine));
      }
      return;
    }
    const picture = target.closest<HTMLElement>("[data-note-image]");
    if (picture && onImage) {
      e.preventDefault();
      const ids = Array.from(root.current?.querySelectorAll<HTMLElement>("[data-note-image]") ?? []).map((el) => el.dataset.noteImage as string);
      const images = ids.map((id) => files.get(id)).filter((f): f is NoteFile => !!f && isImage(f));
      const at = images.findIndex((f) => f.id === picture.dataset.noteImage);
      onImage(images, Math.max(0, at));
      return;
    }
    const chip = target.closest<HTMLElement>("[data-note-file]");
    if (chip && onFile) {
      e.preventDefault();
      const file = files.get(chip.dataset.noteFile as string);
      if (file) onFile(file);
      return;
    }
    const heading = target.closest<HTMLElement>("[data-note-heading]");
    if (heading) {
      e.preventDefault();
      onHeading?.(heading.dataset.noteHeading as string);
      return;
    }
    const anchor = target.closest<HTMLAnchorElement>("a[data-note-anchor]");
    if (anchor) {
      e.preventDefault();
      scrollToHeading(root.current, anchor.dataset.noteAnchor as string);
    }
  }, [files, onToggleTask, onImage, onFile, onHeading]);
  return (
    <div ref={root} className={`answer note-view ${className ?? ""}`} onClick={onClick}>
      <RawHtml html={html} onWritten={hydrateNote} />
    </div>
  );
});

/** Bring a heading of a drawn note into view, by its slug; ids are looked up inside the note only,
 *  since two open notes may well have a heading with the same words. */
export function scrollToHeading(root: HTMLElement | null, slug: string): boolean {
  const heading = root?.querySelector<HTMLElement>(`[id="${CSS.escape(slug)}"]`);
  if (!heading) return false;
  heading.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  heading.classList.remove("flash");
  void heading.offsetWidth;
  heading.classList.add("flash");
  return true;
}
