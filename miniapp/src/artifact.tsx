// The files a turn produced, under the answer. What the agent handed to the operator (SendFile) is
// a card: a glyph, the name, what it is and how big, open it in the panel or download it; an image
// shows itself. Three cards at most before "+N more". What the turn merely wrote or edited is one
// compact line — "Changed 23 files" — that opens into a list of paths relative to the workspace, the
// way the chat apps show changed files; a card per written file was a wall the answer drowned in.

import { useState } from "react";
import { Icon, IconName } from "./icons";
import { AuthImg, PreviewSource, canPreview, downloadHref, previewKind, sessionBase } from "./preview";
import type { Artifact } from "./turns";
import { plural, t } from "./i18n";
import { bytes } from "./format";
import { useQuery } from "./store";
import { filesKey, handleIds, keptBase, type KeptFile } from "./keptfiles";
import { folderBase } from "./folders";
import { useMedia } from "./shell";
import { Chevron } from "./ui/components";
import { BottomSheet, ListRow } from "./ui/phone";
import type { MenuItem } from "./ui/dialogs";
import { useReveal } from "./reveal";
import { SENT_VISIBLE, type ChangedFile, type SentFile } from "./turnfiles";

/** The line icon for a file, by what the viewer would make of it. */
export function fileIcon(name: string): IconName {
  switch (previewKind(name)) {
    case "image":
      return "image";
    case "json":
      // Braces, not a terminal or a picture: a data file drawn with either read as a command or
      // as an image attachment.
      return "braces";
    case "diff":
      return "changes";
    case "html":
      return "globe";
    case "csv":
    case "sheet":
      return "board";
    case "audio":
    case "video":
      return "play";
    case "markdown":
    case "text":
    case "pdf":
    case "docx":
      return "file";
    default:
      return "attach";
  }
}

function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i > 0 ? name.slice(i + 1).toLowerCase() : "";
}

export type ArtifactCardProps = {
  item: Artifact;
  /** Where the file is served from: the workspace for a written file, the call itself for a sent one. */
  src: PreviewSource;
  downloadUrl: string;
  onOpen: (src: PreviewSource) => void;
};

export function ArtifactCard({ item, src, downloadUrl, onOpen }: ArtifactCardProps) {
  const image = previewKind(item.name) === "image";
  const openable = canPreview(item.name);
  const meta = [item.size, extOf(item.name), t(`turn.artifact.${item.how}`)].filter(Boolean).join(" · ");
  const open = () => (openable ? onOpen(src) : window.open(downloadUrl, "_blank", "noreferrer"));
  return (
    <div className={`artifact ${image ? "image" : ""}`} data-path={item.path}>
      <button type="button" className="artifact-main" onClick={open} title={openable ? t("turn.artifact.open") : t("preview.download")}>
        <span className="artifact-glyph" aria-hidden><Icon name={fileIcon(item.name)} size={18} /></span>
        <span className="artifact-text">
          <span className="artifact-name truncate">{item.name}</span>
          <span className="artifact-meta truncate">{item.caption ? `${item.caption} · ${meta}` : meta}</span>
        </span>
      </button>
      <span className="artifact-actions">
        <a className="iconbtn small" href={downloadUrl} target="_blank" rel="noreferrer" aria-label={t("preview.download")} title={t("preview.download")} onClick={(e) => e.stopPropagation()}>
          <Icon name="download" size={15} />
        </a>
        {openable && (
          <button type="button" className="iconbtn small artifact-open" onClick={() => onOpen(src)} aria-label={t("turn.artifact.open")} title={t("turn.artifact.open")}>
            {/* Beside the chat it opens in the side panel; on a phone the same press opens the
                file over the whole screen, so the glyph says that rather than the button vanishing
                and leaving a phone with one action where the other sizes have two. */}
            <span className="artifact-open-panel"><Icon name="panel" size={15} /></span>
            <span className="artifact-open-full"><Icon name="expand" size={15} /></span>
          </button>
        )}
      </span>
      {image && (
        <div className="artifact-thumb">
          <AuthImg src={src} alt={item.name} className="tool-image" onClick={() => onOpen(src)} />
        </div>
      )}
    </div>
  );
}

