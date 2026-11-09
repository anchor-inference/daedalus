// The pencil on a picture waiting in a composer: it opens the markup editor on that picture, and Done
// puts the marked-up PNG in its place. Every picture gets it however it arrived (pasted, dropped, picked,
// photographed or handed over by a snapshot), and none is opened in the editor uninvited: a screenshot
// pasted only to be sent goes as it is, with no step in between.
//
// Marking up a second time starts from the original, with the earlier marks back as steps that Undo
// takes away one by one, rather than from the flattened copy, where an old arrow could only be drawn
// over. The original and the steps are held beside the marked-up file for as long as the page lives;
// the saved draft keeps only the files, so after a reload the editor starts from the marked-up copy.

import { useState } from "react";
import { Icon } from "../icons";
import { t } from "../i18n";
import { toast } from "../ui/dialogs";
import { Annotate } from "./Annotate";
import { fitPixels, type Op } from "./ops";
import { imageFrom, type Snapshot } from "./source";

/** A marked-up file's original and the steps that made it, keyed by the file itself. */
const origins = new WeakMap<File, { original: File; ops: Op[] }>();

/** The kinds of picture a canvas can read. An SVG is a drawing, not a picture, and drawn to a canvas it loses its text. */
const RASTER = /^image\/(png|jpe?g|webp|gif|bmp|avif)$/i;
const RASTER_NAME = /\.(png|jpe?g|webp|gif|bmp|avif)$/i;

/** Whether the pencil is offered: a picture a canvas can read, in a browser that can make a PNG of a canvas. */
export function markable(file: File): boolean {
  if (typeof HTMLCanvasElement === "undefined" || !("toBlob" in HTMLCanvasElement.prototype)) return false;
  return RASTER.test(file.type) || (!file.type && RASTER_NAME.test(file.name));
}

/** The marked-up file's name: the original's, as a PNG, since that is what the editor makes. */
export function pngName(name: string): string {
  const stem = name.replace(/\.[^./\\]+$/, "");
  return `${stem || "picture"}.png`;
}

type Picture = { image: CanvasImageSource; width: number; height: number; release: () => void };

/**
 * A file decoded for the editor, no larger than a canvas may safely be. Decoded as a bitmap where the
 * browser can, which turns a phone photo upright by its orientation tag and can be freed the moment
 * the editor closes, instead of waiting for an image element to be collected.
 */
async function pictureOf(file: File): Promise<Picture | null> {
  if (typeof createImageBitmap !== "function") {
    const url = URL.createObjectURL(file);
    const img = await imageFrom(url);
    if (!img) {
      URL.revokeObjectURL(url);
      return null;
    }
    const size = fitPixels(img.naturalWidth, img.naturalHeight);
    return { image: img, ...size, release: () => URL.revokeObjectURL(url) };
  }
  let bitmap: ImageBitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    return null;
  }
  const size = fitPixels(bitmap.width, bitmap.height);
  if (size.width !== bitmap.width) {
    try {
      const smaller = await createImageBitmap(bitmap, { resizeWidth: size.width, resizeHeight: size.height, resizeQuality: "high" });
      bitmap.close();
      bitmap = smaller;
    } catch {
      // The canvas draws the full one smaller instead; slower to open, the same result.
    }
  }
  const held = bitmap;
  return { image: held, ...size, release: () => held.close() };
}

type Open = { snapshot: Snapshot; original: File; ops: Op[]; release: () => void };

export function MarkupButton({ file, onReplace }: { file: File; onReplace: (file: File) => void }) {
  const [open, setOpen] = useState<Open | null>(null);
  const [opening, setOpening] = useState(false);
  const close = () => {
    open?.release();
    setOpen(null);
  };
  const press = async () => {
    if (opening) return;
    const origin = origins.get(file);
    const original = origin?.original ?? file;
    setOpening(true);
    try {
      const picture = await pictureOf(original);
      if (!picture) {
        toast(t("annotate.unreadable"));
        return;
      }
      setOpen({
        snapshot: { image: picture.image, width: picture.width, height: picture.height, where: file.name, text: "" },
        original, ops: origin?.ops ?? [], release: picture.release,
      });
    } finally {
      setOpening(false);
    }
  };
  return (
    <>
      <button type="button" className="attachment-markup" onClick={() => void press()} disabled={opening} aria-label={t("annotate.markup")} title={t("annotate.markup")}>
        <Icon name="pen" size={12} />
      </button>
      {open && (
        <Annotate snapshot={open.snapshot} initial={open.ops} finish="done" title={t("annotate.markup.title")} name={pngName(file.name)}
          onCancel={close}
          // Every earlier mark undone: what is left is the original itself, not a re-encoded copy of it.
          onUnchanged={() => {
            if (open.original !== file) onReplace(open.original);
            close();
          }}
          onAttach={(marked, ops) => {
            origins.set(marked, { original: open.original, ops });
            onReplace(marked);
            close();
          }} />
      )}
    </>
  );
}
