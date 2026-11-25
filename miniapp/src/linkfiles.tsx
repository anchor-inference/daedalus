// What both sides of an agent link draw the same way: a file that went or came over it, a task one agent set
// on the other, and the operator's choice of which waiting files may go. The Agents tab (agenttalk.tsx) and a
// link's conversation (guestlinks.tsx) use these, so a file reads alike whichever door it passed.
//
// A file's name and a task's words are a stranger's when they came in: plain text, never a link or markup.
// Only a type the host vouches for as a picture (`image`) is drawn as one, from bytes fetched with the app's
// own credential, since an <img src> carries none.

import { useEffect, useState } from "react";
import { api, HeldGroup, LinkFile, LinkTask } from "./api";
import { bytes } from "./format";
import { Icon } from "./icons";
import { t } from "./i18n";
import { errorText } from "./ui";
import "./linkfiles.css";

const encoded = encodeURIComponent;
export const filePath = (id: number) => `/api/link-files/${id}`;

/** The states a task can still be stopped in; the rest are its end. */
export const OPEN_TASK = new Set(["accepted", "working", "needs_input"]);

/** The tone of a task's chip, in the Subagents tab's colours. */
const TASK_PHASE: Record<string, string> = { accepted: "standby", working: "working", needs_input: "question", done: "done", refused: "failed", cancelled: "stopped" };
export const taskPhase = (state: string) => TASK_PHASE[state] ?? "standby";

/** Whether this side may cancel a task: only one it is waiting on, and only while it is open. The Agents tab
 *  cancels what its agent set (`mine`); a link's owner cancels what the guest set on the chat (`theirs`). */
export function cancellable(task: Pick<LinkTask, "direction" | "state">, side: LinkTask["direction"]): boolean {
  return task.direction === side && OPEN_TASK.has(task.state);
}

/** The word a file's state is shown with, or "" where it went or came as it should: a word on every row would
 *  bury the ones that say something. */
const QUIET = new Set(["received", "sent"]);
const FILE_STATES = new Set(["pending", "refused", "failed", "held", "sending", "dropped"]);
export function fileWord(state: string): string {
  if (QUIET.has(state)) return "";
  return FILE_STATES.has(state) ? t(`link.file.${state}`) : state;
}

/** The start of a hash, enough to compare by eye with what the other side says it sent. */
export const shortHash = (sha256: string) => sha256.slice(0, 10);

/** A picture's bytes as a blob URL, revoked when the row goes. Null while it loads and when it cannot be read. */
function useBlob(id: number, enabled: boolean): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    let made: string | null = null;
    void api.fetch(filePath(id)).then(async (response) => {
      if (!response.ok || !live) return;
      made = URL.createObjectURL(await response.blob());
      if (live) setUrl(made);
      else URL.revokeObjectURL(made);
    }).catch(() => {});
    return () => {
      live = false;
      if (made) URL.revokeObjectURL(made);
      setUrl(null);
    };
  }, [id, enabled]);
  return url;
}