/**
 * The files a message names by handle, as cards: what the operator attached, what a member handed
 * back, what an orchestrator passed on. Nothing is drawn until the host says what the handles are,
 * and a handle it does not know draws nothing.
 */
export function KeptFiles({ text, onOpen }: { text: string | null | undefined; onOpen: (src: PreviewSource) => void }) {
  const ids = handleIds(text);
  const { data } = useQuery<{ files: KeptFile[] }>(filesKey(ids), { staleMs: 60000 });
  const files = data?.files ?? [];
  if (!files.length) return null;
  return (
    <div className="artifacts kept-files" aria-label={t("files.kept")}>
      {files.map((f) => {
        const src: PreviewSource = { base: keptBase(f.id), path: f.name };
        const item: Artifact = { callId: f.id, path: f.handle, name: f.name, how: "kept", caption: t(`files.origin.${f.origin}`), size: bytes(f.size) };
        return <ArtifactCard key={f.id} item={item} src={src} downloadUrl={downloadHref(src.base, src.path)} onOpen={onOpen} />;
      })}
    </div>
  );
}

/** Where a changed file is served from: its folder's address and its path there. */
function changedSource(sessionId: string, file: ChangedFile): { base: string; path: string } {
  const base = sessionBase(sessionId);
  return file.place ? { base: folderBase(base, file.place.folder, ""), path: file.place.rel } : { base, path: file.path };
}

/**
 * The files a turn produced: the sent ones as cards, three before "+N more", and the changed ones as
 * one line that opens into a list. Nothing is drawn for a turn that touched no file.
 */
export function TurnFiles({ sessionId, sent, changed, onOpen }: { sessionId: string; sent: SentFile[]; changed: ChangedFile[]; onOpen: (src: PreviewSource) => void }) {
  const [allSent, setAllSent] = useState(false);
  if (!sent.length && !changed.length) return null;
  const shown = allSent ? sent : sent.slice(0, SENT_VISIBLE);
  const hidden = sent.length - shown.length;
  return (
    <div className="turn-files">
      {sent.length > 0 && (
        <div className="artifacts" aria-label={t("turn.artifacts")}>
          {shown.map((file) => {
            // A sent file is served by the call that sent it, so a path outside the workspace opens too.
            const src: PreviewSource = { base: `${sessionBase(sessionId)}/sent/${encodeURIComponent(file.callId)}`, path: file.name };
            const item: Artifact = { callId: file.callId, path: file.path, name: file.name, how: "sent", caption: file.caption, size: file.size };
            return <ArtifactCard key={file.callId} item={item} src={src} downloadUrl={downloadHref(src.base, src.path)} onOpen={onOpen} />;
          })}
          {hidden > 0 && (
            <button type="button" className="artifacts-more" onClick={() => setAllSent(true)}>
              {t("turn.files.more", { n: hidden })}
            </button>
          )}
        </div>
      )}
      {changed.length > 0 && <ChangedFiles sessionId={sessionId} files={changed} onOpen={onOpen} />}
    </div>
  );
}

/** The path a row shows: the folder part quieter than the name, so a column of them scans by name. */
function RowPath({ file }: { file: ChangedFile }) {
  const shown = file.place?.rel ?? file.path;
  const cut = shown.lastIndexOf("/") + 1;
  return (
    <span className="changed-file-path truncate" title={file.path}>
      {cut > 0 && <span className="changed-file-dir">{shown.slice(0, cut)}</span>}
      {shown.slice(cut)}
    </span>
  );
}

/**
 * The changed files: one line with the count, opened into compact rows. Beside the chat a row opens
 * its file in the panel and shows its actions on hover; on a phone the line opens a sheet of rows, a
 * tap opens the file and a long press offers the same actions.
 */
