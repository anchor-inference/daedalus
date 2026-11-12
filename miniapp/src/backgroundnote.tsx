// The one line that says an agent link or a contact dies with the window. A native installation runs
// its agent, its door and its public address under the desktop application, so quitting it takes all
// of them offline: another person's agent finds nothing there, and contacts show this side as gone.
// There is no free relay to hold the door open while the machine's own application is closed, so the
// way to keep it open is to keep the application running in the background, and this offers that at
// the moment it matters — once a link or a contact exists — instead of turning it on for everyone.
//
// Nothing is drawn on a server (it has no window to close), in a browser or Telegram (no shell to ask),
// in Docker mode (the containers outlive the application) or when closing already keeps it running.

import { useEffect, useState } from "react";
import { t } from "./i18n";
import { ADDRESS, type PublicAddress } from "./publicaddress";
import type { DeskInfo } from "./quick/bridge";
import { useQuery } from "./store";

/** Whether the note is due: a native installation whose shell says closing the window quits it. A
 *  shell older than the setting reports no `background`, and is not offered a switch it cannot hear. */
export function backgroundNoteDue(native: boolean | undefined, info: DeskInfo | null): boolean {
  return native === true && !!info && info.background === false;
}

export function BackgroundNote() {
  const desk = typeof window === "undefined" ? undefined : window.daedalus?.desk;
  const { data } = useQuery<PublicAddress>(desk ? ADDRESS : null, { staleMs: 5000 });
  const [info, setInfo] = useState<DeskInfo | null>(null);
  useEffect(() => {
    void desk?.settings().then(setInfo).catch(() => setInfo(null));
  }, [desk]);
  if (!desk || !backgroundNoteDue(data?.native, info)) return null;
  async function keep() {
    const next = await desk!.setSettings({ background: true }).catch(() => null);
    if (next) setInfo(next);
  }
  return (
    <div className="sub" role="note" data-background-note="">
      {t("background.note")} <button type="button" className="btn small" data-background-on="" onClick={() => void keep()}>{t("background.on")}</button>
    </div>
  );
}
