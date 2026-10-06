/** A project's folders, as the screens read them. */

import type { ProjectDir, ProjectEnvironments, ProjectRef } from "./api";

/** The folder an agent of the project works in unless it is given another: the first one. */
export function primaryFolder(project: Pick<ProjectRef, "folders">): ProjectDir | undefined {
  return project.folders[0];
}

/** The primary folder's path, for the line under a project's name. */
export function projectPath(project: Pick<ProjectRef, "folders">): string {
  return primaryFolder(project)?.path ?? "";
}

/** Whether an agent can be started in the project from where the bot runs: its primary folder is there. */
export function projectReachable(project: Pick<ProjectRef, "folders">): boolean {
  const primary = primaryFolder(project);
  return !!primary && (primary.reachable || primary.reach === "terminals");
}

/** What a folder is called on screen: the label the operator gave it, else the last part of its path. */
export function folderName(folder: { label: string; path: string }): string {
  if (folder.label.trim()) return folder.label.trim();
  const parts = folder.path.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] ?? folder.path;
}

/** The folders an agent of the bot can be started in: those of the bot's own environment. */
export function agentFolders(project: Pick<ProjectRef, "folders">): ProjectDir[] {
  return project.folders.filter((folder) => folder.reach === "agents" || folder.reach === "terminals");
}

/** Whether a new agent is offered a choice of folder: only when there is more than one it could work in. */
export function offersFolderChoice(project: Pick<ProjectRef, "folders">): boolean {
  return agentFolders(project).length > 1;
}

/** The sentence under a folder that says who can work in it, as an i18n key.
 *
 * A folder of the other environment is not "missing" when the bot cannot see it: it was never the
 * bot's to see, and the sentence says who can reach it instead of asking for a mount. */
export function reachKey(folder: ProjectDir, environments?: Pick<ProjectEnvironments, "docker">): string {
  if (folder.reach === "terminals") return "folder.reach.terminals";
  if (folder.reach === "none") return "folder.reach.none";
  if (!folder.reachable) return environments?.docker ? "folder.reach.unmounted" : "folder.reach.missing";
  if (folder.readonly) return "folder.reach.readonly";
  if (!folder.writable) return "folder.reach.notwritable";
  return "folder.reach.ok";
}

/** Whether the sentence is a problem the operator has to act on, drawn in the warning colour. */
export function reachIsProblem(key: string): boolean {
  return key === "folder.reach.unmounted" || key === "folder.reach.missing" || key === "folder.reach.none";
}

/** Whether adding a folder here needs the Docker mount said out loud: a container folder in Docker is
 *  invisible to the bot until it is bind-mounted, which only a restart applies. */
export function needsMount(env: "container" | "host", environments?: Pick<ProjectEnvironments, "docker">): boolean {
  return !!environments?.docker && env === "container";
}

/** Whether a typed folder is a full path: it starts with / (or a drive letter on Windows). Empty is not a problem. */
export function pathProblem(path: string): boolean {
  const typed = path.trim();
  return !!typed && !(typed.startsWith("/") || /^[A-Za-z]:[\\/]/.test(typed));
}

/**
 * Where a session's file pane reads a folder from. The session's own folder is the session's own
 * address; another one is under `/folders/{id}`, so every file address built from the base — the
 * preview, a download, a page preview's links — names the folder without knowing about it.
 */
export function folderBase(sessionBase: string, folder: string, home: string): string {
  return !folder || folder === home ? sessionBase : `${sessionBase}/folders/${encodeURIComponent(folder)}`;
}
