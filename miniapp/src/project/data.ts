// What focus mode reads about a project, under the same keys the team page and the board use, so a
// member hired in the sidebar shows on the team page and a task moved on the board moves in the panel
// without a second request. One subscription to the event stream keeps all of it current.

import type { Project, SessionList, TerminalList, Wakeup, WatchList } from "../api";
import type { ProjectBoardData } from "../board/board";
import { useEvent, useStreamUp } from "../events";
import { useProjects } from "../projects";
import { invalidate, useQuery } from "../store";
import type { Team } from "../team/team";
import { isFocusState, type FocusState } from "./goalmodel";
import { ProjectUsage, usageKey } from "./usage";

const enc = encodeURIComponent;

export const staffKey = (projectId: string) => `/api/projects/${enc(projectId)}/staff?archived=0`;
export const boardKey = (projectId: string) => `/api/projects/${enc(projectId)}/board?include_done=0`;
export const terminalsKey = (projectId: string) => `/api/terminals?project_id=${enc(projectId)}`;
export const briefKey = (projectId: string) => `/api/projects/${enc(projectId)}/brief`;
export const journalKey = (projectId: string) => `/api/projects/${enc(projectId)}/journal`;
export const wakeupsKey = (projectId: string) => `/api/projects/${enc(projectId)}/wakeups`;
export const watchesKey = (projectId: string) => `/api/projects/${enc(projectId)}/watches`;
export const focusStateKey = (projectId: string) => `/api/projects/${enc(projectId)}/focus-state`;

type Board = ProjectBoardData & { project: { id: string; name: string } };

/** The project itself, from the list the shell already holds. */
export function useProject(projectId: string): { project: Project | null; loading: boolean } {
  const projects = useProjects();
  return { project: projects.data?.find((p) => p.id === projectId) ?? null, loading: !projects.data && !projects.error };
}

/** Everything the sidebar draws: the team with its queue, the board's counts, the terminals, and the
 *  orchestrator's row in the agents listing. A request that fails leaves its part out rather than the
 *  sidebar: a project with no terminals service simply has no Terminals section. */
export function useFocus(projectId: string) {
  // Polls are the net under the stream: slow while it is up, as the other lists do.
  const live = useStreamUp();
  const team = useQuery<Team>(staffKey(projectId), { pollMs: live ? 60000 : 15000, staleMs: 5000 });
  const board = useQuery<Board>(boardKey(projectId), { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  const terminals = useQuery<TerminalList>(terminalsKey(projectId), { pollMs: live ? 60000 : 10000, staleMs: 5000 });
  const sessions = useQuery<SessionList>("/api/sessions?view=all", { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  const wakeups = useQuery<{ wakeups: Wakeup[] }>(wakeupsKey(projectId), { pollMs: live ? 120000 : 30000, staleMs: 15000 });
  const watches = useQuery<WatchList>(watchesKey(projectId), { pollMs: live ? 120000 : 30000, staleMs: 15000 });
  useProjectEvents(projectId);
  // An answer of the wrong shape (a proxy's page, an older host) is no answer: the column draws
  // without that part rather than taking the shell down with it.
  return {
    team: team.data && Array.isArray(team.data.staff) ? team.data : null,
    board: board.data && Array.isArray(board.data.tasks) ? board.data : null,
    terminals: terminals.data && Array.isArray(terminals.data.terminals) ? terminals.data : null,
    sessions: sessions.data ?? null,
    wakeups: wakeups.data && Array.isArray(wakeups.data.wakeups) ? wakeups.data.wakeups : [],
    watches: watches.data && Array.isArray(watches.data.watches) ? watches.data.watches : [],
  };
}

/** Read the project's parts again when its events say they changed. */
export function useProjectEvents(projectId: string): void {
  useEvent(["staff.", "task.", "ask.", "permission.", "project.changed", "terminal.", "schedule.fired", "watch.fired"], (event) => {
    if (event.project_id && event.project_id !== projectId) return;
    if (event.type === "schedule.fired" || event.type === "watch.fired") {
      // A one-off that fired is done, a recurring one moved on, and a watch counted a fire.
      invalidate(wakeupsKey(projectId));
      invalidate(watchesKey(projectId));
      return;
    }
    if (event.type.startsWith("terminal.")) {
      invalidate(terminalsKey(projectId));
      return;
    }
    invalidate(`/api/projects/${enc(projectId)}/staff`);
    invalidate(`/api/projects/${enc(projectId)}/board`);
    if (event.type.startsWith("ask.") || event.type.startsWith("permission.")) invalidate(`/api/asks?project=${enc(projectId)}`);
    if (event.type === "project.changed") {
      invalidate("/api/projects");
      invalidate(`/api/projects/${enc(projectId)}/brief`);
      invalidate(`/api/projects/${enc(projectId)}/journal`);
      invalidate(wakeupsKey(projectId));
      invalidate(watchesKey(projectId));
    }
  }, [projectId]);
}

/** What the project spends. Spend moves with every model call, so it is read again when a run of the
 *  project ends and otherwise once a minute. */
export function useUsage(projectId: string) {
  const { data } = useQuery<ProjectUsage>(usageKey(projectId), { pollMs: 60000, staleMs: 10000 });
  useEvent(["run.finished", "staff.status"], (event) => {
    if (event.project_id === projectId) invalidate(usageKey(projectId));
  }, [projectId]);
  return data && data.total ? data : null;
}

/** The project as the orchestrator's chat reads it: the counts over the composer, the list they open,
 *  and what became of each of the operator's messages. Read again whenever something it is built from
 *  moves — a card, a report, a request, an open result — and slowly otherwise, as the net under the stream. */
export function useFocusState(projectId: string | null): FocusState | null {
  const live = useStreamUp();
  const { data } = useQuery<FocusState>(projectId ? focusStateKey(projectId) : null, { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  useEvent(["project.changed", "task.", "staff.report", "orchestrator.open_results", "ask.", "run.finished"], (event) => {
    if (!projectId || (event.project_id && event.project_id !== projectId)) return;
    // Every chat's runs end on this stream; only the orchestrator's own, which carry the project, count.
    if (event.type === "run.finished" && event.project_id !== projectId) return;
    invalidate(focusStateKey(projectId));
  }, [projectId]);
  return isFocusState(data) ? data : null;
}
