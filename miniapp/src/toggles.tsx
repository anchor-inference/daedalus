import { ControlTrigger } from "./ui/control-trigger";
// The two switches that change how the agent works in this session, behind the chip beside the
// model: YAGNI and autopilot. There is one agent configuration and nothing else to choose, so the
// chip names the agent while both are off and the switches that are on otherwise. It stays on a
// phone while a run is on — the model chip gives way there, this one does not — so it says only
// what fits.

import { useEffect, useRef, useState } from "react";
import { Popover, Sheet } from "./ui/dialogs";
import { t } from "./i18n";
import type { AutopilotStatus } from "./api";

export type TogglesSelectProps = {
  yagni: boolean;
  onYagni: (on: boolean) => void;
  /** The session's autopilot switch; absent where the session cannot have one (a role of its own). */
  autopilot?: AutopilotStatus | null;
  onAutopilot?: (on: boolean) => void;
  /** Phones: a sheet instead of a popover. */
  sheet: boolean;
};

/** What the autopilot row says under its name: off for the whole app, the turns it took, or what it does. */
export function autopilotHint(status: AutopilotStatus, on: boolean): string {
  if (!status.enabled) return t("composer.autopilot.disabled");
  if (on) return t("composer.autopilot.used", { used: String(status.used), allowed: String(status.allowed) });
  return t("composer.autopilot.hint");
}

/** The chip's tooltip: the agent, then each switch that is on. */
export function togglesTitle(yagni: boolean, autopilot: boolean): string {
  return [t("composer.mode.agent"), yagni && t("composer.yagni"), autopilot && t("composer.autopilot")].filter(Boolean).join(" · ");
}

export function TogglesSelect({ yagni, onYagni, autopilot, onAutopilot, sheet }: TogglesSelectProps) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  // A switch answers at once; the host's word arrives with the next poll and replaces it.
  const [shown, setShown] = useState<boolean | null>(null);
  useEffect(() => setShown(null), [yagni]);
  const on = shown ?? yagni;
  const toggle = () => {
    setShown(!on);
    onYagni(!on);
  };
  const [pilotShown, setPilotShown] = useState<boolean | null>(null);
  useEffect(() => setPilotShown(null), [autopilot?.on]);
  const offered = !!(autopilot?.eligible && onAutopilot);
  const pilotOn = offered && (pilotShown ?? !!autopilot?.on);
  const togglePilot = () => {
    setPilotShown(!pilotOn);
    onAutopilot?.(!pilotOn);
  };
  const title = togglesTitle(on, pilotOn);
  const list = (
    <div className="mode-list">
      <button type="button" role="menuitemcheckbox" aria-checked={on} className={`model-row mode-row yagni-row ${on ? "on" : ""}`} onClick={toggle}>
        <span className="grow model-text">
          <span className="truncate">{t("composer.yagni")}</span>
          <span className="sub clamp-2">{t("composer.yagni.hint")}</span>
        </span>
        <span className={`mode-switch ${on ? "on" : ""}`} aria-hidden="true"><i /></span>
      </button>
      {offered && autopilot && (
        <button type="button" role="menuitemcheckbox" aria-checked={pilotOn} className={`model-row mode-row autopilot-row ${pilotOn ? "on" : ""}`} onClick={togglePilot}>
          <span className="grow model-text">
            <span className="truncate">{t("composer.autopilot")}</span>
            <span className="sub clamp-2">{autopilotHint(autopilot, pilotOn)}</span>
          </span>
          <span className={`mode-switch ${pilotOn ? "on" : ""}`} aria-hidden="true"><i /></span>
        </button>
      )}
    </div>
  );
  return (
    <>
      <ControlTrigger ref={trigger} type="button" className={`composer-mode ${on ? "yagni" : ""} ${pilotOn ? "autopilot" : ""} ${open ? "on" : ""}`} onClick={() => setOpen(!open)} title={title} aria-label={`${t("composer.toggles.for")}: ${title}`} aria-haspopup="menu" aria-expanded={open}>
        {!on && !pilotOn && <span className="truncate">{t("composer.mode.agent")}</span>}
        {on && <span className="mode-yagni">{t("composer.yagni")}</span>}
        {pilotOn && <span className="mode-autopilot truncate">{t("composer.autopilot")}</span>}
      </ControlTrigger>
      {open && sheet && (
        <Sheet title={t("composer.toggles.for")} onClose={() => setOpen(false)} className="mode-sheet">
          {list}
        </Sheet>
      )}
      {open && !sheet && (
        <Popover anchor={trigger.current} onClose={() => setOpen(false)} className="mode-menu" label={t("composer.toggles.for")}>
          {list}
        </Popover>
      )}
    </>
  );
}
