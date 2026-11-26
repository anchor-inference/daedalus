// What the secrets manager knows about a secret without its value, and the orderings it draws them in.
// A list from the host never carries a value (api_secrets.py): everything here works on names, scopes
// and times, so nothing in this file can leak one.

import type { SessionList } from "./api";

export type SecretScope = "session" | "project";

/** A secret as the host shows it: everything but the value. */
export type SecretView = {
  id: string;
  name: string;
  scope: SecretScope;
  scope_id: string;
  scope_title?: string;
  note: string;
  placeholder: string;
  env: string;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
  last_used_by: string;
  uses: number;
  readable: boolean;
};

/** What a draft carries: the name it goes by, and whether this composer made it (removing the chip
 *  then takes it back, as nothing else asked for it). */
export type AttachedSecret = { id: string; name: string; scope: SecretScope; fresh: boolean };

/** A sign-in saved from the browser's private form: the site and when, never the username or password. */
export type SavedLoginView = { id: string; site: string; name: string; created_at: string; updated_at: string; last_used_at: string | null; uses: number; readable: boolean };

export type SecretsList = {
  secrets: SecretView[];
  /** With a chat: every secret it cannot attach (another chat's, another project's), to manage from here. */
  others?: SecretView[];
  logins?: SavedLoginView[];
  /** Without a chat: the projects a new secret can be filed under. */
  projects?: { id: string; name: string }[];
  project_id?: string | null;
  project_name?: string | null;
};

/** One line of the host's trail of who looked at or changed a secret: never the value. */
export type SecretAudit = { at: string; name: string; action: string; via: string };

/** Where a secret is named, and its trail: asked for the one secret that is open, never for the list. */
export type SecretUsage = { conversations: number; titles: { id: string; title: string }[]; audit?: SecretAudit[] };

/** The name as the host will keep it, shown while typing so the placeholder is no surprise. */
export function secretName(raw: string): string {
  return raw.trim().toLowerCase().replace(/[\s.-]+/g, "_");
}

export function validSecretName(raw: string): boolean {
  return /^[a-z][a-z0-9_]{0,47}$/.test(secretName(raw));
}

export const placeholderOf = (name: string) => `«secret:${name}»`;

/** Whether a secret answers a search: its name, its note, or the chat or project it belongs to. */
export function matchesSecret(secret: SecretView, query: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return [secret.name, secret.note, secret.scope_title ?? ""].some((part) => part.toLowerCase().includes(q));
}

export type SecretSort = "used" | "name" | "changed";

const when = (iso: string | null | undefined) => (iso ? Date.parse(iso) || 0 : 0);

/** The list in the order asked for. "Recently used" puts the never-used last, newest change first
 *  among them, so a secret just handed over is not buried under thirty that ran last night. */
export function sortSecrets(list: SecretView[], sort: SecretSort): SecretView[] {
  const byName = (a: SecretView, b: SecretView) => a.name.localeCompare(b.name) || (a.scope_title ?? "").localeCompare(b.scope_title ?? "");
  const out = [...list];
  if (sort === "name") return out.sort(byName);
  if (sort === "changed") return out.sort((a, b) => when(b.updated_at) - when(a.updated_at) || byName(a, b));
  return out.sort((a, b) => when(b.last_used_at) - when(a.last_used_at) || when(b.updated_at) - when(a.updated_at) || byName(a, b));
}

export type SecretTab = "chat" | "project" | "all";

/** Which of a chat's three views a secret is listed in: the chat's own, the chat's project's, or all. */
export function inTab(secret: SecretView, tab: SecretTab, sessionId: string | undefined, projectId: string | null | undefined): boolean {
  if (tab === "chat") return secret.scope === "session" && secret.scope_id === sessionId;
  if (tab === "project") return !!projectId && secret.scope === "project" && secret.scope_id === projectId;
  return true;
}

/** A heading of the list in its "all" form: one project with its own secrets and its chats', or the
 *  chats that belong to no project the operator made. */
export type SecretGroup = { id: string; title: string; secrets: SecretView[] };

/** The list grouped by project. A project's secret goes under its project; a chat's goes under the
 *  project the chat is in, read from the chats list the sidebar already holds. A chat's throwaway
 *  project is the chat itself and makes no heading of its own, and a chat the list does not know (an
 *  old one past its first page) is put with the chats that have no project rather than guessed at.
 *  `first` is the open chat and its project, which is listed before the others. */
export function groupByProject(list: SecretView[], sessions: SessionList | null | undefined, first?: { session?: string; project?: string | null; name?: string | null }): SecretGroup[] {
  const real = new Map((sessions?.projects ?? []).filter((p) => !p.settings?.ephemeral).map((p) => [p.id, p.name]));
  const chats = new Map((sessions?.sessions ?? []).map((s) => [s.id, s.project_id]));
  // The open chat's project is known from the secrets list itself, whatever the chats list holds.
  if (first?.session && first.project) {
    chats.set(first.session, first.project);
    if (!real.has(first.project)) real.set(first.project, first.name || first.project);
  }
  const groups = new Map<string, SecretGroup>();
  for (const secret of list) {
    let id = "";
    let title = "";
    if (secret.scope === "project") {
      id = secret.scope_id;
      title = secret.scope_title || real.get(id) || id;
    } else {
      const project = chats.get(secret.scope_id);
      if (project && real.has(project)) {
        id = project;
        title = real.get(project)!;
      }
    }
    const group = groups.get(id) ?? { id, title, secrets: [] };
    group.secrets.push(secret);
    groups.set(id, group);
  }
  return [...groups.values()].sort((a, b) => {
    if (!a.id !== !b.id) return a.id ? -1 : 1;
    const top = first?.project;
    if (top && (a.id === top) !== (b.id === top)) return a.id === top ? -1 : 1;
    return a.title.localeCompare(b.title);
  });
}

/** What the host wrote as the last user of a secret, taken apart: the tool, and the chat it ran in
 *  ("Exec in <chat id>", "MCP files (read) in <chat id>") or the site it typed into ("BrowserAct (agent)
 *  on https://…"). Anything else is shown as written. */
export function usedBy(raw: string): { tool: string; session?: string; site?: string } {
  const text = raw.trim();
  const chat = /^(.*\S)\s+in\s+([A-Za-z0-9_-]{6,})$/.exec(text);
  if (chat) return { tool: chat[1], session: chat[2] };
  const site = /^(.*\S)\s+on\s+(\S+)$/.exec(text);
  if (site) return { tool: site[1], site: site[2] };
  return { tool: text };
}
