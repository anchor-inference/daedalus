// What a screen shows about push on this device: the Settings card, and the one-line nudge at the
// top of the phone's notification list while push is off. The test button lives with the rest of
// the notification settings, because it tests every channel and not push alone.

import { t } from "./i18n";
import { usePush } from "./push";
import { pathFor } from "./router";
import { go, useMedia } from "./shell";
import { Row, SetBlock, SetGroup } from "./settingsrow";
import { Button } from "./ui/primitives";

export function PushCard() {
  const { state, busy, error, enable, disable } = usePush();
  return (
    <SetGroup title={t("push.device")} data-push-state={state ?? "loading"} data-setting="notifications.push">
      <Row title={t("push.title")} desc={state === null ? t("common.loading") : t(`push.state.${state}`)}>
        {state === "off" && <Button variant="primary" size="sm" busy={busy} onClick={() => void enable()}>{t("push.enable")}</Button>}
        {state === "on" && <Button size="sm" busy={busy} onClick={() => void disable()}>{t("push.disable")}</Button>}
      </Row>
      {state === "needs-home-screen" && <SetBlock><ol className="push-steps"><li>{t("push.ios.share")}</li><li>{t("push.ios.add")}</li><li>{t("push.ios.open")}</li></ol></SetBlock>}
      {error && <SetBlock><span className="push-error">{error}</span></SetBlock>}
    </SetGroup>
  );
}

/** A line above the list on a phone, while this device could have push and does not. */
export function PushNudge() {
  const phone = !useMedia("(min-width: 1024px)");
  const { state } = usePush(phone);
  if (!phone || state !== "off") return null;
  const target = pathFor("settings", "notifications");
  return (
    <a className="push-nudge" href={target} onClick={(e) => go(e, target)}>
      {t("push.nudge")}
    </a>
  );
}
