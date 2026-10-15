// Why a member's attempt stands where it does: the step it waits at or was stopped at, on the team's
// cards and under the staff view's header, and every step in the details. A member the host stopped
// at a deadline used to read only "exited"; the reason was in the database and nowhere on screen.

import type { MemberCondition } from "../api";
import { t } from "../i18n";
import { Icon } from "../icons";
import { blockingCondition, conditionText, conditionTone } from "./model";

/** The one step in the way, in one line: "First output: none for 5m, deadline 08:12". Nothing when
 *  every step is through. */
export function ConditionLine({ conditions, now }: { conditions: MemberCondition[] | null | undefined; now?: number }) {
  // An attempt that ended without a report and without any step failing was ended by someone's
  // decision (a release, a stop), which the session's end reason already says; on every card of
  // every member ever stopped it would be red noise. The details still list it.
  const found = blockingCondition((conditions ?? []).filter((c) => c.reason !== "ended"));
  if (!found) return null;
  const tone = conditionTone(found);
  return (
    <span className={`staff-cond ${tone}`} data-condition={found.name} data-tone={tone} title={found.message}>
      <Icon name={tone === "bad" ? "alert" : "clock"} size={14} />
      <span className="staff-cond-text">
        <b>{t(`staff.cond.name.${found.name}`)}</b>: {conditionText(found, now)}
      </span>
    </span>
  );
}

/** Every step of the attempt, in order, each with its reason. */
export function ConditionList({ conditions, now }: { conditions: MemberCondition[]; now?: number }) {
  return (
    <ul className="staff-cond-list" data-conditions>
      {conditions.map((c) => {
        const tone = conditionTone(c);
        return (
          <li key={c.name} className={`staff-cond-item ${tone}`} data-condition={c.name} data-tone={tone} title={c.message}>
            <span className="staff-cond-mark" aria-hidden />
            <span className="staff-cond-name">{t(`staff.cond.name.${c.name}`)}</span>
            <span className="staff-cond-why">{conditionText(c, now)}</span>
          </li>
        );
      })}
    </ul>
  );
}
