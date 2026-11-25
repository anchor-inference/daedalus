// The schedules over a chat, in a sheet: that chat's schedules, with the one a schedule card named open.
// Opened from a schedule card under a turn, or from the chat's menu. The list and the open schedule
// take turns in it, the schedule with a way back to the list; Escape and the close button close it.

import { useState } from "react";
import { t } from "../i18n";
import { Sheet, toast } from "../ui/primitives";
import { SchedulesBrowser } from "./SchedulesBrowser";

export function SchedulesDialog({ sessionId, selected: initial, onClose, onOpenSession }: {
  sessionId: string | null;
  selected: string | null;
  onClose: () => void;
  onOpenSession: (id: string) => void;
}) {
  const [selected, setSelected] = useState<string | null>(initial);
  return (
    <Sheet title={t("nav.schedules")} size="lg" className="schedules-dialog" onClose={onClose}>
      <SchedulesBrowser
        variant="sheet"
        sessionId={sessionId}
        selected={selected}
        onSelect={setSelected}
        toast={(text) => toast(text)}
        onOpenSession={onOpenSession}
      />
    </Sheet>
  );
}
