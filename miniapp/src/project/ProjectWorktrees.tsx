// The worker worktrees in a project's folders. A member's worktree is kept from task to task so its
// installed dependencies stay installed, which also means nothing ever cleans one up: this is the
// one place that lists them, with removal offered only where it cannot lose work.

import { useState } from "react";
import { api } from "../api";
import { bytes, relTime } from "../format";
import { plural, t } from "../i18n";
import { invalidate, useOffline, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";

type WorkerWorktree = {
  folder_id: string; path: string; name: string;
  member: { id: string; name: string; dismissed: boolean } | null;
  branch: string; base: string; size_bytes: number | null; changes: number; last_commit_at: string;
  merged: boolean; prunable: boolean;
  live: { staff_id: string; name: string; status: string } | null;
  blocked: "" | "live" | "changes" | "readonly";
};
type Listing = { worktrees: WorkerWorktree[]; problems: { folder_id: string; reason: string }[] };

export const worktreesKey = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/worktrees`;

export function ProjectWorktrees({ projectId, toast }: { projectId: string; toast: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  return <details className="sheet-section project-worktrees" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("worktrees.title")}</summary>
    {open && <WorktreeList key={projectId} projectId={projectId} toast={toast} />}
  </details>;
}

function WorktreeList({ projectId, toast }: { projectId: string; toast: (message: string) => void }) {
  const key = worktreesKey(projectId);
  const listing = useQuery<Listing>(key, { staleMs: 5000 });
  const offline = useOffline();
  const [busy, setBusy] = useState("");
  const [withBranch, setWithBranch] = useState<Record<string, boolean>>({});
  async function remove(tree: WorkerWorktree) {
    if (busy || offline || tree.blocked) return;
    // Only a merged branch may go with its worktree: it holds nothing the folder's branch lacks.
    // Any other branch stays, because it is the only copy of the member's work.
    const dropBranch = tree.merged && !!tree.branch && !!withBranch[tree.path];
    const body = dropBranch ? t("worktrees.remove.bodyBranch", { branch: tree.branch, base: tree.base })
      : tree.branch ? t("worktrees.remove.body", { branch: tree.branch }) : t("worktrees.remove.bodyDetached");
    if (!(await confirmAsync(t("worktrees.remove.title", { name: tree.name }), { body, action: t("worktrees.remove") }))) return;
    setBusy(tree.path);
    try {
      const done = await api.post<{ removed: boolean; branch_deleted: boolean }>(`${key}/remove`,
        { folder_id: tree.folder_id, path: tree.path, delete_branch: dropBranch });
      toast(t(done.branch_deleted ? "worktrees.removed.withBranch" : "worktrees.removed", { name: tree.name }));
    } catch (error) { toast(errorText(error)); }
    finally { setBusy(""); invalidate(key); }
  }
  if (listing.error && !listing.data) return <p className="sub attn">{errorText(listing.error)}</p>;
  if (!listing.data) return <p className="sub">{t("common.loading")}</p>;
  const { worktrees, problems } = listing.data;
  return <div className="worktree-list">
    {!worktrees.length && <p className="sub">{t("worktrees.empty")}</p>}
    {worktrees.map((tree) => (
      <div key={tree.path} className="worktree-row" data-worktree={tree.name}>
        <div className="worktree-head">
          <span className="grow truncate">{tree.member ? tree.member.name : tree.name}</span>
          {tree.member?.dismissed && <span className="badge">{t("worktrees.dismissed")}</span>}
          {tree.live && <span className="badge attn">{t("worktrees.live")}</span>}
          {tree.merged && tree.branch && <span className="badge">{t("worktrees.merged")}</span>}
          <button className="btn ghost small" disabled={!!tree.blocked || !!busy || offline} onClick={() => void remove(tree)}
            title={tree.blocked ? t(`worktrees.blocked.${tree.blocked}`, { name: tree.live?.name ?? "" }) : undefined}>{t("worktrees.remove")}</button>
        </div>
        <div className="sub mono truncate">{tree.branch || t("worktrees.detached")}</div>
        <div className="sub">{[
          tree.prunable ? t("worktrees.prunable") : tree.size_bytes != null ? bytes(tree.size_bytes) : "",
          tree.prunable ? "" : tree.changes ? plural("worktrees.changes", tree.changes) : t("worktrees.clean"),
          tree.last_commit_at ? t("worktrees.lastCommit", { when: relTime(tree.last_commit_at) }) : "",
        ].filter(Boolean).join(" · ")}</div>
        {tree.merged && tree.branch && !tree.blocked && <label className="toggle-row">
          <input type="checkbox" checked={!!withBranch[tree.path]} disabled={!!busy}
            onChange={(event) => setWithBranch((current) => ({ ...current, [tree.path]: event.target.checked }))} />
          <span>{t("worktrees.withBranch", { base: tree.base })}</span>
        </label>}
        {tree.blocked && <div className="sub attn">{t(`worktrees.blocked.${tree.blocked}`, { name: tree.live?.name ?? "" })}</div>}
      </div>
    ))}
    {problems.map((problem) => <p key={problem.folder_id} className="sub attn">{t("worktrees.problem", { reason: problem.reason })}</p>)}
  </div>;
}
