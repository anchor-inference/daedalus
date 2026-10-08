// A task on a phone, as the review page of the design: a pushed page with the task's name, where it
// stands and its priority in the bar, three tabs (Task, Result, Diff with its file count) and a
// decision footer that stays at the bottom whichever tab is open. The tabs hold the task sheet's own
// parts, handed in by ProjectBoard's TaskSheet, so a phone and a desktop review the same thing with
// the same commands; only the layout differs. Every tab stays mounted while hidden, because the
// result's component on the Result tab is the one a tapped diff line hands its note to.

import { type ReactNode, useState } from "react";
import { Sheet } from "../ui/dialogs";
import { copyText } from "../ui/components";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { navigate, projectSessionPath } from "../router";
import { useQuery } from "../store";
import { ActionSheet, IconButton, SegmentedControl, TopBar } from "../ui/phone";
import { useProject } from "../project/data";
import { relTime } from "../format";
import { DiffView } from "../previewparts";
import { DecisionSlot } from "./slot";
import { NEXT, type ProjectTask, type Review, type TaskStatus } from "./board";

type Tab = "task" | "result" | "diff";
const STEPS = ["handed_in", "accepted", "operator_approved"] as const;

export function PhoneTaskPage({ projectId, task, onClose, toast, busy, writeBlocked, onArchive, onMove, taskTab, resultTab, diffTab }: {
  projectId: string; task: ProjectTask; onClose: () => void; toast: (text: string) => void; busy: boolean; writeBlocked: boolean;
  onArchive: () => void; onMove: (status: TaskStatus) => void; taskTab: ReactNode; resultTab: ReactNode | null; diffTab: ReactNode | null;
}) {
  const { project } = useProject(projectId);
  // The same key the review panel reads, so the tab's file count costs no second request.
  const review = useQuery<Review>(diffTab ? `/api/board/${encodeURIComponent(task.id)}/review` : null, { staleMs: 2000 });
  // A task with a result to judge, or one already accepted, opens on that result; work still under
  // way opens on the task.
  const [tab, setTab] = useState<Tab>(resultTab && (task.status === "review" || task.status === "done") ? "result" : "task");
  const [slot, setSlot] = useState<HTMLDivElement | null>(null);
  const [menu, setMenu] = useState(false);
  const sub = [project?.name, t(`board.col.${task.status}`), `P${task.priority}`].filter(Boolean).join(" · ");
  const reached = task.acceptance_state === "operator_approved" ? 3 : task.acceptance_state === "accepted" ? 2 : task.acceptance_state === "handed_in" || task.acceptance_state === "returned" ? 1 : 0;
  const files = review.data?.files.length;
  const tabs: { id: Tab; label: ReactNode }[] = [
    { id: "task", label: t("pres.tab.task") },
    ...(resultTab ? [{ id: "result" as const, label: t("pres.tab.result") }] : []),
    ...(diffTab ? [{ id: "diff" as const, label: <>{t("pres.tab.diff")}{files ? <span className="ph-seg-n">{files}</span> : null}</> }] : []),
  ];
  return (
    <Sheet size="full" className="pboard-sheet ph-taskpage" onClose={onClose} ariaLabel={task.title}>
      <DecisionSlot.Provider value={slot}>
        <div className="ph-taskpage-top">
          <TopBar back={onClose} title={task.title} sub={sub}
            actions={<IconButton icon="vdots" label={t("board.actions")} onClick={() => setMenu(true)} popup="menu" expanded={menu} />} />
          {tabs.length > 1 && <div className="ph-taskpage-tabs"><SegmentedControl label={t("pres.tabs")} value={tab} onChange={setTab} options={tabs} /></div>}
        </div>
        <div className="ph-taskpage-body" hidden={tab !== "task"} data-tab="task">{taskTab}</div>
        {resultTab && (
          <div className="ph-taskpage-body" hidden={tab !== "result"} data-tab="result">
            {/* Where the result stands: handed in, checked by the orchestrator, approved by the operator. */}
            <ol className="ph-steps" aria-label={t("pboard.acceptance")}>
              {STEPS.map((step, i) => (
                <li key={step} className={i < reached ? "done" : i === reached ? "next" : ""}>
                  {i < reached && <Icon name="check" size={16} />}{t(`pres.step.${step}`)}
                </li>
              ))}
            </ol>
            {resultTab}
          </div>
        )}
        {diffTab && <div className="ph-taskpage-body" hidden={tab !== "diff"} data-tab="diff">{diffTab}</div>}
        <div ref={setSlot} className="ph-decide" />
      </DecisionSlot.Provider>
      {menu && (
        <ActionSheet onClose={() => setMenu(false)} preview={{ title: task.title, meta: sub }} items={[
          ...(task.assignee?.session_id ? [{ label: t("pboard.open.staff", { name: task.assignee.name }), icon: "bots" as const, onSelect: () => navigate(projectSessionPath(projectId, task.assignee!.session_id!)) }] : []),
          { label: t("board.copyid"), icon: "copy", onSelect: async () => toast((await copyText(task.id)) ? t("board.copied") : task.id) },
          ...NEXT[task.status].map((status) => ({ label: t("pbph.moveto", { column: t(`board.col.${status}`) }), icon: "forward" as const, disabled: writeBlocked, onSelect: () => onMove(status) })),
          "-",
          { label: t("pboard.archive.action"), icon: "archive", danger: true, disabled: writeBlocked || busy || !["todo", "blocked"].includes(task.status), hint: t("pboard.archive.unavailable"), onSelect: onArchive },
        ]} />
      )}
    </Sheet>
  );
}

