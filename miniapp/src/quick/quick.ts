// Quick Chat's decisions, apart from its page so they are tested on their own (quick.test.ts): where
// a message goes, what `@` and `$` are completing, what the agent is told about the context the
// operator attached, and what the desktop's tray is told about this installation.

import type { NotificationSummary, Project, SessionSummary, SkillEntry } from "../api";
import { t } from "../i18n";

/** `GET /api/companion`: the operator's standing companion. An installation without one answers 404. */
export type Companion = {
  session_id: string;
  name: string;
  avatar: { shape: string; color: string; eyes: string; accessory: string };
  paused: boolean;
  state: "idle" | "listening" | "thinking" | "working";
  running_agents: number;
};

/** Where Enter sends: the companion, the conversation the operator was last in, or a new one. */
export type TargetKind = "companion" | "last" | "new";

/** Something `@` attached, told to the agent beside the words. */
export type ContextItem =
  | { kind: "session"; id: string; title: string }
  | { kind: "project"; id: string; name: string }
  | { kind: "file"; path: string; session: string; sessionTitle: string };

export type Trigger = { char: "@" | "$"; query: string; start: number };

/** The conversation Quick Chat calls "last": the newest one the operator talks to, not a helper an
 *  agent started, not one put away, and not the companion's own, which is a target of its own. */
export function lastSession(sessions: SessionSummary[], companionSession = ""): SessionSummary | null {
  const eligible = sessions.filter((s) => !s.archived && !s.metadata?.subagent_of && s.id !== companionSession);
  if (!eligible.length) return null;
  return eligible.reduce((best, s) => (stamp(s.last_message_at || s.created_at) > stamp(best.last_message_at || best.created_at) ? s : best));
}

function stamp(value: string): number {
  const n = Date.parse(value);
  return Number.isFinite(n) ? n : 0;
}

/** Where Enter sends when the operator has not picked: a new conversation. The companion was the
 *  default once, and a quick question typed over another program then landed in the companion's
 *  long-running thread, where it was neither found again nor started clean; the companion and the
 *  last chat stay one press away on the chip. */
export const DEFAULT_TARGET: TargetKind = "new";

/** The `@word` or `$word` the caret is at the end of, when it starts a word: an address in an email or
 *  a price in a sentence is typed, not a request for a picker. */
export function trigger(text: string, caret: number): Trigger | null {
  const before = text.slice(0, caret);
  const match = /(^|\s)([@$])([^\s@$]*)$/.exec(before);
  if (!match) return null;
  return { char: match[2] as "@" | "$", query: match[3], start: before.length - match[3].length - 1 };
}

/** The text with the completed `@word` taken out, and where the caret goes. */
export function removeTrigger(text: string, found: Trigger, caret: number): { text: string; caret: number } {
  const head = text.slice(0, found.start);
  const tail = text.slice(caret);
  const joined = head.endsWith(" ") && tail.startsWith(" ") ? head + tail.slice(1) : head + tail;
  return { text: joined, caret: found.start };
}

/** What `@` offers for a query: conversations, projects and files, a few of each, best match first. */
export function mentionChoices(query: string, sessions: SessionSummary[], projects: Project[], files: { path: string; session: string; sessionTitle: string }[], limit = 4): ContextItem[] {
  const q = query.trim().toLowerCase();
  const hit = (name: string) => !q || name.toLowerCase().includes(q);
  const rank = (name: string) => (name.toLowerCase().startsWith(q) ? 0 : 1);
  const bySession = sessions
    .filter((s) => !s.archived && !s.metadata?.subagent_of && hit(s.title || s.id))
    .sort((a, b) => rank(a.title) - rank(b.title) || stamp(b.last_message_at) - stamp(a.last_message_at))
    .slice(0, limit)
    .map((s): ContextItem => ({ kind: "session", id: s.id, title: s.title || s.id }));
  const byProject = projects
    .filter((p) => !p.settings?.archived && hit(p.name))
    .sort((a, b) => rank(a.name) - rank(b.name))
    .slice(0, limit)
    .map((p): ContextItem => ({ kind: "project", id: p.id, name: p.name }));
  return [...bySession, ...byProject, ...files.slice(0, limit).map((file): ContextItem => ({ kind: "file", ...file }))];
}

/** What `$` offers: the enabled skills whose name or description has the query in it. */
export function skillChoices(query: string, skills: SkillEntry[], limit = 6): SkillEntry[] {
  const q = query.trim().toLowerCase();
  return skills.filter((s) => !q || s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q)).slice(0, limit);
}