/** Saves a file under its own name. A plain link would go out without the app's credential and be refused. */
export async function saveLinkFile(file: Pick<LinkFile, "id" | "name">, toast: (text: string) => void): Promise<void> {
  try {
    const response = await api.fetch(filePath(file.id));
    if (!response.ok) throw new Error(t("link.file.gone"));
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = file.name;
    document.body.append(link);
    link.click();
    link.remove();
    // A download reads the blob after the click returns; a minute is long past that for any file.
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (e) {
    toast(errorText(e));
  }
}

/** One file: a thumbnail for a picture or a mark for anything else, its name, size and the start of its hash, a
 *  word for a state worth saying, and Download while the bytes are kept here. With `pick` it is a choice too:
 *  ticked goes, unticked stays here. */
export function LinkFileRow({ file, toast, pick }: { file: LinkFile; toast: (text: string) => void; pick?: { checked: boolean; disabled?: boolean; onChange: (checked: boolean) => void } }) {
  const thumb = useBlob(file.id, file.image);
  // In an approval every file waits; the tick says what becomes of it, and a word on each would say it again.
  const word = pick ? "" : fileWord(file.state);
  const meta = (
    <>
      <span className="link-file-name truncate" title={file.name}>{file.name}</span>
      <span className="link-file-meta">
        <span>{bytes(file.size)}</span>
        {file.sha256 && <span className="mono" title={file.sha256} data-link-file-hash="">{shortHash(file.sha256)}</span>}
        {word && <span className="link-file-word" data-link-file-word={file.state}>{word}</span>}
      </span>
    </>
  );
  const mark = file.image
    ? <span className="link-file-thumb" data-link-file-thumb="">{thumb ? <img src={thumb} alt="" /> : <Icon name="image" size={16} />}</span>
    : <span className="link-file-thumb glyph" aria-hidden="true"><Icon name="file" size={16} /></span>;
  return (
    <div className="link-file" data-link-file={file.id} data-state={file.state} data-image={file.image || undefined}>
      {pick ? (
        <label className="link-file-pick">
          <input type="checkbox" checked={pick.checked} disabled={pick.disabled} aria-label={file.name} onChange={(e) => pick.onChange(e.target.checked)} />
          {mark}
          <span className="link-file-text">{meta}</span>
        </label>
      ) : (
        <>
          {thumb ? <a className="link-file-open" href={thumb} target="_blank" rel="noreferrer" title={t("link.file.open")}>{mark}</a> : mark}
          <span className="link-file-text">{meta}</span>
        </>
      )}
      {file.downloadable && (
        <button type="button" className="btn small ghost link-file-save" title={t("common.download")} aria-label={`${t("common.download")}: ${file.name}`} data-link-file-save="" onClick={() => void saveLinkFile(file, toast)}>
          <Icon name="download" size={14} />
        </button>
      )}
    </div>
  );
}

export function LinkFiles({ files, toast }: { files: readonly LinkFile[]; toast: (text: string) => void }) {
  if (!files.length) return null;
  return <div className="link-files">{files.map((file) => <LinkFileRow key={file.id} file={file} toast={toast} />)}</div>;
}

/** Waiting files with a tick each, all ticked to begin with: what the operator unticks is the `drop` list. */
export function FilePicks({ files, dropped, disabled, toast, onChange }: {
  files: readonly LinkFile[]; dropped: ReadonlySet<number>; disabled?: boolean; toast: (text: string) => void; onChange: (next: Set<number>) => void;
}) {
  if (!files.length) return null;
  const toggle = (id: number, going: boolean) => {
    const next = new Set(dropped);
    if (going) next.delete(id);
    else next.add(id);
    onChange(next);
  };
  return (
    <div className="link-files link-picks" data-link-picks="">
      {files.map((file) => <LinkFileRow key={file.id} file={file} toast={toast} pick={{ checked: !dropped.has(file.id), disabled, onChange: (going) => toggle(file.id, going) }} />)}
    </div>
  );
}

/** One task: who set it, its words, its state, what came of it once done, and Cancel while this side may. */
export function TaskCard({ task, side, busy, onCancel }: { task: LinkTask; side: LinkTask["direction"]; busy?: boolean; onCancel?: (task: LinkTask) => void }) {
  const phase = taskPhase(task.state);
  return (
    <div className="link-task" data-link-task={task.task} data-state={task.state} data-direction={task.direction}>
      <div className="link-task-head">
        <span className="sub grow truncate">{t(task.direction === "mine" ? "link.task.mine" : "link.task.theirs")}</span>
        <span className="subagent-chip" data-phase={phase} data-link-task-state="">{t(`link.task.state.${task.state}`)}</span>
      </div>
      <div className="talk-text link-task-text">{task.text}</div>
      {task.result && (task.state === "done" || task.state === "refused") && (
        <div className="link-task-result" data-link-task-result=""><span className="sub">{t("link.task.result")}</span><div className="talk-text">{task.result}</div></div>
      )}
      {onCancel && cancellable(task, side) && (
        <button type="button" className="btn small link-task-cancel" disabled={busy} data-link-task-cancel={task.task} onClick={() => onCancel(task)}>{t("link.task.cancel")}</button>
      )}
    </div>
  );
}

export function TaskList({ tasks, side, busy, onCancel }: { tasks: readonly LinkTask[]; side: LinkTask["direction"]; busy?: boolean; onCancel?: (task: LinkTask) => void }) {
  if (!tasks.length) return null;
  // The open ones first: they are what can still be answered or stopped.
  const ordered = [...tasks].sort((a, b) => Number(OPEN_TASK.has(b.state)) - Number(OPEN_TASK.has(a.state)));
  return <div className="link-tasks" data-link-tasks="">{ordered.map((task) => <TaskCard key={task.task} task={task} side={side} busy={busy} onCancel={onCancel} />)}</div>;
}

/** The label a message's kind is shown with beside who said it; plain words go without one. */
export function kindWord(kind: string | undefined, taskState?: string): string {
  if (kind === "task") return t("link.kind.task");
  if (kind === "question") return t("link.kind.question");
  if (kind === "file") return t("link.kind.file");
  if (kind === "contact") return t("link.kind.contact");
  if (kind === "status") return taskState && TASK_PHASE[taskState] ? `${t("link.kind.status")} · ${t(`link.task.state.${taskState}`)}` : t("link.kind.status");
  return "";
}

const guestOutbox = (group: HeldGroup) => `/api/guest/links/${encoded(group.link_id)}/outbox/${encoded(group.batch)}`;

/** Files a guest's turn on one of this chat's links wants to send, as an approval: which link, what the turn
 *  said of them, each file with its tick, and Send or Don't send. Sending with every tick off sends nothing, so
 *  Send waits for at least one. */
export function GuestHeldApproval({ group, phone, toast, onDone }: { group: HeldGroup; phone?: boolean; toast: (text: string) => void; onDone: () => void }) {
  const [dropped, setDropped] = useState<Set<number>>(() => new Set());
  const [busy, setBusy] = useState(false);
  const title = t("link.held.title", { label: group.label || t("guest.untitled") });
  const none = dropped.size >= group.files.length;
  async function act(send: boolean) {
    if (busy || (send && none)) return;
    setBusy(true);
    try {
      await api.post(`${guestOutbox(group)}/${send ? "send" : "refuse"}`, send ? { drop: [...dropped] } : undefined);
      onDone();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const picks = <FilePicks files={group.files} dropped={dropped} disabled={busy} toast={toast} onChange={setDropped} />;
  const note = group.note ? <div className={`${phone ? "ph-tray-detail" : "dock-detail"} talk-approve-text`}>{group.note}</div> : null;
  const why = <div className="sub talk-approve-why">{none ? t("link.held.none") : t("link.held.why")}</div>;
  if (phone) {
    return (
      <div className="dock approval ph-tray talk-approval" role="group" aria-label={title} data-guest-held={group.batch}>
        <div className="ph-tray-head warn"><Icon name="link" size={14} /><span>{t("link.held.head")}</span></div>
        <div className="ph-tray-title">{title}</div>
        {why}
        {note}
        {picks}
        <div className="ph-tray-actions">
          <button type="button" className="ph-btn primary" data-action="send" disabled={busy || none} onClick={() => void act(true)}>{t("talk.send")}</button>
          <button type="button" className="ph-btn ph-deny" data-action="refuse" disabled={busy} onClick={() => void act(false)}>{t("talk.refuse")}</button>
        </div>
      </div>
    );
  }
  return (
    <div className="dock approval talk-approval" role="group" aria-label={title} data-guest-held={group.batch}>
      <div className="dock-title">
        <Icon name="link" size={16} />
        <span className="grow truncate">{title}</span>
      </div>
      {why}
      {note}
      {picks}
      <div className="dock-actions">
        <button type="button" className="btn small primary" data-action="send" disabled={busy || none} onClick={() => void act(true)}>{t("talk.send")}</button>
        <button type="button" className="btn small" data-action="refuse" disabled={busy} onClick={() => void act(false)}>{t("talk.refuse")}</button>
      </div>
    </div>
  );
}
