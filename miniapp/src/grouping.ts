// How the Agents screen is arranged: one folder for every project and its agents.
//
// All of it is pure, and that is the point — the screen renders what these functions return, and a
// test can ask what the arrangement is without a browser. Nesting (a subagent under its leader, a
// fork under the session it was taken from) is decided here too, so one pass over the rows answers
// both questions and the screen does no bookkeeping of its own.

import { ProjectFolder, ProjectRef, SessionSummary } from "./api";

export type Filter = "all" | "working" | "loops";
export type Kind = "waiting" | "working" | "loop" | "idle";

/** One agent and what hangs off it: its subagents, and the forks taken from it (each with its own). */
export type Row = { s: SessionSummary; kids: SessionSummary[]; forks: Row[] };

/** A project's folder in the list. */
export type Folder = {
  key: string;
  name: string;
  project: ProjectFolder;
  /** True on the installation's own project — the concierge's — which is drawn with a mic. */
  system: boolean;
  /** True on a chat's own scratch project: it is listed as the chat, not as a project (`isProject`). */
  chat: boolean;
  /** When it was pinned to the top of the sidebar; empty when it is not. A chat's pin is its
   *  scratch project's, so a chat that becomes a project is still pinned. */
  pinned_at: string;
  rows: Row[];
  /** What the rows of this folder would look different for, built once while they are arranged.
   *  The listing is re-fetched whole every few seconds and every object in it is new, so a folder
   *  that did not change has nothing but this to prove it by. */
  sig: string;
  /** Counted over the whole table by the API, not over the rows above: a folder says how many agents
   *  are in it even where the page of rows did not reach them all. */
  total: number;
  active: number;
  loops: number;
  last_message_at: string;
};

/**
 * Whether a project is listed as a project or as the chat it was made for.
 *
 * Every chat gets a project of its own; the server marks it `ephemeral` and clears the mark for
 * good once a second top-level chat lives in it, and a project made by hand never carries it. The
 * installation's own project (Voice) is a project whatever it holds. The phone's Chats page and the
 * desktop's sidebar both split by this one rule, so a chat never sits in "Projects" on one screen
 * and among the chats on the other.
 */
export function isProject(p: Pick<ProjectRef, "settings" | "system">): boolean {
  return !!(p.settings.system || p.system) || !p.settings.ephemeral;
}

const OPEN_PREFIX = "daedalus.folder.";

/** Whether a folder is expanded. Collapsed is the default: a folder that opens itself is a list again. */
export function folderOpen(key: string): boolean {
  try {
    return localStorage.getItem(OPEN_PREFIX + key) === "1";
  } catch {
    return false;
  }
}

export function rememberFolder(key: string, open: boolean): void {
  try {
    if (open) localStorage.setItem(OPEN_PREFIX + key, "1");
    else localStorage.removeItem(OPEN_PREFIX + key);
  } catch {
    /* private mode: the folder just forgets between visits */
  }
}

/** The day group a chat falls in, by its last message: today, yesterday, the week, the month, older. */
export function dayGroup(iso: string, now = new Date()): "today" | "yesterday" | "week" | "month" | "older" {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "older";
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 86_400_000;
  if (at.getTime() >= start) return "today";
  if (at.getTime() >= start - day) return "yesterday";
  if (at.getTime() >= start - 7 * day) return "week";
  if (at.getTime() >= start - 30 * day) return "month";
  return "older";
}

/** The desktop's day groups: the phone's five less the month, which the narrow column folds into
 *  "Earlier" — four headers are already a quarter of what the column shows between them. */
export const SIDEBAR_DAYS = ["today", "yesterday", "week", "older"] as const;
export type SidebarDay = typeof SIDEBAR_DAYS[number];

/** A row whose chat asks for the operator, works or has failed: what a collapsed project still shows. */
export function liveRow(r: Row): boolean {
  const live = (s: SessionSummary) => s.status === "waiting" || s.status === "running" || s.status === "failed" || s.status === "compacting";
  return live(r.s) || r.kids.some(live);
}