function ChangedFiles({ sessionId, files, onOpen }: { sessionId: string; files: ChangedFile[]; onOpen: (src: PreviewSource) => void }) {
  const phone = !useMedia("(min-width: 1024px)");
  const [open, setOpen] = useState(false);
  const revealer = useReveal();
  const open_ = (file: ChangedFile) => {
    const src = changedSource(sessionId, file);
    if (canPreview(file.name)) onOpen(src);
    else window.open(downloadHref(src.base, src.path), "_blank", "noreferrer");
  };
  // A file outside every folder of the session cannot be confined by the host, so it is not offered.
  const revealOf = (file: ChangedFile) => (revealer && file.place ? () => void revealer.reveal({ session_id: sessionId, folder_id: file.place!.folder || undefined, path: file.place!.rel }) : null);
  const head = (
    <button type="button" className="changed-files-head" aria-expanded={phone ? undefined : open} aria-haspopup={phone ? "dialog" : undefined} onClick={() => setOpen((o) => !o)}>
      <Icon name="changes" size={14} />
      <span className="changed-files-count">{plural("turn.files.changed", files.length)}</span>
      {!phone && <Chevron open={open} />}
    </button>
  );
  if (phone) {
    return (
      <div className="changed-files">
        {head}
        {open && (
          <BottomSheet title={plural("turn.files.changed", files.length)} onClose={() => setOpen(false)} className="changed-files-sheet">
            {files.map((file) => {
              const src = changedSource(sessionId, file);
              const revealFile = revealOf(file);
              const actions: MenuItem[] = [
                ...(canPreview(file.name) ? [{ label: t("turn.artifact.open"), icon: "expand" as IconName, onSelect: () => { setOpen(false); onOpen(src); } }] : []),
                { label: t("common.download"), icon: "download", onSelect: () => window.open(downloadHref(src.base, src.path), "_blank", "noreferrer") },
                ...(revealer && revealFile ? [{ label: revealer.label, icon: "external" as IconName, onSelect: revealFile }] : []),
              ];
              return (
                <ListRow
                  key={file.callId}
                  one
                  more={false}
                  className="changed-file-row"
                  lead={<Icon name={fileIcon(file.name)} size={18} />}
                  title={file.place?.rel ?? file.path}
                  preview={{ title: file.name, meta: file.place?.rel ?? file.path }}
                  actions={actions}
                  onOpen={() => { setOpen(false); open_(file); }}
                />
              );
            })}
          </BottomSheet>
        )}
      </div>
    );
  }
  return (
    <div className={`changed-files ${open ? "open" : ""}`}>
      {head}
      {open && (
        <ul className="changed-file-list">
          {files.map((file) => {
            const src = changedSource(sessionId, file);
            const openable = canPreview(file.name);
            const revealFile = revealOf(file);
            return (
              <li key={file.callId} className="changed-file" data-path={file.place?.rel ?? file.path}>
                <button type="button" className="changed-file-main" onClick={() => open_(file)} title={openable ? t("turn.artifact.open") : t("preview.download")}>
                  <span className="changed-file-glyph" aria-hidden><Icon name={fileIcon(file.name)} size={14} /></span>
                  <RowPath file={file} />
                  {file.how === "edited" && <span className="changed-file-how">{t("turn.files.edited")}</span>}
                </button>
                <span className="changed-file-actions">
                  {openable && (
                    <button type="button" className="iconbtn small" onClick={() => onOpen(src)} aria-label={t("turn.artifact.open")} title={t("turn.artifact.open")}>
                      <Icon name="panel" size={14} />
                    </button>
                  )}
                  <a className="iconbtn small" href={downloadHref(src.base, src.path)} target="_blank" rel="noreferrer" aria-label={t("preview.download")} title={t("preview.download")}>
                    <Icon name="download" size={14} />
                  </a>
                  {revealer && revealFile && (
                    <button type="button" className="iconbtn small changed-file-reveal" onClick={revealFile} aria-label={revealer.label} title={revealer.label}>
                      <Icon name="external" size={14} />
                    </button>
                  )}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
