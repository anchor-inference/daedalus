// The undo every memory change offers: the host journals the change and answers with its history
// entry, and the toast's Undo asks for that entry back.

import { api } from "../api";
import { errorText } from "../ui";
import { toast } from "../ui/dialogs";

export type Undoable = { history_id?: number | null };

/** The Undo a journalled change offers. A change with nothing to undo says nothing: it shows where it was made. */
export function undoToast(text: string, history: number | null | undefined, after: () => void): void {
  if (!history) return;
  toast(text, {
    undo: () => {
      api.post(`/api/memory/history/${history}/undo`, {}).then(() => { after(); }, (e) => toast(errorText(e)));
    },
  });
}
