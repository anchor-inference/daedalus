// The staff worktrees as folders of the orchestrator's file pane. The orchestrator writes no file,
// yet what the staff made lives in their worktrees until it is merged, and the operator watching the
// project from the orchestrator had nowhere to see it.

import type { ProjectDir, SessionFolder } from "../api";
import type { Listing } from "./ProjectWorktrees";

/**
 * One read-only folder per worktree the host can serve. A worktree that is about to be pruned, or one
 * without a `pane`, is left out: its files are gone or unreachable, and a folder that lists nothing
 * would read as an empty piece of work. The environment is the project folder's the worktree sits in,
 * since that is where its files are.
 */
export function worktreeFolders(listing: Listing | undefined, folders: readonly Pick<SessionFolder, "id" | "env">[], projectFolders: readonly Pick<ProjectDir, "id" | "env">[] = []): SessionFolder[] {
  return (listing?.worktrees ?? []).filter((row) => !row.prunable && row.pane).map((row) => {
    const env = folders.find((f) => f.id === row.folder_id)?.env ?? projectFolders.find((f) => f.id === row.folder_id)?.env ?? "container";
    const who = row.member?.name || row.name;
    return { id: row.pane!, path: row.path, label: row.branch ? `${who} · ${row.branch}` : who, env, readonly: true, writable: false };
  });
}
