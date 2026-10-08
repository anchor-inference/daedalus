// "Open in Explorer", "Show in Finder", "Open in file manager": a folder or a file of the agent's,
// shown in the operator's own file manager. Only when that file manager is on the machine in front
// of the operator — the installation is native and the page was opened on that same machine — which
// the host says on /api/capabilities. On the server deployment reached from a phone there is no
// such file manager, and the action is not drawn at all rather than drawn and refused.
//
// The host decides what may be revealed: every request names a session or a project and a path
// relative to it, and the host confines it to that folder. Inside the desktop window the shell does
// the opening (Electron's shell, which knows the platform's file manager best); everywhere else the
// host runs the platform's own command. Either way the host has validated the path first.

import { api } from "./api";
import { useQuery } from "./store";
import type { Capabilities } from "./capabilities";
import { t } from "./i18n";
import { toast } from "./ui/dialogs";
import { Icon } from "./icons";

export type RevealPlatform = "windows" | "macos" | "linux";

/** What the host's capability answer says about revealing; absent on a host that predates it. */
export type RevealCapability = { available: boolean; platform: RevealPlatform };

/** What to reveal: a session's workspace (or one of its project's folders), or a project's folder, and a path inside it. */
export type RevealTarget = { session_id?: string; project_id?: string; folder_id?: string; path?: string };

type Revealed = { path: string; directory: boolean; opened: boolean };

/** The action's words for the platform the host runs on, which is the operator's platform here. */
export function revealLabel(platform: RevealPlatform): string {
  return t(`reveal.${platform}`);
}

/** Whether the page may offer the action at all. */
export function revealAvailable(caps: Pick<Capabilities, "reveal"> | undefined): RevealCapability | null {
  const reveal = caps?.reveal;
  return reveal?.available ? reveal : null;
}

/**
 * Reveal `target`. In the desktop window the host only resolves and confines the path (`run:
 * false`) and the shell opens it; elsewhere the host opens it itself. A failure is a toast: the
 * file may have been deleted since the turn wrote it, and the page has nothing better to show.
 */
export async function reveal(target: RevealTarget): Promise<boolean> {
  const shell = window.daedalus?.reveal;
  try {
    const answer = await api.post<Revealed>("/api/reveal", { ...target, run: !shell });
    if (shell) {
      const error = await shell(answer.path);
      if (error) throw new Error(error);
    }
    return true;
  } catch (e) {
    toast(t("reveal.failed", { error: e instanceof Error ? e.message : String(e) }));
    return false;
  }
}

/** The capability and the label, for a component that offers the action; null when it must not. */
export function useReveal(): { label: string; reveal: (target: RevealTarget) => Promise<boolean> } | null {
  const { data } = useQuery<Capabilities>("/api/capabilities", { staleMs: 20000 });
  const capability = revealAvailable(data);
  return capability ? { label: revealLabel(capability.platform), reveal } : null;
}

/** The action as an icon button, or nothing where it must not be offered. */
export function RevealButton({ target, className = "" }: { target: RevealTarget; className?: string }) {
  const revealer = useReveal();
  if (!revealer) return null;
  return (
    <button type="button" className={`iconbtn small reveal-btn ${className}`} onClick={(e) => { e.stopPropagation(); void revealer.reveal(target); }} aria-label={revealer.label} title={revealer.label}>
      <Icon name="external" size={14} />
    </button>
  );
}