type Note = { comment_id: string; priority: "blocking" | "important" | "suggestion"; body: string; path: string | null; line_start: number | null; state: string; created_at: string };

/** The notes left on a result's lines, read under the keys the result's own component reads them by. */
export function useLineNotes(taskId: string, enabled: boolean): Note[] {
  const base = `/api/board/${encodeURIComponent(taskId)}`;
  const results = useQuery<{ result_id: string; current_result_id?: string | null }[]>(enabled ? `${base}/results` : null, { staleMs: 2000 });
  const list = Array.isArray(results.data) ? results.data : [];
  const current = list.find((r) => r.result_id === list[0]?.current_result_id) ?? list[0];
  const comments = useQuery<Note[]>(enabled && current ? `${base}/results/${encodeURIComponent(current.result_id)}/comments` : null, { staleMs: 2000 });
  return Array.isArray(comments.data) ? comments.data.filter((c) => c.path && c.line_start) : [];
}

/** A changed file as a row of the Diff tab: its kind, its folder and name, what changed and its notes. */
export function FileRow({ path, added, removed, notes, onOpen }: { path: string; added: number | null; removed: number; notes: Note[]; onOpen: () => void }) {
  const cut = path.lastIndexOf("/");
  const open = notes.filter((n) => n.state === "open" || n.state === "reopened");
  const blocking = open.some((n) => n.priority === "blocking");
  return (
    <button type="button" className="ph-frow" onClick={onOpen} data-file={path}>
      <Icon name={added === null ? "image" : "file"} size={22} />
      <span className="ph-frow-main">
        <span className="ph-frow-path">{cut >= 0 && <span>{path.slice(0, cut + 1)}</span>}<b>{path.slice(cut + 1)}</b></span>
        <span className="ph-frow-m">{added === null ? t("pboard.review.binary") : <><span className="tk-add">+{added}</span><span className="tk-del">−{removed}</span></>}</span>
      </span>
      {notes.length > 0 && <span className={`ph-frow-notes ${blocking ? "bad" : ""}`} title={plural("pres.notes", notes.length)}><Icon name="journal" size={18} />{notes.length}</span>}
    </button>
  );
}

/**
 * One file of the branch, pushed over the review page: its name and folder in the bar, the arrows to
 * the file before and after, and its diff wrapped to the phone's width with the notes under the lines
 * they are about. A tap on a line opens the note sheet of the result's component.
 */
export function FileDiff({ patch, files, index, notes, onLine, onIndex, onClose }: {
  patch: string; files: { path: string; added: number | null; removed: number }[]; index: number; notes: Note[];
  onLine: (anchor: { path: string; line: number }) => void; onIndex: (i: number) => void; onClose: () => void;
}) {
  const file = files[index];
  const cut = file.path.lastIndexOf("/");
  const mine = notes.filter((n) => n.path === file.path);
  const sub = [cut >= 0 ? file.path.slice(0, cut) : "", file.added === null ? t("pboard.review.binary") : `+${file.added} −${file.removed}`, mine.length ? plural("pres.notes", mine.length) : ""].filter(Boolean).join(" · ");
  return (
    <Sheet size="full" className="ph-filediff" onClose={onClose} ariaLabel={file.path}>
      <TopBar back={onClose} title={<span className="mono">{file.path.slice(cut + 1)}</span>} sub={sub}
        actions={<>
          <IconButton icon="up" label={t("pres.file.prev")} disabled={index === 0} onClick={() => onIndex(index - 1)} />
          <IconButton icon="down" label={t("pres.file.next")} disabled={index === files.length - 1} onClick={() => onIndex(index + 1)} />
        </>} />
      <p className="ph-filediff-hint"><Icon name="pen" size={16} />{t("diff.commentHint")}</p>
      <div className="ph-filediff-body">
        <DiffView text={patch} file={file.path} onLine={onLine} notes={(anchor) => {
          const here = mine.filter((n) => n.line_start === anchor.line);
          return here.length ? <div className="ph-linenotes">{here.map((n) => (
            <div key={n.comment_id} className={`ph-linenote ${n.priority} ${n.state}`}>
              <div className="ph-linenote-head"><span className="grow">{t("pres.note.line", { line: n.line_start ?? "" })} · {relTime(n.created_at)}</span>
                <span className={`ph-pill ${n.state === "open" || n.state === "reopened" ? (n.priority === "blocking" ? "bad" : "") : "ok"}`}>{n.state === "open" || n.state === "reopened" ? t(`pres.priority.${n.priority}`) : t(`result.commentState.${n.state}`)}</span></div>
              <div className="ph-linenote-body">{n.body}</div>
            </div>
          ))}</div> : null;
        }} />
      </div>
    </Sheet>
  );
}
