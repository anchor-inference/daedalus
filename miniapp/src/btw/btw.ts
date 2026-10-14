// The side question (/btw), the parts that are words rather than drawing: how the composer knows one
// when it is typed, and the line it adds to the slash palette.

import type { SlashCommand } from "../api";
import { t } from "../i18n";

/** The side question as `POST /api/sessions/<id>/btw` describes it: the session that answers it. */
export type BtwView = { session_id: string; parent_id: string };

/**
 * The question in a composer line that asks one, or null for a line that does not. `/btw` alone is a
 * side question with nothing asked yet, which opens the window on its empty field. Only the first line
 * decides: `/btwx` is another command, and a message that mentions /btw later on is a message.
 */
export function btwQuestion(text: string): string | null {
  const found = /^\/btw(?:\s+([\s\S]*))?$/i.exec(text.trim());
  return found ? (found[1] ?? "").trim() : null;
}

/** The palette's line for /btw: the app answers it itself, so the host's list of commands never names it. */
export function btwCommand(): SlashCommand {
  return { name: "btw", args: t("btw.args"), description: t("btw.command"), scope: "app", confirm: false };
}
