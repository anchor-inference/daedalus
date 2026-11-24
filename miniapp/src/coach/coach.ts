// The prompt coach's window, the parts that are arithmetic and words rather than drawing: what the coach
// is told first, and where the floating window stands.

import { t } from "../i18n";

/** The coach as `GET/POST /api/sessions/<id>/coach` describes it. */
export type CoachResult = { title: string; prompt: string; path: string; at: string };
export type CoachView = { session_id: string; parent_id: string; finished: boolean; result: CoachResult | null; created?: boolean };

/**
 * The coach's first turn, in the reader's language: the coach answers in the language it is spoken to
 * in, and a draft alone, in whatever language the draft is, would leave that to chance. An empty draft
 * asks for help writing one, and the coach starts by asking what it is for.
 */
export function seedMessage(draft: string): string {
  const text = draft.trim();
  return text ? t("coach.seed", { draft: text }) : t("coach.seed.empty");
}

/** Where the coach's window is remembered; the side question keeps its own. */
export const POSITION_KEY = "daedalus.coach.position";
