// The files an operator's message carried, drawn above the bubble the way the chat apps draw them: a
// row of rounded square thumbnails for the pictures, then a card per other file with a coloured type
// icon, the name in bold and the kind under it. The bubble below holds only the operator's words.
// A picture opens in the centred viewer, a file the app can show opens in the preview, and anything
// else is downloaded — the same routes every other file of the conversation takes.

import type { MessageAttachment } from "./api";
import { Icon, type IconName } from "./icons";
import { canPreview, downloadHref, openOrSave, useBlobUrl, type PreviewSource } from "./preview";
import { t } from "./i18n";
import { bytes } from "./format";
import { attachmentKind, attachmentSource, splitAttachments, type AttachmentKind } from "./attachments";

const KIND_ICON: Record<AttachmentKind, IconName> = {
  image: "image", pdf: "file", document: "file", sheet: "board", slides: "chart", archive: "archive",
  audio: "volume", video: "play", code: "braces", text: "file", file: "attach",
};

function described(item: MessageAttachment): string {
  return typeof item.size === "number" ? `${item.name} · ${bytes(item.size)}` : item.name;
}

function Thumb({ item, src, onOpen }: { item: MessageAttachment; src: PreviewSource; onOpen: (src: PreviewSource) => void }) {
  const { url, error } = useBlobUrl(src);
  return (
    <button type="button" className={`msg-thumb ${url ? "ready" : error ? "failed" : "loading"}`} onClick={() => onOpen(src)} aria-label={t("attach.open", { name: item.name })} title={described(item)}>
      {url ? <img src={url} alt={item.name} draggable={false} /> : error ? <Icon name="image" size={22} /> : null}
    </button>
  );
}

function FileCard({ item, src, onOpen }: { item: MessageAttachment; src: PreviewSource & { base: string; path: string }; onOpen: (src: PreviewSource) => void }) {
  const kind = attachmentKind(item);
  const openable = canPreview(src.path);
  const open = () => (openable ? onOpen(src) : openOrSave(downloadHref(src.base, src.path), item.name));
  return (
    <button type="button" className="msg-file" data-kind={kind} onClick={open} aria-label={t(openable ? "attach.open" : "attach.save", { name: item.name })} title={described(item)}>
      <span className="msg-file-icon" aria-hidden><Icon name={KIND_ICON[kind]} size={18} /></span>
      <span className="msg-file-text">
        <span className="msg-file-name">{item.name}</span>
        <span className="msg-file-kind">{t(`attach.kind.${kind}`)}</span>
      </span>
    </button>
  );
}

/** A message's attachments, or nothing when it carried none. */
export function MessageAttachments({ sessionId, items, onOpen }: { sessionId: string; items: readonly MessageAttachment[] | null | undefined; onOpen: (src: PreviewSource) => void }) {
  const { images, files } = splitAttachments(items);
  if (!images.length && !files.length) return null;
  const key = (item: MessageAttachment, index: number) => `${item.kept ?? item.file ?? item.name}:${index}`;
  return (
    <div className="msg-attachments" role="group" aria-label={t("attach.files")}>
      {images.length > 0 && (
        <div className="msg-thumbs">
          {images.map((item, index) => <Thumb key={key(item, index)} item={item} src={attachmentSource(sessionId, item)} onOpen={onOpen} />)}
        </div>
      )}
      {files.map((item, index) => <FileCard key={key(item, index)} item={item} src={attachmentSource(sessionId, item)} onOpen={onOpen} />)}
    </div>
  );
}
