// One line on a member's card while the project's route runs it on another runtime than its own:
// without it a fallback reads as the member's own choice, and the reason (a spent window, a missing
// sign-in) is what tells the operator when it will be back.

import { t } from "../i18n";
import { type Harness, HARNESS_NAMES, type Staff } from "./team";

export function RouteLine({ member }: { member: Pick<Staff, "harness" | "route_home"> }) {
  const home = member.route_home;
  if (!home) return null;
  const own = HARNESS_NAMES[home.harness as Harness] ?? home.harness;
  return (
    <div className="sub staff-route truncate" data-route-fallback title={home.reason || undefined}>
      {t("team.route.fallback", { runtime: HARNESS_NAMES[member.harness], own })}{home.reason ? ` — ${home.reason}` : ""}
    </div>
  );
}
