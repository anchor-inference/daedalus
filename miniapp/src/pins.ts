// Pinning a project or a chat to the top of the left column. The pin lives on the server, keyed by
// the project (a chat is pinned by its scratch project), so the desktop, the phone and the desktop
// app list the same block; grouping.ts decides where a pinned folder is drawn.

import { api } from "./api";
import { t } from "./i18n";
import { invalidate } from "./store";
import { errorText } from "./ui";
import { type MenuItem, toast } from "./ui/dialogs";

/** Pin or unpin, then read the listing again so the block and the sections below it agree. */
export async function setPinned(projectId: string, pinned: boolean): Promise<void> {
  try {
    await api.put(`/api/projects/${encodeURIComponent(projectId)}/pin`, { pinned });
    invalidate("/api/sessions");
  } catch (error) {
    toast(errorText(error));
  }
}

/** The menu's command, worded for what it does now, with the sidebar's key beside it. */
export function pinCommand(projectId: string, pinned: boolean, opts: { key?: boolean } = {}): Exclude<MenuItem, "-"> {
  return {
    label: t(pinned ? "pin.off" : "pin.on"),
    icon: "pushpin",
    hint: opts.key ? t("side.key.pin") : undefined,
    onSelect: () => void setPinned(projectId, !pinned),
  };
}
