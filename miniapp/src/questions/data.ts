// What the Questions tab reads: the operator's list, one project's or every orchestrated project's,
// or one session's side questions, kept current by the event stream. A question the orchestrator adds
// is an `ask.pending`; one answered anywhere, or taken back, is an `ask.answered` or a
// `permission.resolved`; a side question left, answered or taken back is an `ask.side` — each says
// "read the list again", and the list is the truth.

import type { WaitingQuestion } from "../api";
import { useEvent, useStreamUp } from "../events";
import { invalidate, useQuery } from "../store";
import { concerns, questionsKey, type QuestionScope } from "./routes";

export { QUESTIONS_KEY, answerPath, concerns, dismissPath, questionsKey, type QuestionScope } from "./routes";

/** The list, or null before the first answer from the host. */
export function useQuestions(scope: QuestionScope | null): { questions: WaitingQuestion[] | null; error: string | null } {
  const live = useStreamUp();
  const key = scope ? questionsKey(scope) : null;
  const query = useQuery<{ questions: WaitingQuestion[] }>(key, { pollMs: live ? 60000 : 10000, staleMs: 2000 });
  useEvent(["ask.", "permission.", "project.changed"], (event) => {
    if (scope && key && concerns(scope, event)) invalidate(key);
  }, [key]);
  // An answer of the wrong shape (an older host) draws an empty list rather than breaking the panel.
  const questions = query.data && Array.isArray(query.data.questions) ? query.data.questions : null;
  return { questions, error: query.error };
}