export function contextKey(item: ContextItem): string {
  return item.kind === "file" ? `file:${item.session}:${item.path}` : `${item.kind}:${item.id}`;
}

export function contextLabel(item: ContextItem): string {
  return item.kind === "session" ? item.title : item.kind === "project" ? item.name : item.path;
}

/** The message as the agent reads it. A skill is asked for in words, exactly as the composer's
 *  palette does (the Skill tool does the loading); the context goes in a short list after the words,
 *  naming each thing by what the agent's own tools take — a session's id, a project's id, a path. */
export function composeMessage(text: string, skill: SkillEntry | null, context: ContextItem[]): string {
  let body = text.trim();
  if (skill) body = `${t("composer.skill.use", { name: skill.name })}${body}`;
  if (!context.length) return body;
  const lines = context.map((item) =>
    item.kind === "session" ? t("quick.context.session", { title: item.title, id: item.id })
      : item.kind === "project" ? t("quick.context.project", { name: item.name, id: item.id })
        : t("quick.context.file", { path: item.path, title: item.sessionTitle, id: item.session }));
  return `${body}\n\n${t("quick.context.title")}\n${lines.map((line) => `- ${line}`).join("\n")}`;
}

/** The project a new conversation is started in: the one `@` named, when exactly one was. */
export function projectForNew(context: ContextItem[]): string | undefined {
  const projects = context.filter((item) => item.kind === "project");
  return projects.length === 1 ? (projects[0] as { id: string }).id : undefined;
}

/** How a message reaches its target. Words alone to the companion go to its own endpoint; a picture
 *  goes through the app's ordinary upload, into the companion's session, since that endpoint takes
 *  no file of its own. A message to a conversation is always marked a follow-up: an idle one runs it
 *  at once, and a running one holds it for the end of the turn instead of steering the turn with
 *  words typed from another program. */
export type Delivery =
  | { kind: "companion" }
  | { kind: "upload"; session: string }
  | { kind: "message"; session: string }
  | { kind: "new" };

export function delivery(target: TargetKind, files: number, companion: Companion | null, last: SessionSummary | null): Delivery | null {
  if (target === "companion") {
    if (!companion) return null;
    return files ? { kind: "upload", session: companion.session_id } : { kind: "companion" };
  }
  if (target === "last") {
    if (!last) return null;
    return files ? { kind: "upload", session: last.id } : { kind: "message", session: last.id };
  }
  return { kind: "new" };
}

/** The new conversation a Computer request starts: of kind 'computer', titled by its words, in the
 *  project `@` named when exactly one was. */
export function computerSession(words: string, context: ContextItem[]): { autotitle: true; title: string; kind: "computer"; project_id?: string } {
  const body: { autotitle: true; title: string; kind: "computer"; project_id?: string } = { autotitle: true, title: words.slice(0, 80) || t("quick.target.computer"), kind: "computer" };
  const project = projectForNew(context);
  if (project) body.project_id = project;
  return body;
}

/** The grant a Computer request makes for its new conversation, in the host's terms. */
export function computerGrant(session: string, scope: { kind: string; window?: string; app?: string; display?: string }, minutes: number): { session_id: string; scope: typeof scope; minutes: number } {
  return { session_id: session, scope, minutes };
}

/** What the desktop's tray shows: agents at work and decisions waiting for the operator. */
export type DeskStatus = { running: number; waiting: number; companion: { paused: boolean } | null };

export function deskStatus(sessions: SessionSummary[] | undefined, summary: NotificationSummary | undefined, companion: Companion | null): DeskStatus {
  const running = (sessions ?? []).filter((s) => s.status === "running" || s.status === "compacting").length;
  return { running, waiting: Math.max(0, summary?.needs_you ?? 0), companion: companion ? { paused: !!companion.paused } : null };
}

/** The picture's file, from the bytes the shell hands over. */
export function appshotFile(shot: { name: string; type: string; bytes: Uint8Array }): File {
  return new File([shot.bytes as BlobPart], shot.name || "appshot.png", { type: shot.type || "image/png" });
}

/** The sentence for a refusal the shell reported instead of a picture. */
export function appshotError(error: string, detail = ""): string {
  const known = ["permission", "cancelled", "empty", "gone", "busy"];
  return known.includes(error) ? t(`quick.appshot.error.${error}`) : detail ? t("quick.appshot.error.failed.detail", { detail }) : t("quick.appshot.error.failed");
}
