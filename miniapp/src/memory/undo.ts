// The undo every memory change offers: the host journals the change and answers with its history
// entry, and the toast's Undo asks for that entry back.

import { api } from "../api";
import { t } from "../i18n";
import { errorText } from "../ui";
import { toast } from "../ui/dialogs";

export type Undoable = { history_id?: number | null };

/** A toast for a change, with Undo when the host journalled it. */
export function undoToast(text: string, history: number | null | undefined, after: () => void): void {
  if (!history) {
    toast(text);
    return;
  }
  toast(text, {
    undo: () => {
      api.post(`/api/memory/history/${history}/undo`, {}).then(() => { toast(t("memory.history.restored")); after(); }, (e) => toast(errorText(e)));
    },
  });
}
