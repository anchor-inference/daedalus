// A project's team as the app draws it: the shapes the host sends, and the few decisions the page
// makes from them — which executor can be chosen and why not, what a status is called, what branch a
// worktree will be given. Pure, so each one is tested without a browser.

import type { ChannelHealth } from "../api";

export const HARNESSES = ["daedalus", "claude", "codex", "grok", "opencode", "pi", "cursor"] as const;
export type Harness = (typeof HARNESSES)[number];

/** The two letters on a staff member's badge, the way the team page and the hiring form show an executor. */
export const HARNESS_BADGES: Record<Harness, string> = { daedalus: "D", claude: "CC", codex: "CX", grok: "GK", opencode: "OC", pi: "π", cursor: "CU" };

/** Each executor's own name. Product names: the same in every language, so they are not in the dictionary. */
export const HARNESS_NAMES: Record<Harness, string> = { daedalus: "Daedalus", claude: "Claude Code", codex: "Codex", grok: "Grok Build", opencode: "OpenCode", pi: "pi", cursor: "Cursor Agent" };

/** Every status a staff member can show: the live session's, or "off" when there is none. */
export const STAFF_STATUSES = ["off", "starting", "working", "turn_done_unseen", "idle", "question", "permission", "error", "exited", "no_signal"] as const;
export type StaffStatus = (typeof STAFF_STATUSES)[number];

export const ISOLATIONS = ["worktree", "shared", "readonly"] as const;
export type Isolation = (typeof ISOLATIONS)[number];

/** The colours the host hands out, by the name of the stylesheet token for each. */
export const STAFF_COLOURS = ["blue", "green", "amber", "violet", "rose", "teal", "orange", "slate"] as const;

export type Env = "container" | "host";

export type StaffSession = {
  id: string;
  /** The Daedalus session a Daedalus member works in; a command-line member has none. */
  session_id?: string | null;
  terminal_id?: string | null;
  pause_requested?: boolean;
  status: Exclude<StaffStatus, "off">;
  waiting_for: string;
  task_id: string | null;
  started_at: string;
  ended_at: string | null;
  branch: string | null;
  worktree_path: string | null;
};

/** A launch waiting in the project's queue, with the reason the host gives (daedalus/host/launch_queue.py):
 *  a code the app has words for, and a sentence of the host's own with the numbers in it. */
export type Queued = { staff_id: string; task_id: string; priority: number; position: number; reason: string | null; detail: string; since: number; by: string };

export type Staff = {
  id: string;
  project_id: string;
  name: string;
  color: string;
  role: string;
  harness: Harness;
  agent: string;
  model: string;
  effort: string;
  permission_mode: string;
  env: "" | Env;
  default_folder_id: string | null;
  isolation: Isolation;
  instructions: string;
  notes: string;
  one_off: boolean;
  created_by: "operator" | "orchestrator";
  created_at: string;
  archived_at: string | null;
  live: StaffSession | null;
  status: StaffStatus;
  sessions: number;
  /** What the member waits to start, each with why. */
  queued?: Queued[];
  /** Whether the host still hears the member's live session; absent without one. */
  health?: ChannelHealth | null;
};

export type TeamFolder = { id: string; path: string; label: string; env: Env; is_git: boolean; readonly: boolean };

export type Team = {
  project: {
    id: string;
    name: string;
    ephemeral: boolean;
    system: string;
    default_env: Env;
    local_env: Env;
    concurrency: number;
    concurrency_cap: number;
    orchestrator: boolean;
    folders: TeamFolder[];
  };
  staff: Staff[];
  /** The project's whole launch queue, in the order it would start. */
  queue?: Queued[];
  counts: { staff: number; working: number };
  choices: { harnesses: Harness[]; personas: string[]; presets: { id: string; label: string }[]; default_preset: string; coordinator_default_preset: string };
};

/** What the harness manager knows about one command-line agent. Every field may be missing: the
 *  catalog belongs to another part of the installation, and absence reads as "not installed". */
export type CatalogEntry = {
  installed?: boolean;
  version?: string;
  latest?: string;
  logged_in?: boolean;
  agents?: { name: string; source?: string }[];
  /** The models the operator chose to offer, or every model when there is no choice. */
  models?: string[];
  /** Every model the CLI listed: the ones not offered are still hired with when named. */
  all_models?: string[];
  models_chosen?: boolean;
  error?: string;
  checked_at?: string;
  /** The installed version against the adapter's tested range: "" inside it, "verified" outside but
   *  self-checked on this very version, "unverified" outside and not yet self-checked. */
  version_guard?: "" | "verified" | "unverified";
  tested_versions?: [string, string];
};
export type Catalog = Partial<Record<Harness, CatalogEntry>>;

/** The hiring form's model list: the offered models first, and — only when the operator chose some —
 *  every other model the CLI listed after them, because a choice shortens the list and never takes a
 *  model away. */
export function modelGroups(entry: Pick<CatalogEntry, "models" | "all_models" | "models_chosen"> | null | undefined): { offered: string[]; others: string[] } {
  const offered = entry?.models ?? [];
  const others = entry?.models_chosen ? (entry.all_models ?? []).filter((m) => !offered.includes(m)) : [];
  return { offered, others };
}

