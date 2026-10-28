// The schedules dialog is opened from places that know nothing of each other: a card under a turn, a
// chat's overflow menu. One host in the shell draws it, and anyone asks it to open; the dialog itself
// is a lazy chunk, so the chat does not carry the schedules screen until someone opens it.

import { lazy, Suspense, useEffect, useState } from "react";
import { retried } from "../chunks";

export type SchedulesRequest = { sessionId: string | null; selected: string | null };

let setRequest: ((request: SchedulesRequest | null) => void) | null = null;

/** Open the schedules dialog: one conversation's schedules when ``sessionId`` is given, with ``selected`` open. */
export function openSchedules(request: SchedulesRequest): void {
  setRequest?.(request);
}

const SchedulesDialog = lazy(retried(() => import("./SchedulesDialog"), (m) => ({ default: m.SchedulesDialog })));

export function SchedulesDialogHost({ onOpenSession }: { onOpenSession: (id: string) => void }) {
  const [request, set] = useState<SchedulesRequest | null>(null);
  useEffect(() => {
    setRequest = set;
    return () => {
      if (setRequest === set) setRequest = null;
    };
  }, []);
  if (!request) return null;
  return (
    <Suspense fallback={null}>
      <SchedulesDialog
        // A second open while one is up starts the dialog over on what was asked for.
        key={`${request.sessionId ?? ""}:${request.selected ?? ""}`}
        sessionId={request.sessionId}
        selected={request.selected}
        onClose={() => set(null)}
        onOpenSession={(id) => {
          set(null);
          onOpenSession(id);
        }}
      />
    </Suspense>
  );
}
