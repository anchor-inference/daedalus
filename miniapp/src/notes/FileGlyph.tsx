// A note file's square: the picture itself for an image, the grey kind glyph for anything else. The
// same square the conversation draws an attached file in (`filetile.tsx`).

import { useEffect, useState } from "react";
import { FileTile } from "../filetile";
import { fileFamily } from "../md";
import { fileUrl } from "./api";
import { isImage, type NoteFile } from "./model";

export function FileGlyph({ file, size = "md" }: { file: NoteFile; size?: "sm" | "md" }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!isImage(file)) return;
    let gone = false;
    fileUrl(file.id).then((u) => !gone && setUrl(u), () => undefined);
    return () => {
      gone = true;
    };
  }, [file.id, file.mime]); // eslint-disable-line react-hooks/exhaustive-deps -- the id names the bytes
  if (isImage(file)) return <FileTile family="image" size={size}>{url && <img src={url} alt="" />}</FileTile>;
  return <FileTile family={fileFamily(file)} size={size} />;
}
