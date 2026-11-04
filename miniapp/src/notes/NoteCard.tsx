// One note in the panel's list: its colour on the edge, its title, two lines of its words, its first
// picture, how many of its tasks are done, how many files it shows, when it was edited and whether the
// agent did it. The whole card opens the note; the pin and the menu sit above that.

import { memo, useEffect, useState } from "react";
import { relTime } from "../format";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { OverflowMenu, type MenuItem } from "../ui/dialogs";
import { fileUrl } from "./api";
import { excerpt, firstImage, references, shownTitle, taskProgress, type Note, type NoteFile } from "./model";

export type CardProps = {
  note: Note;
  index: number;
  files: Map<string, NoteFile>;
  query: string;
  menu: MenuItem[];
  draggable: boolean;
  leaving: boolean;
  onOpen: (note: Note) => void;
  onPin: (note: Note) => void;
  onGrab?: (note: Note, e: React.PointerEvent<HTMLElement>) => void;
};

export const NoteCard = memo(function NoteCard({ note, index, files, query, menu, draggable, leaving, onOpen, onPin, onGrab }: CardProps) {
  const title = shownTitle(note);
  const words = excerpt(note.body, 200);
  const picture = firstImage(note.body, files);
  const shown = references(note.body).filter((id) => files.has(id)).length;
  const tasks = taskProgress(note.body);
  return (
    <article data-note={note.id} className={`note-card ${note.color ? `tone-${note.color}` : ""} ${note.pinned ? "pinned" : ""} ${leaving ? "leaving" : ""}`} style={{ "--i": Math.min(index, 12) } as React.CSSProperties}>
      {draggable && onGrab && (
        <span className="note-grip" aria-hidden="true" onPointerDown={(e) => onGrab(note, e)}><Icon name="vdots" size={14} /></span>
      )}
      <button type="button" className="note-open" onClick={() => onOpen(note)} aria-label={t("notes.open", { title: title || t("notes.untitled") })} />
      <div className="note-main">
        <div className={`note-title ${title ? "" : "untitled"}`}>
          {note.pinned && <span className="note-pinmark" aria-label={t("notes.pinned")}><Icon name="pushpin" size={12} /></span>}
          <Mark text={title || t("notes.untitled")} query={query} />
        </div>
        {words && <div className="note-excerpt"><Mark text={words} query={query} /></div>}
        <div className="note-meta">
          <span title={note.updated_at}>{relTime(note.updated_at)}</span>
          {note.updated_by === "agent" && <span className="note-agent" title={t("notes.edited.agent.short")}><Icon name="bolt" size={12} />{t("notes.agent")}</span>}
          {tasks.total > 0 && <TaskRing done={tasks.done} total={tasks.total} />}
          {shown > 0 && <span className="note-files" title={plural("notes.files.count", shown)}><Icon name="attach" size={12} />{shown}</span>}
        </div>
      </div>
      {picture && <Thumb file={picture} />}
      <div className="note-actions">
        <button type="button" className={`note-pin ${note.pinned ? "on" : ""}`} onClick={() => onPin(note)} aria-pressed={note.pinned} aria-label={t(note.pinned ? "notes.unpin" : "notes.pin")} title={t(note.pinned ? "notes.unpin" : "notes.pin")}>
          <Icon name="pushpin" size={14} />
        </button>
        <OverflowMenu items={menu} small label={t("notes.more")} />
      </div>
    </article>
  );
});

/** The words with what the search found marked, so a long note shows why it matched. */
function Mark({ text, query }: { text: string; query: string }) {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return <>{text}</>;
  const lower = text.toLowerCase();
  const marks: [number, number][] = [];
  for (const term of terms) {
    let at = lower.indexOf(term);
    while (at >= 0 && marks.length < 20) {
      marks.push([at, at + term.length]);
      at = lower.indexOf(term, at + term.length);
    }
  }
  if (!marks.length) return <>{text}</>;
  marks.sort((a, b) => a[0] - b[0]);
  const parts: React.ReactNode[] = [];
  let from = 0;
  marks.forEach(([s, e], i) => {
    if (s < from) return;
    parts.push(text.slice(from, s), <mark key={i}>{text.slice(s, e)}</mark>);
    from = e;
  });
  parts.push(text.slice(from));
  return <>{parts}</>;
}

function TaskRing({ done, total }: { done: number; total: number }) {
  const r = 5.5;
  const length = 2 * Math.PI * r;
  const all = done === total;
  return (
    <span className={`note-tasks ${all ? "done" : ""}`} title={t("notes.tasks", { done, total })}>
      <svg viewBox="0 0 14 14" width="13" height="13" aria-hidden="true">
        <circle cx="7" cy="7" r={r} className="track" />
        <circle cx="7" cy="7" r={r} className="fill" strokeDasharray={`${(done / total) * length} ${length}`} />
      </svg>
      {done}/{total}
    </span>
  );
}

function Thumb({ file }: { file: NoteFile }) {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let gone = false;
    fileUrl(file.id).then((u) => !gone && setUrl(u), () => undefined);
    return () => {
      gone = true;
    };
  }, [file.id]);
  return <span className={`note-thumb ${url ? "loaded" : ""}`} aria-hidden="true">{url && <img src={url} alt="" />}</span>;
}
