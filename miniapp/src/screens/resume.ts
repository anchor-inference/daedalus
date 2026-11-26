// The chats the start page offers to pick up again, and what each one says about itself: a pure
// ordering and a state, kept apart from the page so the order is tested without drawing it.

import type { SessionSummary } from "../api";
import { fmtInterval } from "../ui/components";
import { shortModel, untilShort } from "../format";
import { t } from "../i18n";

export type ResumeState = "waiting" | "failed" | "working" | "done" | "loop" | "idle";

/** What a chat is in, for the dot and the line under its title. A finished chat whose result nobody
 *  has opened reads "done"; one already read is only idle. */
export function resumeState(s: SessionSummary): ResumeState {
  if (s.status === "waiting") return "waiting";
  if (s.status === "failed") return "failed";
  if (s.status === "running" || s.status === "compacting") return "working";
  if (s.unread_result) return "done";
  if (s.metadata?.loop && s.metadata.loop.status === "active") return "loop";
  return "idle";
}

const RANK: Record<ResumeState, number> = { waiting: 0, failed: 1, working: 2, done: 3, loop: 4, idle: 5 };

/** The top-level chats, the ones that need the operator first, then the failed and the working, then what
 *  finished unread, the loops, and the rest by how lately they were used; the helpers of a chat and the
 *  archived are not offered. `project` narrows to one project's chats. */
export function resumeOrder(sessions: SessionSummary[], project: string, limit: number): { s: SessionSummary; state: ResumeState }[] {
  return sessions
    .filter((s) => !s.metadata?.subagent_of && !s.archived && (!project || s.project_id === project))
    .map((s) => ({ s, state: resumeState(s) }))
    .sort((a, b) => RANK[a.state] - RANK[b.state] || Date.parse(b.s.last_message_at) - Date.parse(a.s.last_message_at))
    .slice(0, limit);
}

/** The line under a chat's title: its state in words, then what the list knows of it (a loop's cadence,
 *  the model). The listing carries no text of the last turn, so none is invented. */
export function resumeNote(s: SessionSummary, state: ResumeState): string {
  const loop = s.metadata?.loop;
  const words = state === "waiting" ? t("start.resume.waiting")
    : state === "failed" ? t("start.resume.failed")
    : state === "working" ? t("start.resume.working")
    : state === "done" ? t("start.resume.done")
    : state === "loop" && loop ? [loop.mode === "interval" ? t("loop.every", { t: fmtInterval(loop.interval_seconds) }) : t("loop.selfpaced"), loop.next_run_at ? t("ph.loop.next", { t: untilShort(loop.next_run_at) }) : ""].filter(Boolean).join(" · ")
    : "";
  return [words, s.model ? shortModel(s.model, 28) : ""].filter(Boolean).join(" · ");
}