export type Unavailable = "" | "notinstalled" | "loggedout" | "error";

/** Whether an executor can be hired in an environment, and if not, why — as a dictionary key suffix.
 *
 *  Daedalus is always there. A command-line agent needs the catalog to say it is installed and signed
 *  in; with no catalog at all (an installation without the harness manager) every one of them is
 *  "not installed", which is the truth for this installation. */
export function availability(harness: Harness, catalog: Catalog | null): Unavailable {
  if (harness === "daedalus") return "";
  const entry = catalog?.[harness];
  if (!entry || !entry.installed) return entry?.error ? "error" : "notinstalled";
  if (entry.logged_in === false) return "loggedout";
  return "";
}

/** Where an executor would run if hired now, and why it could not be.
 *
 *  The grid used to ask only the catalog of the environment the sheet was showing, which with
 *  Daedalus selected is Daedalus's own (the container): every command-line agent installed on the
 *  host then read as "not installed" until some other executor was picked. Each agent is judged
 *  where it would run, and one missing there but ready in the other environment is offered there.
 *  `undefined` catalogs are still loading. */
export function placeExecutor(
  harness: Harness,
  preferred: Env,
  catalogs: Record<Env, Catalog | null | undefined>,
): { env: Env; why: Unavailable; waiting: boolean } {
  const other: Env = preferred === "host" ? "container" : "host";
  const here = availability(harness, catalogs[preferred] ?? null);
  if (here === "" || harness === "daedalus") return { env: preferred, why: here, waiting: false };
  if (catalogs[other] !== undefined && availability(harness, catalogs[other] ?? null) === "") return { env: other, why: "", waiting: false };
  return { env: preferred, why: here, waiting: catalogs[preferred] === undefined || catalogs[other] === undefined };
}

const CYRILLIC_FROM = "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ";
const CYRILLIC_TO = ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g"];
const CYRILLIC: Record<string, string> = Object.fromEntries(Array.from(CYRILLIC_FROM).map((ch, i) => [ch, CYRILLIC_TO[i]]));

/** A name as it appears in a branch and a worktree folder.
 *
 *  The host's rule, step for step (`slug` in daedalus/host/worktrees.py), so the preview is the branch
 *  that will be made: Cyrillic transliterated, accents dropped, anything outside `[a-z0-9._-]` one
 *  dash, no `..`, at most `max` characters, no leading or trailing `.` or `-` and no trailing `.lock`.
 *  A name with nothing left becomes "staff". */
export function branchSlug(name: string, max = 32): string {
  let text = Array.from(name.toLowerCase()).map((ch) => CYRILLIC[ch] ?? ch).join("");
  text = text.normalize("NFKD").replace(/[^\x00-\x7f]/g, "");
  text = text.replace(/[^a-z0-9._-]+/g, "-").replace(/\.{2,}/g, ".").replace(/-{2,}/g, "-").slice(0, max);
  for (;;) {
    let trimmed = text.replace(/^[.-]+|[.-]+$/g, "");
    if (trimmed.endsWith(".lock")) trimmed = trimmed.slice(0, -".lock".length);
    if (trimmed === text) break;
    text = trimmed;
  }
  return text || "staff";
}

/** The branch a staff member in its own worktree works on, with the task left as a placeholder. */
export function branchPreview(name: string, task: string): string {
  return `agent/${branchSlug(name)}/${task}`;
}

/** The session-status class the dot is drawn with: the colours the rest of the app already uses for "running", "waiting" and "failed". */
export function statusTone(status: StaffStatus): "running" | "waiting" | "failed" | "done" | "idle" {
  if (status === "working" || status === "starting") return "running";
  // A finished turn nobody has read is something to look at: green like the member's dot in the
  // sidebar, not the grey of an idle member it used to share and be lost beside.
  if (status === "turn_done_unseen") return "done";
  if (status === "question" || status === "permission") return "waiting";
  if (status === "error") return "failed";
  return "idle";
}

/** The letters in a staff member's avatar: the first letter of the first two words, or of the name. */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length >= 2) return (Array.from(words[0])[0] + Array.from(words[1])[0]).toUpperCase();
  return Array.from(words[0] ?? "?").slice(0, 2).join("").toUpperCase() || "?";
}

/** The colour token a member wears; an unknown name falls back to the first rather than drawing nothing. */
export function colourVar(color: string): string {
  return `var(--staff-${(STAFF_COLOURS as readonly string[]).includes(color) ? color : STAFF_COLOURS[0]})`;
}

/** Folders a member running in `env` can work in: the ones that live there. */
export function foldersFor(folders: TeamFolder[], env: Env): TeamFolder[] {
  return folders.filter((f) => f.env === env);
}

/** The isolation a new member starts with: its own worktree where there is a repository to make one in. */
export function defaultIsolation(folder: TeamFolder | undefined): Isolation {
  return folder && folder.is_git && !folder.readonly ? "worktree" : "shared";
}
