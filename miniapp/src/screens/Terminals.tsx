// The Terminals screen. On a desktop it is one page: every terminal in a list at the left, the ones the
// operator set up beside it, live, in one, two or four panes (TerminalWorkspace.tsx), and the page
// header with the count, the waiting terminals and the layout. `/app/terminals/<id>` is how the rest of
// the app links a terminal; it lands in the panes. A phone draws the same listing as cards
// (TerminalsPhone.tsx), read from the host's listing with a preview (`?preview=6`) and never from a
// connection: forty cards must not hold forty sockets.

import type { Project, SessionList, TerminalEnv, TerminalEnvName, TerminalView as TerminalRow } from "../api";
import { primaryFolder } from "../folders";
import { navigate, pathFor } from "../router";
import { useMedia } from "../ui/index";
import { PhoneTerminals } from "./TerminalsPhone";
import { TerminalWorkspaceScreen } from "./TerminalWorkspace";
import { invalidate, peek } from "../store";
import { errorText } from "../ui";
import { createTerminalConfirmed } from "../terminal/actions";
import { TerminalPicker } from "../terminal/picker";

export const LIST_KEY = "/api/terminals?preview=6";

/** The address of a terminal's full-screen view. */
export function terminalPath(id: string): string {
  return pathFor("terminals", id);
}

/** Where a new container terminal starts: the project the operator is looking at, else the folder of the conversation they used last. */
export function containerFolder(project: Project | null, sessions: SessionList["sessions"]): { path: string; projectId: string | null } | null {
  if (project) {
    const inside = project.folders.find((f) => f.env === "container" && f.reachable) ?? (primaryFolder(project)?.env === "container" ? primaryFolder(project) : undefined);
    if (inside) return { path: inside.path, projectId: project.id };
  }
  const recent = [...sessions].filter((s) => s.workspace_path).sort((a, b) => Date.parse(b.last_message_at || "") - Date.parse(a.last_message_at || ""))[0];
  return recent?.workspace_path ? { path: recent.workspace_path, projectId: recent.project_id || null } : null;
}

/** Opens a free terminal (asking at the cap) and goes to it; the shared path for this screen and the palette. */
export async function openFreeTerminal(env: TerminalEnvName, cwd: string | undefined, projectId: string | null, toast: (text: string) => void, sandbox = false): Promise<void> {
  let row: TerminalRow | null;
  try {
    row = await createTerminalConfirmed({ env, owner_kind: "free", cwd: cwd || undefined, project_id: projectId || undefined, ...(sandbox ? { sandbox: true } : {}) });
  } catch (error) {
    toast(errorText(error));
    return;
  }
  if (!row) return;
  invalidate("/api/terminals");
  navigate(terminalPath(row.id));
}

/** The "where?" question for a free terminal, on this screen, its Workspace and a phone: the folder
 *  the operator works in inside the container, their home on the host, or a folder they choose. */
export function FreeTerminalPicker({ envs, lens, projects, toast, anchor, phone, onClose }: { envs: TerminalEnv[]; lens: Project | null; projects: Project[]; toast: (text: string) => void; anchor: HTMLElement | null; phone: boolean; onClose: () => void }) {
  const sessions = peek<SessionList>("/api/sessions")?.sessions ?? [];
  const here = containerFolder(lens, sessions);
  const home = envs.find((e) => e.env === "host")?.home || null;
  return (
    <TerminalPicker
      envs={envs}
      folders={{ container: { path: here?.path ?? null, projectId: here?.projectId ?? null }, host: { path: home } }}
      projects={projects}
      lens={lens}
      anchor={anchor}
      phone={phone}
      onPick={(picked) => openFreeTerminal(picked.env, picked.cwd, picked.projectId ?? null, toast, picked.sandbox)}
      onClose={onClose}
    />
  );
}

export function TerminalsScreen(props: { detail: string | null; query: URLSearchParams; toast: (text: string) => void; project: string; projects: Project[] }) {
  const { detail, query, ...rest } = props;
  // A phone draws the same listing from its own pieces (TerminalsPhone.tsx): a capacity line, cards,
  // sheets; a terminal opened from it is the Workspace's one pane at a time. A desktop has one page,
  // the list beside the panes, whether or not a terminal is open in them.
  const phone = !useMedia("(min-width: 1024px)");
  if (detail || !phone) return <TerminalWorkspaceScreen detail={detail} query={query} {...rest} />;
  return <PhoneTerminals {...rest} />;
}
