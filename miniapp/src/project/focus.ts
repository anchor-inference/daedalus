// A project's focus mode as the app decides it: which page a route shows, what the sidebar says about
// each member of the team, why a launch waits, and what the orchestrator's steps are called. Pure, so
// every decision here is tested without a browser; the components only draw what these return.

import type { Project } from "../api";
import type { Queued, Staff, StaffStatus } from "../team/team";

/** The pages of a project, each at /app/project/<id>/<page>. No page is the orchestrator's chat. */
export const FOCUS_PAGES = ["team", "board", "journal", "brief", "wakeups", "folders", "terminals"] as const;
export type FocusPage = (typeof FOCUS_PAGES)[number];

/** What the centre of focus mode shows for a route. */
export type FocusView = { kind: "orchestrator" } | { kind: "session"; id: string } | { kind: "staff"; id: string } | { kind: "page"; page: FocusPage };

export function focusView(page: string | null, inner: string | null): FocusView {
  if (page === "s" && inner) return { kind: "session", id: inner };
  if (page === "staff" && inner) return { kind: "staff", id: inner };
  if (page && (FOCUS_PAGES as readonly string[]).includes(page)) return { kind: "page", page: page as FocusPage };
  return { kind: "orchestrator" };
}

/** Whether the centre is a conversation, which owns the whole height and its own composer. */
export function isChat(view: FocusView): boolean {
  return view.kind !== "page";
}

/** A project that can have a team and an orchestrator: not a chat's own scratch project, not the
 *  installation's own. The others have no focus mode worth entering. */
export function canFocus(project: Pick<Project, "system" | "settings"> | null | undefined): boolean {
  return !!project && !project.system && !project.settings.system && !project.settings.ephemeral;
}

// ── the team, as the sidebar draws it ─────────────────────────────────────────────────────────

/** The colour of a member's dot, as the mock-up names them: at work, something to look at, waiting on
 *  someone, free. Silence is grey on purpose — silent is not failed — and an error is its own. A member
 *  with nothing running is "off", not "free": a phone shows the word alone, and one word for both left
 *  the operator unable to tell a member ready for a message from one that has to be launched first. */
export type StaffTone = "working" | "review" | "waiting" | "free" | "off" | "silent" | "error";

const TONES: Record<StaffStatus, StaffTone> = {
  off: "off",
  starting: "working",
  working: "working",
  turn_done_unseen: "review",
  idle: "free",
  question: "waiting",
  permission: "waiting",
  error: "error",
  exited: "off",
  no_signal: "silent",
};

/** A launch waiting in the queue is "waiting" too: the operator asked for work that has not begun. */
export function staffTone(member: Pick<Staff, "status" | "queued">): StaffTone {
  if (member.status === "off" && (member.queued?.length ?? 0) > 0) return "waiting";
  return TONES[member.status] ?? "free";
}

/** Why a launch waits, as the host's launch queue names it (daedalus/host/launch_queue.py). */
export const WAIT_REASONS = ["dependencies", "busy", "project", "terminals", "machine", "stagger", "behind"] as const;
export type WaitReason = (typeof WAIT_REASONS)[number];

/** The first of a member's waiting launches, which is the one the card explains. */
export function firstWait(member: Pick<Staff, "queued">): Queued | null {
  const queued = member.queued ?? [];
  if (queued.length === 0) return null;
  return [...queued].sort((a, b) => a.position - b.position)[0];
}

/** The key of a reason's words; a reason this app does not know yet reads as "in the queue". */
export function waitKey(reason: string | null | undefined): string {
  return reason && (WAIT_REASONS as readonly string[]).includes(reason) ? `focus.wait.${reason}` : "focus.wait.queued";
}

/** Members who are not one-off helpers, then the helpers: the two lists the sidebar draws. */
export function splitTeam<T extends Pick<Staff, "one_off" | "archived_at">>(staff: T[]): { team: T[]; oneOff: T[] } {
  const current = staff.filter((m) => !m.archived_at);
  return { team: current.filter((m) => !m.one_off), oneOff: current.filter((m) => m.one_off) };
}

// ── the orchestrator's steps ─────────────────────────────────────────────────────────────────

/** The orchestrator's own tools, drawn as step lines in its chat rather than folded away with the work:
 *  what it did to the project is what the operator reads the chat for. */
export const ORCHESTRATOR_STEPS = [
  "Brief", "Folders", "Journal", "Team", "Hire", "StaffEdit", "Dismiss", "Assign", "Tell", "ReadStaff", "Answer",
  "Interrupt", "Pause", "Release", "Peek", "Tasks", "WakeMe", "Watch", "Unwatch", "AskOperator", "WithdrawQuestions", "ProjectReport",
] as const;

export function isOrchestratorStep(name: string): boolean {
  return (ORCHESTRATOR_STEPS as readonly string[]).includes(name);
}

/** Which verb a step takes, as a dictionary key under `focus.step.`: the tool, and for the tools with
 *  an `op`, the operation — "Folder added", not "Folders". Reading is quieter than writing: a step that
 *  only looked is named as a look. */
export function stepKey(name: string, args: Record<string, unknown>): string {
  const op = typeof args.op === "string" ? args.op : "";
  switch (name) {
    case "Folders":
      return `Folders.${["add", "update", "remove"].includes(op) ? op : "list"}`;
    case "Tasks":
      return `Tasks.${["get", "create", "update", "move"].includes(op) ? op : "list"}`;
    case "Journal":
      return `Journal.${op === "read" ? "read" : "write"}`;
    case "Brief":
      return typeof args.body === "string" ? "Brief.write" : "Brief.read";
    case "Team":
      return args.concurrency !== undefined && args.concurrency !== null ? "Team.concurrency" : "Team.read";
    default:
      return isOrchestratorStep(name) ? name : "other";
  }
}