/**
 * The pinned projects and chats, newest pin first, and every other folder in its order.
 *
 * A pinned folder is listed in the pinned block alone, never again below it. It obeys the same
 * filter as the rest: a pinned chat is there while its row is (an archived or deleted chat has none),
 * and a pinned project under a filter or a search only while something in it answers. `pins: false`
 * pins nothing, for a list that is not the everyday one (the archive).
 */
export function splitPinned(folders: Folder[], opts: { filtered?: boolean; pins?: boolean } = {}): { pinned: Folder[]; rest: Folder[] } {
  if (opts.pins === false) return { pinned: [], rest: folders };
  const pinned = folders.filter((f) => f.pinned_at)
    .filter((f) => (f.chat || opts.filtered ? f.rows.length > 0 : true))
    .sort((a, b) => b.pinned_at.localeCompare(a.pinned_at) || a.key.localeCompare(b.key));
  return { pinned, rest: folders.filter((f) => !f.pinned_at) };
}

/**
 * The desktop sidebar's sections: the pinned block, the projects, and the chats by day.
 *
 * Every project is listed, an empty one included, since a project made by hand is a project before
 * its first chat; under a filter or a search only those with something in the answer are, or the
 * section would answer with a list of names that hold nothing. A chat's forks and subagents travel
 * with its row. The chats are in recency order across their scratch projects. What is pinned is in
 * `pinned` and in neither of the other two (`splitPinned`).
 */
export function sidebarSections(all: Folder[], opts: { filtered?: boolean; now?: Date; pins?: boolean } = {}): { pinned: Folder[]; projects: Folder[]; days: { day: SidebarDay; rows: Row[] }[]; chats: number } {
  const now = opts.now ?? new Date();
  const { pinned, rest: folders } = splitPinned(all, opts);
  const projects = folders.filter((f) => !f.chat && (!opts.filtered || f.rows.length > 0));
  const loose = folders.filter((f) => f.chat).flatMap((f) => f.rows)
    .sort((a, b) => activity(b.s) - activity(a.s) || a.s.id.localeCompare(b.s.id));
  const bucket = (r: Row): SidebarDay => {
    const day = dayGroup(r.s.last_message_at || r.s.created_at, now);
    return day === "month" ? "older" : day;
  };
  const days = SIDEBAR_DAYS.map((day) => ({ day, rows: loose.filter((r) => bucket(r) === day) })).filter((d) => d.rows.length > 0);
  return { pinned, projects, days, chats: loose.length };
}

/** What the status chips count and what the Active filter keeps. */
export function kindOf(s: SessionSummary, childRunning: boolean): Kind {
  if (s.status === "waiting") return "waiting";
  if (s.status === "running" || childRunning) return "working";
  if (s.metadata?.loop && s.metadata.loop.status === "active") return "loop";
  return "idle";
}

/** The name a subagent is listed under: the one its leader gave it, not the `[sub]` title. */
export function agentName(s: SessionSummary): string {
  return s.metadata?.subagent_of ? s.metadata?.subagent_name || s.title.replace(/^\[sub\]\s*/, "") : s.title;
}

export type Arranged = {
  folders: Folder[];
  /** Every top-level agent that survived the filter and the search, for the header's counts. */
  shown: number;
  /** Top-level agents before either, which is what "All · n" means. */
  total: number;
  active: number;
  loops: number;
};

function activity(s: SessionSummary): number {
  return Date.parse(s.last_message_at || s.created_at) || 0;
}

/** What the folder's memo compares: a change that is not in here never reaches the rows. The unread
 *  mark is in it because a run that finishes unseen changes nothing else a row shows. */
function rowSig(rows: Row[]): string {
  return rows.map((r) => `${r.s.id}:${r.s.status}:${r.s.unread_result ? "u" : ""}:${r.s.last_message_at}:${r.s.title}:${r.s.model ?? ""}:${r.s.match?.snippet ?? ""}:${r.kids.map((k) => k.id + k.status).join(",")}|${rowSig(r.forks)}`).join(";");
}

/**
 * Arrange the listing.
 *
 * `project` narrows the whole screen to one project (the shell's switcher); `filter` and `query`
 * apply inside every folder because a reader who filters wants the answer across the screen. What
 * belongs to orchestration mode is taken out before this is called (`agentsListing` in mode.ts).
 */
