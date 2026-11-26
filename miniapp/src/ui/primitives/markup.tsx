// The smallest pieces: a key, a badge, a status dot. Kept apart from the controls, which reach the
// router for a link, so a module that only draws a dot does not load the router with it.

import type { ReactNode } from "react";

/** A key or a chord, as the keyboard shows it. */
export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>;
}

/** A count or a short word beside a name. Its tone is one of the three accents or none. */
export function Badge({ children, tone, className = "" }: { children: ReactNode; tone?: "live" | "attention" | "accent"; className?: string }) {
  return <span className={`badge ${tone ?? ""} ${className}`.replace(/\s+/g, " ").trim()}>{children}</span>;
}

/** A state as a dot: live (running, connected), attention (needs the operator, failed) or idle.
    `status` takes a session's own word instead, for the dots that already speak it. A dot never says
    anything alone: the word beside it does. */
export function Dot({ tone, status, className = "" }: { tone?: "live" | "attention" | "idle"; status?: string; className?: string }) {
  return <span className={`dot ${tone ?? status ?? "idle"} ${className}`.trim()} aria-hidden="true" />;
}
