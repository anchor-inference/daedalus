// Whether a project is one the operator keeps, or the scratch project a plain chat was given.

import type { ProjectRef } from "./api";

/** A project is the installation's own or one kept on purpose; a chat's scratch project is ephemeral
 *  until a second chat joins it or the operator keeps it. Lists of projects show only the first kind,
 *  so a chat never appears twice: once as a chat and once as a "project" of one chat. */
export function isProject(project: Pick<ProjectRef, "settings">): boolean {
  return !!project.settings.system || !project.settings.ephemeral;
}
