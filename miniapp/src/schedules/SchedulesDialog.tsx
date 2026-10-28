// The schedules over a chat, in a wide dialog: the list on the left, the open schedule on the right.
// Opened from a schedule card under a turn, or from the chat's menu, on that chat's schedules. On a
// phone it is a full-screen sheet; Escape, the close button and the backdrop close it.

import { useEffect, useRef, useState } from "react";
import { t } from "../i18n";
import { Icon } from "../icons";
import { Overlay, toast, trapDialogTab, useLayer } from "../ui/dialogs";
import { SchedulesBrowser } from "./SchedulesBrowser";

export function SchedulesDialog({ sessionId, selected: initial, onClose, onOpenSession }: {
  sessionId: string | null;
  selected: string | null;
  onClose: () => void;
  onOpenSession: (id: string) => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const [selected, setSelected] = useState<string | null>(initial);
  useLayer(onClose);
  // Focus goes in once and back to whatever opened the dialog when it closes.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    return () => opener?.focus?.();
  }, []);
  return (
    <Overlay>
      <div className="sheet-backdrop schedules-backdrop" onClick={(e) => { e.stopPropagation(); onClose(); }}>
        <div
          ref={panel}
          tabIndex={-1}
          className="schedules-dialog"
          role="dialog"
          aria-modal="true"
          aria-label={t("nav.schedules")}
          onClick={(e) => e.stopPropagation()}
          onKeyDown={trapDialogTab}
        >
          <header className="schedules-dialog-head">
            <Icon name="calendar" size={16} />
            <h2>{t("nav.schedules")}</h2>
            <button type="button" className="iconbtn small" onClick={onClose} aria-label={t("common.close")} title={t("common.close")}><Icon name="close" size={16} /></button>
          </header>
          <SchedulesBrowser
            variant="dialog"
            sessionId={sessionId}
            selected={selected}
            onSelect={setSelected}
            toast={(text) => toast(text)}
            onOpenSession={onOpenSession}
          />
        </div>
      </div>
    </Overlay>
  );
}
