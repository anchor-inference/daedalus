// The two modes of the app: Agents, the chats and projects the operator works in directly, and
// Orchestration, the main orchestrator and the projects that have an orchestrator of their own. The
// operator asked for them apart: nothing of one is drawn in the other's lists, and the two mode items
// of the rail (the first two tabs on a phone) are the only place where one mode speaks of the other —
// as a count of what waits there.
//
// Everything here is pure, so which session belongs where, what the switch counts and where a link
// lands are answered by tests without a browser.

import type { MainView, ProjectFolder, ProjectRef, SessionSummary } from "./api";
import { ORCHESTRATION, ORCHESTRATION_LIST, projectHome, projectSessionPath, type Route } from "./router";

export type Mode = "agents" | "orchestration";

const KEY = "daedalus.mode";

/** The mode a route belongs to, or null for a screen both modes share (Terminals, Settings, Inbox…),
 *  which then keeps the column of the mode the operator was in. */
export function modeOf(route: Pick<Route, "screen">): Mode | null {
  if (route.screen === "orchestration") return "orchestration";
  if (route.screen === "agents") return "agents";
  return null;
}

/** The mode this device was last in: a phone and a desktop each keep their own. */
export function storedMode(): Mode {
  try {
    return localStorage.getItem(KEY) === "orchestration" ? "orchestration" : "agents";
  } catch {
    return "agents";
  }
}

export function rememberMode(mode: Mode): void {
  try {
    localStorage.setItem(KEY, mode);
  } catch {
    /* private mode: the device forgets between visits and opens on Agents */
  }
}

/** Where switching into a mode lands: the start canvas, or orchestration's home. On a desktop that
 *  is the main orchestrator's chat, with the list of projects in the column beside it; a phone has no
 *  column, so there it is the list — Main first, then the projects — and the main chat is one tap in. */
export function modeHome(mode: Mode, wide = true): string {
  if (mode === "agents") return "/app/agents";
  return wide ? ORCHESTRATION : ORCHESTRATION_LIST;
}

/** Whether a project belongs to orchestration mode: its orchestrator is switched on. A project whose
 *  orchestrator is switched off again is an ordinary project and goes back to the Agents list. */
export function orchestrated(project: Pick<ProjectFolder, "orchestrator"> | undefined | null): boolean {
  return !!project?.orchestrator?.enabled;
}

/** The main orchestrator's own system project, which holds its chat and the chats it replaced. */
export function isMainProject(project: { system?: string } | undefined | null): boolean {
  return project?.system === "dispatcher";
}

/**
 * The listing as Agents mode shows it: without the orchestrated projects, anything working in them
 * (the orchestrator, the staff, a chat opened there before), and without the main orchestrator's chats.
 * What remains is the list exactly as it was before orchestration existed.
 */
export function agentsListing<T extends { sessions: SessionSummary[]; projects: ProjectFolder[] }>(listing: T): T {
  // An archived project leaves the list with everything in it; restoring it brings them back.
  const away = new Set(listing.projects.filter((p) => orchestrated(p) || isMainProject(p) || p.settings?.archived).map((p) => p.id));
  if (away.size === 0 && !listing.sessions.some((s) => s.metadata?.dispatcher)) return listing;
  return {
    ...listing,
    sessions: listing.sessions.filter((s) => !away.has(s.project_id) && !s.metadata?.dispatcher),
    projects: listing.projects.filter((p) => !away.has(p.id)),
  };
}

/** The projects Orchestration mode lists under Main: those with an orchestrator, most recent first. */
export function orchestratedProjects(projects: ProjectFolder[]): ProjectFolder[] {
  return projects
    .filter((p) => orchestrated(p) && !isMainProject(p) && !p.settings?.archived)
    .sort((a, b) => Date.parse(b.last_message_at || b.created_at) - Date.parse(a.last_message_at || a.created_at) || a.id.localeCompare(b.id));
}

/**
 * How many requests wait for the operator in orchestration mode: the ones routed to the operator in
 * every orchestrated project, and the main chat's own that belong to no such project (a project it
 * proposes to create has none yet). A request the main chat mirrors from a project is that project's
 * request, and is counted once, there.
 */
export function waitingInOrchestration(projects: ProjectFolder[], main: MainView | null): number {
  const counted = new Set(projects.filter((p) => orchestrated(p) && !p.settings?.archived).map((p) => p.id));
  let waiting = 0;
  for (const p of projects) if (counted.has(p.id)) waiting += Math.max(0, p.orchestrator?.needs_you ?? 0);
  for (const ask of main?.asks ?? []) {
    if (ask.resolved_at || ask.routed_to !== "operator") continue;
    if (ask.project_id && counted.has(ask.project_id)) continue;
    waiting += 1;
  }
  return waiting;
}

/**
 * Where a session opened by its plain address (/app/agents/<id>: an old link, a notification, a
 * Telegram message, the palette) really lives, when that is in orchestration mode; null when it is an
 * ordinary chat and stays where it is.
 *
 * The current main chat is orchestration's home. A session of a project with an orchestrator — the
 * orchestrator itself, a staff member's, any other chat there — opens inside that project. A main
 * chat that was replaced stays an ordinary conversation: it is history, not the place questions wait.
 */
export function orchestrationPathOf(session: { id: string; project: Pick<ProjectRef, "id" | "system" | "settings"> }, mainSession: string | null | undefined): string | null {
  if (mainSession && session.id === mainSession) return ORCHESTRATION;
  const project = session.project;
  const orchestrator = project.settings?.orchestrator;
  if (!orchestrator?.enabled || isMainProject(project)) return null;
  return orchestrator.session_id === session.id ? projectHome(project.id) : projectSessionPath(project.id, session.id);
}
