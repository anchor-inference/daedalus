// Moving a folder between the container and the host: a chat's folder or a project's.
//
// The dialog asks the server what the move would be before anything happens — where the folder would
// go, and what stops it now (a chat at work, a terminal open in it, a host daemon too old) — so the
// operator reads the reason before pressing anything rather than after a copy half-done. Out of the
// container the destination is proposed (~/Daedalus/projects/<name>) and may be typed or chosen in the
// host's folder browser; into the container it goes where a new container folder would. The move then
// runs on the server; the dialog follows it and may be closed and opened again, which finds the move
// still running — after a reload too, and the folder's row and the chat show it meanwhile
// (`moves.tsx`). Before anything starts the plan lists what will not be carried — links out of the
// folder, mostly a tool cache's — and says why, so it is read before the move, not after. A move a
// restart interrupted is started again from here; the partial copy it left is removed first.
// Natively there is no container and nothing here is offered.

import { useEffect, useState } from "react";
import { api, type FolderMoveJob, type FolderMovePlan, type ProjectDir, type ProjectEnvironments } from "../api";
import { bytes } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { invalidate, useQuery } from "../store";
import { MOVES_KEY, useFolderMove } from "./moves";
import { errorText } from "../ui";
import { Sheet } from "../ui/dialogs";
import { FolderBrowser, type FolderChoice } from "./FolderBrowser";
import "./newproject.css";

/** Whether a move is offered for the folder: in a Docker installation, for a folder on the host or
 *  one the installation made in the container. A container folder mounted from the host is on the
 *  host already, and natively there is no container to move anything into. */
export function movable(folder: Pick<ProjectDir, "env" | "managed">, environments?: ProjectEnvironments | null): boolean {
  if (!environments?.docker) return false;
  return folder.env === "host" || (folder.env === "container" && folder.managed);
}

const STATES: Record<FolderMoveJob["state"], string> = {
  preparing: "move.state.preparing", copying: "move.state.copying", verifying: "move.state.verifying",
  switching: "move.state.switching", cleaning: "move.state.cleaning", done: "move.state.done",
  failed: "move.state.failed", cancelled: "move.state.cancelled", interrupted: "move.state.interrupted",
};

/** The daemon's and the scan's reasons for leaving an entry out, in the reader's language. */
const REASONS: Record<string, string> = {
  "link outside the folder": "move.reason.link", "special file": "move.reason.special", denied: "move.reason.denied",
};

function reason(text: string): string {
  return REASONS[text] ? t(REASONS[text]) : text;
}

const CANCELLABLE: FolderMoveJob["state"][] = ["preparing", "copying", "verifying"];

function lastName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? "";
}

function joinPath(base: string, name: string): string {
  const sep = base.includes("\\") && !base.includes("/") ? "\\" : "/";
  return base.endsWith(sep) ? `${base}${name}` : `${base}${sep}${name}`;
}

/** The button a folder row and a chat's workspace section show. */
export function MoveFolderButton({ projectId, folder, toast }: { projectId: string; folder: ProjectDir; toast: (text: string) => void }) {
  const [open, setOpen] = useState(false);
  const environments = useQuery<ProjectEnvironments>("/api/project-environments", { staleMs: 60000 }).data;
  const running = useFolderMove(folder.id);
  if (!movable(folder, environments)) return null;
  return (
    <>
      <button type="button" className="btn small" data-move-folder={folder.id} onClick={() => setOpen(true)}>
        <Icon name="forward" size={13} />{running ? t("move.open") : t(folder.env === "container" ? "move.tohost" : "move.tocontainer")}
      </button>
      {open && <MoveFolderSheet projectId={projectId} folder={folder} onClose={() => setOpen(false)} toast={toast} />}
    </>
  );
}

