import { useCallback } from "react";
import { navigate, pathFor } from "../router";
import { SchedulesBrowser } from "../schedules/SchedulesBrowser";
import { PageHeader, screenTitle } from "../ui/index";

/**
 * The Schedules screen: the same list and schedule pane as the dialog over a chat, filling the page.
 * The open schedule is in the address, so a link to one opens it and Back closes it on a phone.
 */
export function SchedulesScreen({ toast, onOpen, selected }: { toast: (t: string) => void; onOpen: (id: string) => void; selected?: string | null }) {
  // A row the reader picks is a step Back can undo; one the browser opened by itself, a change of row
  // or a close is not, so the address is replaced and Back leaves the screen as it should.
  const select = useCallback((key: string | null, auto?: boolean) => navigate(pathFor("schedules", key), { replace: !!auto || !key || !!selected }), [selected]);
  return (
    <>
      <PageHeader title={screenTitle("schedules")} />
      <div className="screen schedules-screen">
        <SchedulesBrowser variant="page" selected={selected ?? null} onSelect={select} toast={toast} onOpenSession={onOpen} />
      </div>
    </>
  );
}
