// Where a snapshot's picture comes from. The Browser tab copies the frame its live view last drew; an
// image in the Preview tab is the image; an HTML page in the Preview tab has no picture the app can
// take, because it runs in a sandboxed frame of an opaque origin that no canvas may read.

import { t } from "../i18n";

/** The picture under the marks, its natural size, and the address the composer is told about. */
export type Snapshot = {
  image: CanvasImageSource | null;
  width: number;
  height: number;
  /** The address or the workspace path the snapshot is of: shown in the overlay and sent as text. */
  where: string;
  /** The text that goes to the composer with the file. */
  text: string;
  /** Said over a blank sheet: why there is no picture under it. */
  note?: string;
};

/** A picture from a blob URL, decoded, or null when it will not decode. */
export async function imageFrom(url: string): Promise<HTMLImageElement | null> {
  const img = new Image();
  img.src = url;
  try {
    await img.decode();
    return img.naturalWidth > 0 ? img : null;
  } catch {
    return null;
  }
}

/**
 * A picture of an HTML file the Preview tab shows, taken by the host's browser. No route for it exists
 * yet, so this answers null and the overlay offers a blank sheet the size of the frame; once the host
 * can render a workspace file to a PNG, this is the one place that asks it.
 */
export async function capturePage(_base: string, _path: string, _width: number, _height: number): Promise<HTMLImageElement | null> {
  return null;
}

/** A blank sheet's size: the frame's, at the device's pixel density up to twice, so text drawn on it stays sharp. */
export function sheetSize(width: number, height: number, density: number): { width: number; height: number } {
  const scale = Math.min(2, Math.max(1, density || 1));
  return { width: Math.max(320, Math.round(width * scale)), height: Math.max(200, Math.round(height * scale)) };
}

/** The browser's live frame as a snapshot of the page at `url`, or null before the first frame. */
export function liveSnapshot(live: { still: () => HTMLCanvasElement | null } | null, url: string, page: (where: string) => string): Snapshot | null {
  const image = live?.still() ?? null;
  if (!image) return null;
  return { image, width: image.width, height: image.height, where: url, text: url ? page(url) : "" };
}

/**
 * The Preview tab's file as a snapshot: an image is its own picture; an HTML page is the host's picture
 * of it when there is one, and otherwise a blank sheet the size of the frame it is shown in, so the
 * operator can still sketch over it and send the path with the sketch.
 */
export async function previewSnapshot(file: { kind: string; url: string | null; base: string; path: string; frame: { width: number; height: number } | null }): Promise<Snapshot | null> {
  if (file.kind === "image") {
    const image = file.url ? await imageFrom(file.url) : null;
    return image ? { image, width: image.naturalWidth, height: image.naturalHeight, where: file.path, text: t("annotate.file", { where: file.path }) } : null;
  }
  if (file.kind !== "html") return null;
  const box = file.frame ?? { width: 800, height: 600 };
  const size = sheetSize(box.width, box.height, window.devicePixelRatio);
  const shot = await capturePage(file.base, file.path, size.width, size.height);
  const text = t("annotate.page", { where: file.path });
  if (shot) return { image: shot, width: shot.naturalWidth, height: shot.naturalHeight, where: file.path, text };
  return { image: null, ...size, where: file.path, text, note: t("annotate.blank") };
}
