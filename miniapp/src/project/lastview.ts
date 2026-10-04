import { projectHome, projectPagePath } from "../router";

type LastView = { version: 1; project_id: string; page: string | null; coordinator_session_id: string };
const KEY = "daedalus.project.last_view";
const PAGES = new Set(["team", "board", "journal", "brief", "attention"]);

export function rememberProjectView(projectId: string, page: string | null, coordinatorSessionId: string): void {
  if (page && !PAGES.has(page)) return;
  try { localStorage.setItem(KEY, JSON.stringify({ version: 1, project_id: projectId,
    page, coordinator_session_id: coordinatorSessionId } satisfies LastView)); } catch { /* private mode */ }
}

export function storedProjectView(): LastView | null {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) ?? "null");
    return value?.version === 1 && typeof value.project_id === "string" &&
      (value.page === null || PAGES.has(value.page)) && typeof value.coordinator_session_id === "string"
      ? value as LastView : null;
  } catch { return null; }
}

export function projectViewPath(view: LastView): string {
  return view.page ? projectPagePath(view.project_id, view.page) : projectHome(view.project_id);
}
