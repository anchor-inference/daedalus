// Whether the host still hears a staff member, in one line: under the staff view's header in full,
// and on the team's cards and rows as its warnings only, so a member whose team tools never loaded or
// who has gone silent is visible from the list, not only from inside it.

import type { ChannelHealth } from "../api";
import { relTime } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { healthParts } from "./model";

/** Whether the host still hears the member, in one line under its header; amber where something is wrong. */
export function HealthLine({ health, compact }: { health: ChannelHealth | null; compact?: boolean }) {
  const parts = healthParts(health);
  if (!health || parts.length === 0) return null;
  const shown = compact ? parts.filter((p) => p.warn).slice(0, 2) : parts;
  if (shown.length === 0) return null;
  return (
    <span className={`staff-health ${health.level} ${compact ? "compact" : ""}`} data-level={health.level} data-tools={health.team_tools}>
      <Icon name={health.level === "warn" ? "alert" : "check"} size={14} />
      {/* The parts wrap inside their own box: wrapping beside the mark put the last one flush left,
          under the mark, in the narrow side column. */}
      <span className="staff-health-parts">
        {shown.map((p) => (
          <span key={p.key} className={`staff-health-part ${p.warn ? "warn" : ""}`} data-part={p.key}>
            {t(p.key, { when: p.at ? relTime(p.at) : "", n: p.n ?? 0 })}
          </span>
        ))}
      </span>
    </span>
  );
}
