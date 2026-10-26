// The folder moves running now, wherever the app shows a folder: the chat's header and its workspace
// card, the project's folder row, and an "in progress" part of the inbox. A move of half a gigabyte
// takes minutes, and the operator who started it has usually closed its dialog — and before these,
// a page reloaded in the middle of one showed no sign of it anywhere, so the move looked lost while
// it was still copying.
//
// The list is one query shared by every place, read again once and a half a second only while a move
// runs, and on the host's word that one started or ended; with nothing moving it costs nothing.

import type { FolderMoveJob, Project } from "../api";
import { useEvent } from "../events";
import { t } from "../i18n";
import { Icon } from "../icons";
import { entryPath } from "../notifications";
import { navigate } from "../router";
import { invalidate, peek, useQuery } from "../store";
import "./newproject.css";

export const MOVES_KEY = "/api/folder-moves?active=true";
/** How often the running moves are read while one runs: often enough that the percentage moves. */
export const MOVES_POLL_MS = 1500;

type Moves = { jobs: FolderMoveJob[] };

/** The moves running now. */
export function useActiveMoves(): FolderMoveJob[] {
  const running = (peek<Moves>(MOVES_KEY)?.jobs ?? []).some((job) => job.active);
  const query = useQuery<Moves>(MOVES_KEY, { pollMs: running ? MOVES_POLL_MS : 0, staleMs: 10000 });
  useEvent(["project.changed"], (event) => {
    const change = String(event.payload.change ?? "");
    if (change === "folder_moved" || change.startsWith("folder_move")) {
      invalidate(MOVES_KEY);
      // A move that ended changed where the folder is: the rows and the chats read it again.
      if (change !== "folder_move_started") {
        invalidate("/api/projects");
        invalidate("/api/sessions");
      }
    }
  });
  return (query.data?.jobs ?? []).filter((job) => job.active);
}

/** The move of one folder running now, if there is one. */
export function useFolderMove(folderId: string | null | undefined): FolderMoveJob | null {
  const moves = useActiveMoves();
  return folderId ? moves.find((job) => job.folder_id === folderId) ?? null : null;
}

/** How far a move is, as a whole percentage of its bytes. */
export function movePercent(job: Pick<FolderMoveJob, "done_bytes" | "total_bytes" | "state">): number {
  if (job.state === "done") return 100;
  if (job.total_bytes <= 0) return 0;
  return Math.max(0, Math.min(100, Math.floor((100 * job.done_bytes) / job.total_bytes)));
}

/** The line a running move is shown by: "Moving to the host… 42 %", or that it is finishing. */
export function moveLine(job: Pick<FolderMoveJob, "direction" | "state" | "done_bytes" | "total_bytes">): string {
  if (job.state === "switching" || job.state === "cleaning" || job.state === "verifying") return t("move.live.finishing");
  return t(job.direction === "to_host" ? "move.live.tohost" : "move.live.tocontainer", { pct: movePercent(job) });
}

/** A running move under a folder: its line and a bar. `compact` is the line alone, for a header. */
export function MoveLive({ job, compact = false }: { job: FolderMoveJob; compact?: boolean }) {
  const line = moveLine(job);
  if (compact) return <span className="head-status moving" role="status" data-folder-move-live={job.folder_id}>{line}</span>;
  return (
    <div className="move-live" role="status" data-folder-move-live={job.folder_id}>
      <span className="sub">{line}</span>
      <progress max={100} value={movePercent(job)} aria-label={line} />
    </div>
  );
}

/** Where a move's row in the inbox takes the reader: the chat whose folder it is, or the project. */
export function movePath(job: Pick<FolderMoveJob, "project_id">, projects: Project[] | undefined): string {
  const project = projects?.find((item) => item.id === job.project_id);
  const chat = project?.settings.ephemeral ? project.sessions[0]?.id : undefined;
  return entryPath({ link: chat ? `/app/agents/${chat}` : `/app/project/${job.project_id}`, session_id: null });
}

/** The moves running now, as the first part of the inbox: nothing at all while nothing moves. */
export function MovesInProgress({ projects, onOpened }: { projects: Project[] | undefined; onOpened?: () => void }) {
  const moves = useActiveMoves();
  if (moves.length === 0) return null;
  return (
    <div className="moves-now" data-moves-now={moves.length}>
      <div className="moves-now-head sub">{t("move.inprogress")}</div>
      {moves.map((job) => {
        const name = projects?.find((item) => item.id === job.project_id)?.name ?? job.source;
        return (
          <button key={job.id} type="button" className="moves-now-row" onClick={() => { navigate(movePath(job, projects)); onOpened?.(); }}>
            <Icon name="forward" size={14} />
            <span className="grow truncate">{name}</span>
            <span className="sub num">{moveLine(job)}</span>
          </button>
        );
      })}
    </div>
  );
}