export function MoveFolderSheet({ projectId, folder, onClose, toast }: { projectId: string; folder: ProjectDir; onClose: () => void; toast: (text: string) => void }) {
  const base = `/api/projects/${encodeURIComponent(projectId)}/folders/${encodeURIComponent(folder.id)}/move`;
  const environments = useQuery<ProjectEnvironments>("/api/project-environments", { staleMs: 60000 }).data;
  const [plan, setPlan] = useState<FolderMovePlan | null>(null);
  const [failure, setFailure] = useState("");
  const [destination, setDestination] = useState("");
  const [removeOriginal, setRemoveOriginal] = useState(false);
  const [browsing, setBrowsing] = useState(false);
  const [choice, setChoice] = useState<FolderChoice | null>(null);
  const [job, setJob] = useState<FolderMoveJob | null>(null);
  const [busy, setBusy] = useState(false);

  async function loadPlan() {
    try {
      const next = await api.get<FolderMovePlan>(base);
      setPlan(next);
      setDestination(next.destination);
      setFailure("");
      if (next.job) setJob(next.job);
    } catch (error) { setFailure(errorText(error)); }
  }

  useEffect(() => { void loadPlan(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [base]);

  // Followed once a second while it runs: a move of a large folder takes minutes, and the bar is the
  // only thing that says it has not stalled.
  useEffect(() => {
    if (!job?.active) return;
    const timer = window.setTimeout(async () => {
      try {
        const next = await api.get<FolderMoveJob>(`/api/folder-moves/${encodeURIComponent(job.id)}`);
        setJob(next);
        if (!next.active) {
          invalidate(MOVES_KEY);
          invalidate("/api/projects");
          invalidate("/api/sessions");
          if (next.state === "done") toast(t("move.done.toast", { path: next.destination }));
        }
      } catch { /* the next tick asks again */ }
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [job, toast]);

  function pickFolder(next: FolderChoice | null) {
    setChoice(next);
    if (next && plan) setDestination(joinPath(next.path, lastName(plan.destination) || lastName(folder.path)));
  }

  async function start() {
    if (!plan || busy) return;
    setBusy(true);
    try {
      const body = plan.direction === "to_host" ? { destination: destination.trim() } : { remove_original: removeOriginal };
      setJob(await api.post<FolderMoveJob>(base, body));
      invalidate(MOVES_KEY);
    } catch (error) { toast(errorText(error)); void loadPlan(); }
    finally { setBusy(false); }
  }

  async function cancel() {
    if (!job) return;
    try { setJob(await api.post<FolderMoveJob>(`/api/folder-moves/${encodeURIComponent(job.id)}/cancel`)); }
    catch (error) { toast(errorText(error)); }
  }

  const toHost = (plan?.direction || (folder.env === "container" ? "to_host" : "to_container")) === "to_host";
  const title = t(toHost ? "move.title.tohost" : "move.title.tocontainer");

  if (job) {
    const fraction = job.total_bytes > 0 ? Math.min(1, job.done_bytes / job.total_bytes) : job.active ? 0 : 1;
    return (
      <Sheet title={title} onClose={onClose} size="narrow">
        <div className="np move-folder" data-move-state={job.state}>
          <div className="np-field">
            <span className="np-label">{t("move.from")}</span><span className="mono truncate" title={job.source}>{job.source}</span>
            <span className="np-label">{t("move.to")}</span><span className="mono truncate" title={job.destination}>{job.destination}</span>
          </div>
          <div className="np-field">
            <span className="np-label">{t(STATES[job.state])}</span>
            {job.active && <progress max={1} value={fraction} aria-label={t(STATES[job.state])} />}
            {job.total_bytes > 0 && <span className="sub">{bytes(job.done_bytes)} / {bytes(job.total_bytes)} · {t("move.files", { n: job.total_files })}</span>}
          </div>
          {(job.state === "failed" || job.state === "interrupted") && <div className="np-error" role="alert"><Icon name="alert" size={14} />{job.error}</div>}
          {job.state === "cancelled" && <div className="sub" role="status">{t("move.cancelled.body")}</div>}
          {job.state === "done" && (
            <div className="sub" role="status">
              {t(job.original_removed ? "move.done.removed" : "move.done.kept", { path: job.source })}
            </div>
          )}
          {job.notes.length > 0 && <ul className="move-notes sub">{job.notes.map((note) => <li key={note}>{note}</li>)}</ul>}
          {!job.active && job.skipped.length > 0 && (
            <details className="sub">
              <summary>{t("move.skipped", { n: job.skipped_count })}</summary>
              <ul className="mono">{job.skipped.map((item) => <li key={item.path}>{item.path} — {reason(item.reason)}</li>)}</ul>
            </details>
          )}
          <div className="sheet-foot">
            {job.active && CANCELLABLE.includes(job.state) && <button type="button" className="btn ghost" onClick={() => void cancel()}>{t("move.cancel")}</button>}
            {!job.active && job.state !== "done" && <button type="button" className="btn ghost" data-move-again onClick={() => { setJob(null); void loadPlan(); }}>{t(job.state === "interrupted" ? "move.restart" : "move.again")}</button>}
            <button type="button" className="btn primary" onClick={onClose}>{t(job.active ? "move.background" : "common.close")}</button>
          </div>
        </div>
      </Sheet>
    );
  }

  const problem = plan?.problem ?? "";
  const ready = !!plan && !problem && (!toHost || !!destination.trim());
  const skipped = plan?.skipped ?? [];
  const skippedCount = plan?.skipped_count ?? 0;
  const leftoverHere = !!plan?.leftover && destination.trim() === plan.leftover.destination;
  return (
    <Sheet title={title} onClose={onClose} size={browsing ? "wide" : "narrow"}>
      <div className="np move-folder" data-move-plan={plan ? plan.direction || "none" : "loading"}>
        <p className="np-intro sub">{t(toHost ? "move.intro.tohost" : "move.intro.tocontainer")}</p>
        <div className="np-field">
          <span className="np-label">{t("move.from")}</span>
          <span className="mono truncate" title={folder.path}>{folder.path}</span>
        </div>
        {failure && <div className="np-error" role="alert"><Icon name="alert" size={14} />{failure}</div>}
        {!plan && !failure && <div className="sub">{t("common.loading")}</div>}
        {problem && (
          <div className="np-error" role="alert" data-move-problem={problem}>
            <Icon name="alert" size={14} />
            <span>{t(`move.problem.${problem}`)} {plan?.detail}</span>
          </div>
        )}
        {plan && plan.direction === "to_host" && (
          <div className="np-field">
            <label className="np-label" htmlFor="move-destination">{t("move.to")}</label>
            <div className="move-dest">
              <input id="move-destination" className="field mono" value={destination} spellCheck={false} disabled={!!problem && problem !== "busy"}
                onChange={(event) => setDestination(event.target.value)} placeholder={t("move.to.placeholder")} />
              <button type="button" className="btn small" aria-pressed={browsing} disabled={!!problem && problem !== "busy"} onClick={() => setBrowsing((value) => !value)}>
                <Icon name="folder" size={13} />{t("move.browse")}
              </button>
            </div>
            <span className="sub">{t("move.to.hint", { home: plan.host_home || "~" })}</span>
            {leftoverHere && <span className="sub attn" data-move-leftover>{t("move.leftover", { path: plan.leftover!.destination })}</span>}
            {browsing && <FolderBrowser environments={environments} only="host" choice={choice} onChoice={pickFolder} toast={toast} />}
          </div>
        )}
        {plan && plan.direction === "to_container" && (
          <>
            <div className="np-field">
              <span className="np-label">{t("move.to")}</span>
              <span className="mono truncate" title={plan.destination}>{plan.destination}</span>
            </div>
            <label className="toggle-row">
              <input type="checkbox" checked={removeOriginal} onChange={(event) => setRemoveOriginal(event.target.checked)} />
              <span>{t("move.remove")}</span>
              <span className="sub">{t("move.remove.hint")}</span>
            </label>
          </>
        )}
        {plan?.counts && <div className="sub" data-move-counts>{bytes(plan.counts.bytes)} · {t("move.files", { n: plan.counts.files })}</div>}
        {skippedCount > 0 && (
          // Said before the move, with what it means for removing the original, because a list of
          // paths alone reads as a fault to fix rather than what is meant to happen.
          <details className="move-skipped sub" data-move-skipped={skippedCount}>
            <summary>{plural("move.skip.title", skippedCount)}</summary>
            <p>{t(toHost ? "move.skip.tohost" : "move.skip.tocontainer")}</p>
            <ul className="mono">{skipped.map((item) => <li key={item.path}>{item.path} — {reason(item.reason)}</li>)}</ul>
          </details>
        )}
        <div className="sheet-foot">
          <button type="button" className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
          <button type="button" className="btn primary" disabled={!ready || busy} onClick={() => void start()}>{t(toHost ? "move.start.tohost" : "move.start.tocontainer")}</button>
        </div>
      </div>
    </Sheet>
  );
}