/** Every step key the dictionary must have, for the test that keeps both languages complete. */
export const STEP_KEYS = [
  "Folders.add", "Folders.update", "Folders.remove", "Folders.list", "Tasks.get", "Tasks.create", "Tasks.update", "Tasks.move", "Tasks.list",
  "Journal.read", "Journal.write", "Brief.write", "Brief.read", "Team.concurrency", "Team.read",
  "Hire", "StaffEdit", "Dismiss", "Assign", "Tell", "ReadStaff", "Answer", "Interrupt", "Pause", "Release", "Peek", "WakeMe", "Watch", "Unwatch",
  "AskOperator", "WithdrawQuestions", "ProjectReport", "other",
] as const;

/** The detail beside a step's verb: the thing it acted on, in the arguments' own words. */
export function stepDetail(name: string, args: Record<string, unknown>): string {
  const s = (k: string) => (typeof args[k] === "string" ? (args[k] as string) : typeof args[k] === "number" ? String(args[k]) : "");
  switch (name) {
    case "Folders":
      return s("path") || s("folder") || s("label");
    case "Tasks":
      return [s("title") || s("task_id"), s("status") && `→ ${s("status")}`, s("assignee") && `→ ${s("assignee")}`].filter(Boolean).join(" ");
    case "Journal":
      return s("text").split("\n")[0].slice(0, 80);
    case "Brief":
      return s("section");
    case "Team":
      return s("staff") || s("concurrency");
    case "Hire":
      return [s("name"), s("role")].filter(Boolean).join(" · ");
    case "Assign":
      return [s("staff"), s("title") || s("task_id")].filter(Boolean).join(" · ");
    case "Tell":
      return [s("staff"), s("text").split("\n")[0].slice(0, 60)].filter(Boolean).join(" · ");
    case "Peek":
      return [s("op"), s("path") || s("pattern") || s("ref")].filter(Boolean).join(" · ");
    case "Answer":
      return s("request_id");
    case "WakeMe":
      return s("note").slice(0, 60);
    case "Watch":
      return s("note").slice(0, 60);
    case "Unwatch":
      return s("id");
    case "AskOperator":
      // A batch is named by its titles; the questions themselves wait in the Questions tab.
      return Array.isArray(args.questions)
        ? args.questions.map((q) => (q && typeof q === "object" && typeof (q as Record<string, unknown>).title === "string" ? (q as Record<string, string>).title : "")).filter(Boolean).join(" · ").slice(0, 120)
        : s("title") || s("text").split("\n")[0].slice(0, 80);
    case "WithdrawQuestions":
      return [Array.isArray(args.ids) ? args.ids.join(", ") : "", s("reason").slice(0, 60)].filter(Boolean).join(" · ");
    case "ProjectReport":
      return s("title") || s("text").split("\n")[0].slice(0, 80);
    default:
      return s("staff");
  }
}

// ── the header ───────────────────────────────────────────────────────────────────────────────

export const AUTONOMIES = ["ask", "normal", "full"] as const;
export type Autonomy = (typeof AUTONOMIES)[number];

// ── the phone ────────────────────────────────────────────────────────────────────────────────

/** A project's four tabs on a phone, in the order the bar shows them: they take the place of the
 *  app's own tabs while a project is open, and the header's back leads back to those. Terminals comes
 *  before the board because the app's own bar has them in that order; with the two swapped, a thumb
 *  that learnt "the board is the fourth tab" landed on the wrong one on the way in and out. */
export const PHONE_TABS = ["orchestrator", "team", "terminals", "board"] as const;
export type PhoneTab = (typeof PHONE_TABS)[number];

/**
 * Which tab a route lights on a phone, and whether the bar is there at all. The project's other pages
 * (the brief, the journal…) keep the bar with no tab lit, so every tab is a thumb away from them. A
 * session inside the project is a detail with a back of its own, the way a conversation is in the
 * agents list, and gives the whole height to the conversation.
 */
export function phoneTab(view: FocusView): { tab: PhoneTab | null; bar: boolean } {
  if (view.kind === "session" || view.kind === "staff") return { tab: null, bar: false };
  if (view.kind === "orchestrator") return { tab: "orchestrator", bar: true };
  const page = view.page as string;
  return { tab: (PHONE_TABS as readonly string[]).includes(page) ? (page as PhoneTab) : null, bar: true };
}

/** The request the phone's banner shows: the operator's oldest open one — the one waiting longest. */
export function oldestOpen<T extends { routed_to: string; resolved_at: string | null; created_at: string }>(asks: T[]): { ask: T | null; waiting: number } {
  const open = asks.filter((a) => a.routed_to === "operator" && !a.resolved_at).sort((a, b) => a.created_at.localeCompare(b.created_at));
  return { ask: open[0] ?? null, waiting: open.length };
}

/** "2 working · 1 in review" under the project's name: members at work, and tasks waiting for a look. */
export function teamCounts(staff: Pick<Staff, "status" | "queued" | "archived_at">[], tasks: { status: string }[]): { working: number; review: number } {
  return {
    working: staff.filter((m) => !m.archived_at && staffTone(m) === "working").length,
    review: tasks.filter((task) => task.status === "review").length,
  };
}
