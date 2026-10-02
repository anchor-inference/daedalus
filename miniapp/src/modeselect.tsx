import { ControlTrigger } from "./ui/control-trigger";
// How the agent works in this session, chosen from the chip beside the model: the mode (the plain
// agent or one of the configured modes) and, apart from it, the YAGNI switch. The chip stays on a
// phone while a run is on — the model chip gives way there, this one does not — so it says only what
// fits: the mode's name, and the word YAGNI while the switch is on.

import { useEffect, useRef, useState } from "react";
import { Popover, Sheet } from "./ui/dialogs";
import { Icon } from "./icons";
import { DICT, t } from "./i18n";

export type ModeInfo = { name: string; description: string };

export type ModeSelectProps = {
  /** The session's mode; empty is the plain agent. */
  mode: string;
  modes: ModeInfo[];
  yagni: boolean;
  onChooseMode: (mode: string) => void;
  onYagni: (on: boolean) => void;
  /** Phones: a sheet instead of a popover. */
  sheet: boolean;
};

/** A mode's name in the reader's language: the built-in ones are translated, an operator's own is shown as written. */
export function modeName(mode: string): string {
  if (!mode) return t("composer.mode.agent");
  return DICT[`composer.mode.name.${mode}`] ? t(`composer.mode.name.${mode}`) : mode;
}

/** What a mode does, in the reader's language where the app knows it, else the host's own words. */
export function modeHint(mode: ModeInfo | null): string {
  if (!mode) return t("composer.mode.hint.agent");
  return DICT[`composer.mode.hint.${mode.name}`] ? t(`composer.mode.hint.${mode.name}`) : mode.description;
}

/** The chip's label and its tooltip: the mode, and the switch when it is on. */
export function modeChip(mode: string, yagni: boolean): { label: string; title: string } {
  const label = modeName(mode);
  return { label, title: yagni ? `${label} · ${t("composer.yagni")}` : label };
}

export function ModeSelect({ mode, modes, yagni, onChooseMode, onYagni, sheet }: ModeSelectProps) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  // The switch answers at once; the host's word arrives with the next poll and replaces it.
  const [shown, setShown] = useState<boolean | null>(null);
  useEffect(() => setShown(null), [yagni]);
  const on = shown ?? yagni;
  const chip = modeChip(mode, on);
  const pick = (name: string) => {
    setOpen(false);
    if (name !== mode) onChooseMode(name);
  };
  const toggle = () => {
    setShown(!on);
    onYagni(!on);
  };
  const list = (
    <div className="mode-list">
      <div className="menu-heading sub">{t("session.mode")}</div>
      {[null, ...modes].map((m) => {
        const name = m?.name ?? "";
        const current = name === mode;
        return (
          <button key={name || "agent"} type="button" role="menuitemradio" aria-checked={current} className={`model-row mode-row ${current ? "on" : ""}`} onClick={() => pick(name)}>
            <span className="grow model-text">
              <span className="truncate">{modeName(name)}</span>
              <span className="sub clamp-2">{modeHint(m)}</span>
            </span>
            {current && <Icon name="check" size={16} />}
          </button>
        );
      })}
      <div className="menu-sep" />
      <button type="button" role="menuitemcheckbox" aria-checked={on} className={`model-row mode-row yagni-row ${on ? "on" : ""}`} onClick={toggle}>
        <span className="grow model-text">
          <span className="truncate">{t("composer.yagni")}</span>
          <span className="sub clamp-2">{t("composer.yagni.hint")}</span>
        </span>
        <span className={`mode-switch ${on ? "on" : ""}`} aria-hidden="true"><i /></span>
      </button>
    </div>
  );
  return (
    <>
      <ControlTrigger ref={trigger} type="button" className={`composer-mode ${on ? "yagni" : ""} ${open ? "on" : ""}`} onClick={() => setOpen(!open)} title={chip.title} aria-label={`${t("composer.mode.for")}: ${chip.title}`} aria-haspopup="menu" aria-expanded={open}>
        <span className="truncate">{chip.label}</span>
        {on && <span className="mode-yagni">{t("composer.yagni")}</span>}

      </ControlTrigger>
      {open && sheet && (
        <Sheet title={t("composer.mode.for")} onClose={() => setOpen(false)} className="mode-sheet">
          {list}
        </Sheet>
      )}
      {open && !sheet && (
        <Popover anchor={trigger.current} onClose={() => setOpen(false)} className="mode-menu" label={t("composer.mode.for")}>
          {list}
        </Popover>
      )}
    </>
  );
}
