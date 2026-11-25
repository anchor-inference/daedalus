// Settings → Desktop app: the shortcuts of Quick Chat, the appshots and dictation, running in the background, and what
// this system allows. The values belong to the desktop shell, not to the host — each computer the
// application runs on keeps its own — so they are read and written through window.daedalus.desk.

import { useCallback, useEffect, useState } from "react";
import { t } from "../i18n";
import { acceleratorFrom, showAccelerator } from "../quick/accelerator";
import type { DeskInfo, DeskSettings, ShortcutName } from "../quick/bridge";
import { Row, SetBlock, SetGroup } from "../settingsrow";
import { Button, Switch, confirmDialog } from "../ui/primitives";

/** What a system shortcut runs where the desktop refuses the application one of its own: a second
 *  start with the flag does what the shortcut would (desktop/shell/main.js). Commands, not words. */
const COMMANDS = ["daedalus --quick-chat", "daedalus --appshot", "daedalus --region", "daedalus --voice"];

export function DesktopPanel() {
  const desk = window.daedalus?.desk;
  const [info, setInfo] = useState<DeskInfo | null>(null);
  const [recording, setRecording] = useState<ShortcutName | null>(null);
  useEffect(() => {
    void desk?.settings().then(setInfo).catch(() => setInfo(null));
  }, [desk]);
  const save = useCallback(async (patch: Partial<DeskSettings>) => {
    const next = await desk?.setSettings(patch);
    if (next) setInfo(next);
  }, [desk]);

  // While a shortcut is being recorded the next combination is it; Esc gives up.
  useEffect(() => {
    if (!recording || !info) return;
    const mac = info.platform === "darwin";
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault();
      e.stopPropagation();
      if (e.key === "Escape") return setRecording(null);
      const accelerator = acceleratorFrom(e, mac);
      if (!accelerator) return;
      setRecording(null);
      void save({ [recording]: accelerator });
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [recording, info, save]);

  if (!desk) return <SetGroup><SetBlock><span className="set-hint">{t("settings.desktop.unavailable")}</span></SetBlock></SetGroup>;
  if (!info) return <div className="empty">{t("common.loading")}</div>;
  const mac = info.platform === "darwin";

  // The anchor is spelled out at each call, not built from the name, so the settings index test finds
  // the row its link points at by reading this file.
  const shortcut = ({ name, setting }: { name: ShortcutName; setting: string }, title: string, desc: string) => {
    const value = info.settings[name];
    const report = info.report[name];
    const refused = info.errors[name];
    const problem = refused ? t(`settings.desktop.shortcut.${refused}`) : report && !report.ok && report.reason !== "off" ? t(`settings.desktop.shortcut.${report.reason}`) : "";
    return (
      <Row title={title} desc={<>
        {desc}
        {problem ? <span className="settings-desktop-note bad" role="alert"> {problem}</span>
          : report?.ok ? <span className="settings-desktop-note ok"> {t("settings.desktop.shortcut.ok")}</span> : null}
      </>} data-setting={setting}>
        <span className="settings-desktop-shortcut">
          <kbd className={recording === name ? "recording" : ""}>{recording === name ? t("settings.desktop.shortcut.record") : value ? showAccelerator(value, mac) : t("settings.desktop.shortcut.none")}</kbd>
          <Button size="sm" onClick={() => setRecording(recording === name ? null : name)}>{t("settings.desktop.shortcut.change")}</Button>
          {value !== info.defaults[name] && <Button size="sm" onClick={() => void save({ [name]: info.defaults[name] })}>{t("settings.desktop.shortcut.reset")}</Button>}
          {value && <Button size="sm" variant="quiet" onClick={() => void save({ [name]: "" })}>{t("settings.desktop.shortcut.off")}</Button>}
        </span>
      </Row>
    );
  };

  // Where there is a tray its menu has Open and Quit; where there is none the way back is opening the
  // application again, and Quit is the row below or the File menu.
  const backgroundNote = info.tray.note === "gnome" ? t("settings.desktop.background.gnome") : info.tray.ready ? t("settings.desktop.background.tray") : t("settings.desktop.background.notray");
  const quit = desk.quit;
  return (
    <div className="settings-desktop">
      <SetGroup title={t("settings.desktop.shortcuts")} help={info.wayland ? undefined : t("settings.desktop.shortcuts.sub")}>
        {shortcut({ name: "quickChat", setting: "desktop.quickChat" }, t("settings.desktop.quick.title"), t("settings.desktop.quick.desc"))}
        {shortcut({ name: "appshot", setting: "desktop.appshot" }, t("settings.desktop.appshot.title"), t("settings.desktop.appshot.desc"))}
        {/* A shell older than the area appshot and the voice shortcut reports neither: no row for a
            shortcut it would not register. */}
        {"region" in info.settings && shortcut({ name: "region", setting: "desktop.region" }, t("settings.desktop.region.title"), t("settings.desktop.region.desc"))}
        {"voice" in info.settings && shortcut({ name: "voice", setting: "desktop.voice" }, t("settings.desktop.voice.title"), t("settings.desktop.voice.desc"))}
        {info.wayland && (
          <SetBlock className="settings-desktop-wayland">
            <span className="set-hint">{t("settings.desktop.wayland")}</span>
            <span className="settings-desktop-commands">{COMMANDS.map((command) => <code key={command}>{command}</code>)}</span>
          </SetBlock>
        )}
      </SetGroup>
      {/* A shell older than the setting reports no `background` and would not hear the switch. */}
      {(info.background !== undefined || mac || quit) && (
        <SetGroup title={t("settings.desktop.app")}>
          {info.background !== undefined && (
            <Row title={t("settings.desktop.background.title")} desc={<>{t("settings.desktop.background.desc")}<span className="settings-desktop-note"> {backgroundNote}</span></>} data-setting="desktop.background">
              <Switch checked={info.background} onChange={(next) => void save({ background: next })} label={t("settings.desktop.background.title")} />
            </Row>
          )}
          {mac && (
            <Row title={t("settings.desktop.screen.title")} desc={t(info.screen === "granted" ? "settings.desktop.screen.granted" : info.screen === "not-determined" ? "settings.desktop.screen.unknown" : "settings.desktop.screen.denied")}>
              {info.screen !== "granted" && <Button size="sm" onClick={() => desk.openScreenSettings()}>{t("settings.desktop.screen.open")}</Button>}
            </Row>
          )}
          {quit && (
            <Row title={t("settings.desktop.quit.title")} desc={t("settings.desktop.quit.desc")} data-setting="desktop.quit">
              <Button variant="danger" size="sm" data-desktop-quit="" onClick={() => void confirmDialog({ title: t("settings.desktop.quit.ask"), action: t("settings.desktop.quit.button"), danger: true }).then((yes) => yes && quit())}>{t("settings.desktop.quit.button")}</Button>
            </Row>
          )}
        </SetGroup>
      )}
    </div>
  );
}
