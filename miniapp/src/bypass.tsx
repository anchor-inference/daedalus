// The mark of a session set to "Bypass all checks", beside its title in the header: drawn like the HOST pill
// and in the danger colour, because a session that asks nobody about anything must be told apart at a glance
// from one that does. The switch itself is in the composer's toggles menu.

import { t } from "./i18n";
import { Icon } from "./icons";

export function BypassMark({ on }: { on: boolean }) {
  if (!on) return null;
  return (
    <span className="bypass-mark" data-bypass title={t("session.bypass.title")}>
      <Icon name="alert" size={11} />
      {t("session.bypass.mark")}
    </span>
  );
}
