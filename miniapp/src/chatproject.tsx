// A chat made a project, a project turned back into its chat, and a chat dragged onto a project.
//
// Every chat has a project of its own that the server marks ephemeral, and the lists draw it as the
// chat (`isProject` in grouping.ts). "Make a project" settles that project in place under a name the
// operator chooses: the same row, the same folder, the chat still in it, so nothing on disk moves.
// "Turn back into a chat" is the way back for a project with one chat; the server refuses it, with
// the reason, while the project holds anything a chat's project would lose when its chat is deleted.
// Both are revisioned project commands, so they carry the revision the list last read and a stale
// list is told so rather than overwriting a rename made on another screen.

import { useState } from "react";
import { api, ApiError, type ProjectFolder, type ProjectRef, type SessionSummary } from "./api";
import { type Folder, rememberFolder } from "./grouping";
import { t } from "./i18n";
import { invalidate } from "./store";
import { errorText } from "./ui";
import { type MenuItem, Sheet, toast } from "./ui/dialogs";

function afterChange(): void {
  invalidate("/api/projects");
  invalidate("/api/sessions");
}

function command(project: ProjectRef, fields: Record<string, unknown> = {}): Record<string, unknown> {
  return { ...fields, expected_entity_revision: project.entity_revision, client_operation_id: crypto.randomUUID() };
}

/** Whether the project is the operator's own ordinary project, the only kind these commands touch:
 *  never the installation's own, never one an orchestrator runs (that one lives in another mode). */
function ordinary(project: ProjectRef & { orchestrator?: ProjectFolder["orchestrator"] }): boolean {
  return !project.system && !project.settings.system && !project.settings.orchestrator?.enabled && !project.orchestrator?.enabled;
}

/** "Make a project" for a chat whose project is still its own, or nothing for any other session. */
export function makeProjectCommand(chat: ProjectRef | null | undefined, open: () => void): Exclude<MenuItem, "-"> | null {
  if (!chat?.settings?.ephemeral || !ordinary(chat)) return null;
  return { label: t("chat.toproject"), icon: "folderplus", onSelect: open };
}

/**
 * The name for the project a chat becomes, with the chat's title offered. One write settles and
 * renames the chat's own project; the sidebar then lists the project, unfolded, with the chat in it.
 */
export function MakeProjectSheet({ project, title, onClose, onMade }: { project: ProjectRef; title: string; onClose: () => void; onMade?: () => void }) {
  const [name, setName] = useState(title);
  const [busy, setBusy] = useState(false);
  async function make() {
    const label = name.trim();
    if (!label || busy) return;
    setBusy(true);
    try {
      await api.post(`/api/projects/${encodeURIComponent(project.id)}/as-project`, command(project, { name: label }));
      // Unfolded at once: the operator just asked for a project around this chat, and a folded row
      // would hide the one thing that tells them where the chat went.
      rememberFolder(project.id, true);
      afterChange();
      toast(t("chat.toproject.done", { name: label }));
      onClose();
      onMade?.();
    } catch (error) {
      // A conflict means the list was stale; it is read again so a second press carries the revision.
      if (error instanceof ApiError && error.status === 409) afterChange();
      toast(errorText(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("chat.toproject")} onClose={onClose} size="narrow">
      <form className="make-project" onSubmit={(event) => { event.preventDefault(); void make(); }}>
        <label className="field" htmlFor="make-project-name">{t("chat.toproject.name")}</label>
        <input id="make-project-name" className="field" autoFocus value={name} maxLength={200} onChange={(event) => setName(event.target.value)} />
        <div className="sub">{t("chat.toproject.hint")}</div>
        <div className="sheet-foot">
          <button type="button" className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
          <button type="submit" className="btn primary" disabled={busy || !name.trim()}>{t("chat.toproject.action")}</button>
        </div>
      </form>
    </Sheet>
  );
}

async function asChat(project: ProjectRef, title: string): Promise<void> {
  try {
    await api.post(`/api/projects/${encodeURIComponent(project.id)}/as-chat`, command(project));
    afterChange();
    toast(t("project.tochat.done", { name: title }));
  } catch (error) {
    afterChange();
    toast(errorText(error));
  }
}

/**
 * "Turn back into a chat" for a project row: offered on an ordinary project, and enabled only while
 * it holds exactly one chat, which the row's count says without asking. Whatever else would keep it a
 * project (a team, a schedule, a brief) only the server knows; its refusal is the toast.
 */
export function asChatCommand(folder: Folder): Exclude<MenuItem, "-"> | null {
  if (folder.chat || folder.system || !ordinary(folder.project)) return null;
  const one = folder.total === 1;
  const title = folder.rows[0]?.s.title || folder.name;
  return { label: t("project.tochat"), icon: "bots", disabled: !one, hint: one ? undefined : t("project.tochat.one"), onSelect: () => void asChat(folder.project, title) };
}

/** The chat being dragged across the sidebar. A drag's data cannot be read until the drop, and a
 *  project row has to decide while the pointer is over it whether it takes the chat. */
let dragged: SessionSummary | null = null;
const DRAG_TYPE = "application/x-daedalus-session";

export function startChatDrag(event: React.DragEvent, session: SessionSummary): void {
  dragged = session;
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData(DRAG_TYPE, session.id);
  event.dataTransfer.setData("text/plain", session.title);
}

export function endChatDrag(): void {
  dragged = null;
}

/** Whether a project row takes the chat being dragged: an ordinary project other than the chat's own. */
export function takesDrop(event: React.DragEvent, folder: Folder): boolean {
  return !!dragged && event.dataTransfer.types.includes(DRAG_TYPE) && !folder.chat && !folder.system && ordinary(folder.project) && dragged.project_id !== folder.key;
}

async function move(sessionId: string, projectId: string, ownDirectory: boolean): Promise<void> {
  await api.post(`/api/sessions/${encodeURIComponent(sessionId)}/project`, { project_id: projectId, own_directory: ownDirectory });
  afterChange();
}

/**
 * Move the dropped chat into the project, working in its folder as the Move sheet's default does.
 * The undo is cheap and exact: the project it left still exists (a move removes nothing), so the
 * chat goes back to it with the directory choice it had.
 */
export async function dropChat(folder: Folder): Promise<void> {
  const session = dragged;
  dragged = null;
  if (!session || session.project_id === folder.key) return;
  try {
    await move(session.id, folder.key, false);
    toast(t("move.done", { name: folder.name }), {
      undo: () => void move(session.id, session.project_id, !!session.workspace_own).catch((error) => toast(errorText(error))),
    });
  } catch (error) {
    toast(errorText(error));
  }
}
