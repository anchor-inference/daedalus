// The square a file is drawn in wherever a file is listed: a message's attachment, a note's file, a
// reference inside a note, a row of the preview's tree. It is grey with a glyph for the kind, never a
// colour per kind: a row of coloured squares made every list of files read as a legend, and the
// colours (a red PDF, a green sheet) carried a meaning none of the app's accents has.

import type { ReactNode } from "react";
import { Icon, type IconName } from "./icons";

/** The families `fileFamily` (notes) and `attachmentKind` (messages) name between them. */
const GLYPH: Record<string, IconName> = {
  image: "image", pdf: "file", document: "file", sheet: "table", slides: "chart", archive: "archive",
  audio: "volume", video: "play", code: "braces", text: "file", file: "file", other: "file",
};

export function fileTileIcon(family: string): IconName {
  return GLYPH[family] ?? "file";
}

/** `sm` 24 px, the default 32, `lg` 48 (an empty list), `fill` the width of its cell (a grid of
    files). A picture, when there is one, takes the whole square. */
export function FileTile({ family, size = "md", children }: { family: string; size?: "sm" | "md" | "lg" | "fill"; children?: ReactNode }) {
  return (
    <span className={`file-tile ${size}`} data-family={family} aria-hidden="true">
      {children ?? <Icon name={fileTileIcon(family)} size={size === "sm" ? 14 : 16} />}
    </span>
  );
}
