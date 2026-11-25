// The pieces a phone's list page is built from: a group's label, the filter chips over a list, an empty or
// failed list's state, a list's skeleton rows, a banner and the creation pill. They keep the `ph-` names
// the phone's list screens (the Inbox, the board, the terminals, the chats) and their checks select; the
// stylesheet is ui/parts/phonepieces.css.
//
//   SectionHeader   a list's group label: 15/600 grey, a count, an optional action at the right
//   Chip, ChipBar   a 32 px filter pill (44 px to the finger) and the scrolling row that holds them
//   EmptyState      a title, the hint and one action, centred, for empty, error and filtered-empty
//   RowSkeleton     a list's placeholder rows at the real row height while it loads
//   Banner          a 32 px strip under the top bar (offline) or a tinted note with an action
//   NewChatPill     the one accent creation pill, floating over a list

import { Icon, type IconName } from "../../icons";
import { t } from "../../i18n";
import type { ReactNode } from "react";

export function SectionHeader({ children, count, action, tone }: { children: ReactNode; count?: ReactNode; action?: ReactNode; tone?: "warn" | "bad" }) {
  return (
    <div className={`ph-sec ${tone ?? ""}`}>
      <span className="ph-sec-t">{children}</span>
      {count !== undefined && count !== null && <span className="ph-sec-n">{count}</span>}
      {action && <span className="ph-sec-act">{action}</span>}
    </div>
  );
}

// ── choices ─────────────────────────────────────────────────────────────────────────────────────

export function Chip({ on, tone, count, onClick, children, label }: { on?: boolean; tone?: "warn"; count?: ReactNode; onClick: () => void; children: ReactNode; label?: string }) {
  return (
    <button type="button" className={`ph-chip ${on ? "on" : ""} ${tone ?? ""}`} aria-pressed={!!on} onClick={onClick} aria-label={label}>
      <span className="ph-chip-l">{children}</span>
      {count !== undefined && count !== null && <span className="ph-chip-n">{count}</span>}
    </button>
  );
}

export function ChipBar({ children, label }: { children: ReactNode; label: string }) {
  return <div className="ph-chips" role="group" aria-label={label}>{children}</div>;
}

// ── states ──────────────────────────────────────────────────────────────────────────────────────

export function EmptyState({ icon, tone, title, body, action, className = "" }: { icon: IconName; tone?: "bad" | "ok"; title: ReactNode; body?: ReactNode; action?: ReactNode; className?: string }) {
  return (
    <div className={`ph-empty ${className}`} role={tone === "bad" ? "alert" : undefined}>
      <span className={`ph-empty-ico ${tone ?? ""}`}><Icon name={icon} size={22} /></span>
      <span className="ph-empty-t">{title}</span>
      {body && <span className="ph-empty-m">{body}</span>}
      {action}
    </div>
  );
}

/** A list's rows while it loads, at the rows' real height, so nothing jumps when they arrive. */
export function RowSkeleton({ rows = 6, lead = "round" }: { rows?: number; lead?: "round" | "square" | "none" }) {
  const widths = [[62, 38], [48, 44], [70, 30], [55, 35], [66, 42], [40, 33], [58, 40], [52, 36]];
  return (
    <div aria-busy="true" aria-label={t("common.loading")}>
      {widths.slice(0, rows).map(([a, b], i) => (
        <div key={i} className="ph-skrow">
          {lead !== "none" && <span className="ph-sk" style={{ width: 36, height: 36, borderRadius: lead === "round" ? "50%" : 10 }} />}
          <span className="grow"><span className="ph-sk" style={{ height: 14, width: `${a}%` }} /><span className="ph-sk" style={{ height: 11, width: `${b}%`, marginTop: 8 }} /></span>
        </div>
      ))}
    </div>
  );
}

/** `strip`: the 32 px line under the top bar (offline, reconnecting). Otherwise a tinted note with an
 *  icon, the words, a second line and one action under them. */
export function Banner({ tone, icon, children, sub, action, strip, className = "" }: { tone?: "bad" | "warn" | "info"; icon?: IconName; children: ReactNode; sub?: ReactNode; action?: ReactNode; strip?: boolean; className?: string }) {
  if (strip) {
    return <div className={`ph-strip ${tone ?? ""} ${className}`} role="status">{icon && <Icon name={icon} size={14} />}<span>{children}</span></div>;
  }
  return (
    <div className={`ph-note ${tone ?? ""} ${className}`} role={tone === "bad" ? "alert" : "status"}>
      {icon && <Icon name={icon} size={18} />}
      <div className="ph-note-main">
        <div>{children}</div>
        {sub && <div className="ph-note-m">{sub}</div>}
        {action && <div className="ph-note-act">{action}</div>}
      </div>
    </div>
  );
}

export function NewChatPill({ label, icon = "compose", onClick }: { label: string; icon?: IconName; onClick: () => void }) {
  return (
    <button type="button" className="ph-newchat" onClick={onClick}>
      <Icon name={icon} size={18} />
      <span>{label}</span>
    </button>
  );
}
