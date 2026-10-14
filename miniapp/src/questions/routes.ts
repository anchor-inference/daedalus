// Where the Questions tab's lists live on the host, and which events concern each: pure, so the
// rules are tested without the app's connection around them.

/** Whose list a tab shows: one project's in its focus mode, all of them in the main chat, or one
 *  ordinary session's side questions beside it. */
export type QuestionScope = { projectId: string } | { sessionId: string } | "all";

export const QUESTIONS_KEY = "/api/questions";

export function questionsKey(scope: QuestionScope): string {
  if (scope === "all") return QUESTIONS_KEY;
  if ("sessionId" in scope) return `${QUESTIONS_KEY}?session=${encodeURIComponent(scope.sessionId)}`;
  return `${QUESTIONS_KEY}?project=${encodeURIComponent(scope.projectId)}`;
}

/** Where a batch of answers from this list is sent. */
export function answerPath(scope: QuestionScope): string {
  if (scope === "all") return "/api/asks/answer";
  if ("sessionId" in scope) return `/api/sessions/${encodeURIComponent(scope.sessionId)}/asks/answer`;
  return `/api/projects/${encodeURIComponent(scope.projectId)}/asks/answer`;
}

/** Where the operator puts one side question aside, or null for a list that has none. */
export function dismissPath(scope: QuestionScope, askId: string): string | null {
  return scope !== "all" && "sessionId" in scope ? `/api/sessions/${encodeURIComponent(scope.sessionId)}/asks/${encodeURIComponent(askId)}/dismiss` : null;
}

/** Whether an event may change this list. A session's list changes only by its own `ask.side`; a
 *  project's by anything of that project; the main chat's by anything at all. */
export function concerns(scope: QuestionScope, event: { type: string; project_id?: string | null; session_id?: string | null }): boolean {
  if (scope === "all") return event.type !== "ask.side";
  if ("sessionId" in scope) return event.type === "ask.side" && event.session_id === scope.sessionId;
  return event.type !== "ask.side" && (!event.project_id || event.project_id === scope.projectId);
}
