// The app in the desktop's main window, talking to the shell around it: it tells the tray what runs
// and what waits, does what the tray's menu asks (pause or resume the companion), and moves to a
// conversation Quick Chat's "open" names without a reload. Nothing happens outside the desktop
// application, where `window.daedalus.desk` is absent.

import { useEffect } from "react";
import { api, type NotificationSummary, type SessionList } from "../api";
import { navigate } from "../router";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { deskStatus, type Companion } from "./quick";

/** Pauses or resumes the companion. The endpoint is the companion's own resource, written back with
 *  the one field `GET /api/companion` reports. */
export async function setCompanionPaused(paused: boolean): Promise<void> {
  await api.patch("/api/companion", { paused });
  invalidate("/api/companion");
}

export function useDeskBridge(authed: boolean, summary: NotificationSummary, toast: (text: string) => void): void {
  const desk = typeof window === "undefined" ? undefined : window.daedalus?.desk;
  const on = !!desk && authed;
  // The same key the sidebar reads, so the tray costs no request of its own while the list is open.
  const sessions = useQuery<SessionList>(on ? "/api/sessions" : null, { pollMs: 60000, staleMs: 5000 });
  // 404 on an installation without the companion: the tray then offers no Pause.
  const companion = useQuery<Companion>(on ? "/api/companion" : null, { pollMs: 60000, staleMs: 30000 });
  const status = deskStatus(sessions.data?.sessions, summary, companion.error ? null : companion.data ?? null);
  const paused = status.companion ? status.companion.paused : null;
  useEffect(() => {
    if (on) desk!.status(status);
    // The status is a new object every render; what it says is these four values.
  }, [on, status.running, status.waiting, paused]);
  useEffect(() => (on ? desk!.onNavigate((path) => navigate(path)) : undefined), [on]);
  useEffect(() => {
    if (!on) return undefined;
    return desk!.onAction((name) => {
      if (name !== "pause-companion" && name !== "resume-companion") return;
      setCompanionPaused(name === "pause-companion").catch((e) => toast(errorText(e)));
    });
  }, [on, toast]);
}
