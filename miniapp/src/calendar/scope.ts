// The question every change to a recurring event has to ask: this one, or the whole series. Asked as
// a dialog the change waits on, so a drag, a resize, an edit and a delete all ask it the same way.

import { t } from "../i18n";
import { confirmDialog } from "../ui/primitives";
import type { Scope } from "./types";

/** `null` when the reader backed out: nothing is sent then. */
export async function askScope(action: "change" | "delete"): Promise<Scope | null> {
  const danger = action === "delete";
  const answer = await confirmDialog({
    title: t(danger ? "cal.scope.delete" : "cal.scope.change"),
    body: t("cal.scope.body"),
    // The whole series first and the one event last: the last choice is the filled one, and the safer
    // answer to "change this" is the single event.
    choices: [{ label: t("cal.scope.all"), value: "all", danger }, { label: t("cal.scope.this"), value: "this", danger }],
  });
  return answer === "this" || answer === "all" ? answer : null;
}
