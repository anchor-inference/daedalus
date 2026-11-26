// The files of one scope, beside its notes: what any of its notes can show. A file is added by the
// button or by dropping it on the panel; each one can be opened, its reference copied to paste into a
// note, downloaded, or removed — after a warning naming the notes that still show it.

import { FileTile } from "../filetile";
import { plural, t } from "../i18n";
import { bytes } from "../format";
import { downloadHref } from "../preview";
import { OverflowMenu } from "../ui/dialogs";
import { Button } from "../ui/primitives";
import { FileGlyph } from "./FileGlyph";
import { fileBase, isImage, type NoteFile } from "./model";

export type ShelfProps = {
  files: NoteFile[];
  uploading: { key: number; name: string; progress: number }[];
  onAdd: () => void;
  onOpen: (file: NoteFile) => void;
  onCopy: (file: NoteFile) => void;
  onRemove: (file: NoteFile) => void;
};

function download(file: NoteFile): void {
  const a = document.createElement("a");
  a.href = downloadHref(fileBase(file.id), file.name);
  a.download = file.name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

export function FilesShelf({ files, uploading, onAdd, onOpen, onCopy, onRemove }: ShelfProps) {
  return (
    <section className="notes-shelf" aria-label={t("notes.files")}>
      <div className="notes-shelf-head">
        <span>{plural("notes.files.count", files.length)}</span>
        <Button size="sm" icon="plus" onClick={onAdd}>{t("notes.files.add")}</Button>
      </div>
      {files.length === 0 && uploading.length === 0 && <p className="notes-shelf-empty">{t("notes.files.empty")}</p>}
      <div className="notes-shelf-list">
        {uploading.map((u) => (
          <div key={`u${u.key}`} className="shelf-tile uploading" title={u.name}>
            <FileTile family="file"><i className="ne-spin" aria-hidden="true" /></FileTile>
            <span className="shelf-text">
              <span className="shelf-name">{u.name}</span>
              <span className="shelf-bar"><i style={{ transform: `scaleX(${Math.max(0.04, u.progress)})` }} /></span>
            </span>
          </div>
        ))}
        {files.map((file) => (
          <div key={file.id} className={`shelf-tile ${isImage(file) ? "pic" : ""}`}>
            <button type="button" className="shelf-open" onClick={() => onOpen(file)} title={file.name} aria-label={t("notes.files.open", { name: file.name })}>
              <FileGlyph file={file} />
              <span className="shelf-text">
                <span className="shelf-name">{file.name}</span>
                <span className="shelf-size">{bytes(file.size)}</span>
              </span>
            </button>
            <OverflowMenu small label={t("notes.more")} items={[
              { label: t("notes.files.copyref"), icon: "copy", onSelect: () => onCopy(file) },
              { label: t("notes.files.download"), icon: "download", onSelect: () => download(file) },
              "-",
              { label: t("notes.files.remove"), icon: "trash", danger: true, onSelect: () => onRemove(file) },
            ]} />
          </div>
        ))}
      </div>
    </section>
  );
}
