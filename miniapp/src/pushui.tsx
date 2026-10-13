// What a screen shows about push on this device: the Settings card, and the one-line nudge at the
// top of the phone's notification list while push is off. The test button lives with the rest of
// the notification settings, because it tests every channel and not push alone.

import { t } from "./i18n";
import { usePush } from "./push";
import { pathFor } from "./router";
import { go, useMedia } from "./shell";

export function PushCard() {
  const { state, busy, error, enable, disable } = usePush();
  return (
    <div className="card push-card" data-push-state={state ?? "loading"} data-setting="notifications.push">
      <div className="section-title" style={{ marginTop: 0 }}>{t("push.device")}</div>
      <div className="sub">{state === null ? t("common.loading") : t(`push.state.${state}`)}</div>
      {state === "needs-home-screen" && <ol className="push-steps"><li>{t("push.ios.share")}</li><li>{t("push.ios.add")}</li><li>{t("push.ios.open")}</li></ol>}
      {(state === "off" || state === "on") && (
        <div className="btnrow">
          {state === "off" ? (
            <button className="btn primary small" disabled={busy} onClick={() => void enable()}>{t("push.enable")}</button>
          ) : (
            <button className="btn small" disabled={busy} onClick={() => void disable()}>{t("push.disable")}</button>
          )}
        </div>
      )}
      {error && <div className="sub push-error">{error}</div>}
    </div>
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