export function arrange(
  sessions: SessionSummary[],
  projects: ProjectFolder[],
  opts: { project?: string; filter?: Filter; query?: string; results?: boolean } = {},
): Arranged {
  const { project = "", filter = "all", query = "" } = opts;
  const all = (project ? sessions.filter((s) => s.project_id === project) : [...sessions]).sort((a, b) =>
    opts.results ? (b.match?.score ?? 0) - (a.match?.score ?? 0) : activity(b) - activity(a) || a.id.localeCompare(b.id));
  const ids = new Set(all.map((s) => s.id));

  // A subagent sits under its leader; one whose leader is gone is listed on its own.
  const children = new Map<string, SessionSummary[]>();
  for (const s of all) {
    const leader = s.metadata?.subagent_of;
    if (!opts.results && leader && ids.has(leader)) children.set(leader, [...(children.get(leader) ?? []), s]);
  }
  // A fork sits under the session it was taken from: it has a copy of that session's files as of the
  // fork point, and the kinship is what the reader is looking for. A fork whose origin is gone is listed on its own.
  const forks = new Map<string, SessionSummary[]>();
  for (const s of all) {
    const origin = s.metadata?.forked_from?.session_id;
    if (!opts.results && origin && origin !== s.id && ids.has(origin) && !s.metadata?.subagent_of) forks.set(origin, [...(forks.get(origin) ?? []), s]);
  }
  const nested = (s: SessionSummary) => !opts.results && ((!!s.metadata?.subagent_of && ids.has(s.metadata.subagent_of)) || (!!s.metadata?.forked_from?.session_id && ids.has(s.metadata.forked_from!.session_id) && !s.metadata?.subagent_of));

  const q = query.trim().toLowerCase();
  const hits = (s: SessionSummary) => !q || agentName(s).toLowerCase().includes(q) || (s.model ?? "").toLowerCase().includes(q) || (children.get(s.id) ?? []).some((c) => agentName(c).toLowerCase().includes(q));
  // A fork that matches keeps its parent on screen, or the row it hangs under would be gone with it.
  const matches = (s: SessionSummary) => hits(s) || (forks.get(s.id) ?? []).some(hits);
  const childRunning = (s: SessionSummary) => (children.get(s.id) ?? []).some((c) => c.status === "running" || c.status === "waiting");
  const kind = (s: SessionSummary) => kindOf(s, childRunning(s));
  const keeps = (s: SessionSummary) => (filter === "all" ? true : filter === "loops" ? kind(s) === "loop" : kind(s) === "waiting" || kind(s) === "working");

  const top = all.filter((s) => !nested(s));
  const kept = top.filter((s) => matches(s) && keeps(s));
  const row = (s: SessionSummary): Row => ({
    s,
    kids: children.get(s.id) ?? [],
    forks: (forks.get(s.id) ?? []).filter((f) => !q || hits(f)).map(row),
  });

  const byProject = new Map<string, Row[]>();
  for (const s of kept) {
    const key = s.project_id;
    if (!projects.some((p) => p.id === key)) continue;
    byProject.set(key, [...(byProject.get(key) ?? []), row(s)]);
  }

  // Empty projects use their creation time; Voice follows the same recency order as every project.
  const folders: Folder[] = projects
    .filter((p) => !project || p.id === project)
    .map((p) => ({
      key: p.id,
      name: p.name,
      project: p,
      system: !!p.system,
      chat: !isProject(p),
      pinned_at: p.pinned_at ?? "",
      rows: byProject.get(p.id) ?? [],
      sig: rowSig(byProject.get(p.id) ?? []),
      total: p.total,
      active: p.active,
      loops: p.loops,
      last_message_at: p.last_message_at || all.filter((s) => s.project_id === p.id).sort((a, b) => activity(b) - activity(a))[0]?.last_message_at || "",
    }))
    .sort((a, b) => Date.parse(b.last_message_at || b.project.created_at) - Date.parse(a.last_message_at || a.project.created_at) || a.key.localeCompare(b.key));
  return {
    folders,
    shown: kept.length,
    total: top.length,
    active: top.filter((s) => kind(s) === "waiting" || kind(s) === "working").length,
    loops: top.filter((s) => kind(s) === "loop").length,
  };
}
