// How the agent works in this session, behind one chip at the left of the composer's row: the access
// it has (full access is "Bypass all checks"), YAGNI, autopilot, and the language it works in. The chip
// says the access, in the attention colour while everything is let through, and after it whatever
// else is on, so a session that plans small or runs by itself is told apart at a glance.
//
// These were three things: an "Agent" chip that named the one agent configuration there is and opened
// two or three switches, a language chip that read "EN" with no word, and a header mark for bypass.
// Bypass is one click with no confirmation: it is the operator's choice, and the line under it says
// what still holds; the session's header keeps its mark while it is on.

import { useEffect, useRef, useState } from "react";
import { MenuRow, Popover } from "./ui/primitives";
import { Icon } from "./icons";
import { t } from "./i18n";
import type { AutopilotStatus, SessionTranslation } from "./api";
import { AgentLangMenu, agentLangLabel, type AgentLangChoice } from "./agentlang";

export type AccessSelectProps = {
  yagni?: boolean;
  /** The session's YAGNI switch; absent where the session offers no switches (a bare or visited chat). */
  onYagni?: (on: boolean) => void;
  /** The session's autopilot switch; absent where the session cannot have one (a role of its own). */
  autopilot?: AutopilotStatus | null;
  onAutopilot?: (on: boolean) => void;
  /** The session's "Bypass all checks"; absent where the session offers no such switch. */
  bypass?: boolean;
  onBypass?: (on: boolean) => void;
  /** The language the agent works in, with the settings' default; the row is there only with `onAgentLang`. */
  translation?: SessionTranslation;
  translationDefault?: string;
  onAgentLang?: (choice: AgentLangChoice) => void;
  /** A phone's row has room for the shield alone; the words are in the menu and the label. */
  compact?: boolean;
};

/** What the autopilot row says under its name: off for the whole app, the turns it took, or what it does. */
export function autopilotHint(status: AutopilotStatus, on: boolean): string {
  if (!status.enabled) return t("composer.autopilot.disabled");
  if (on) return t("composer.autopilot.used", { used: String(status.used), allowed: String(status.allowed) });
  return t("composer.autopilot.hint");
}

/** The chip's words: the access, then each other switch that is on. */
export function accessTitle(yagni: boolean, autopilot: boolean, bypass = false): string {
  return [t(bypass ? "composer.bypass" : "composer.access.checks"), yagni && t("composer.yagni"), autopilot && t("composer.autopilot")].filter(Boolean).join(" · ");
}

export function AccessSelect({ yagni = false, onYagni, autopilot, onAutopilot, bypass, onBypass, translation, translationDefault, onAgentLang, compact }: AccessSelectProps) {
  const trigger = useRef<HTMLButtonElement>(null);
  const langRow = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [langOpen, setLangOpen] = useState(false);
  // A switch answers at once; the host's word arrives with the next poll and replaces it.
  const [shown, setShown] = useState<boolean | null>(null);
  useEffect(() => setShown(null), [yagni]);
  const on = !!onYagni && (shown ?? yagni);
  const [pilotShown, setPilotShown] = useState<boolean | null>(null);
  useEffect(() => setPilotShown(null), [autopilot?.on]);
  const offered = !!(autopilot?.eligible && onAutopilot);
  const pilotOn = offered && (pilotShown ?? !!autopilot?.on);
  const [bypassShown, setBypassShown] = useState<boolean | null>(null);
  useEffect(() => setBypassShown(null), [bypass]);
  const bypassOn = !!onBypass && (bypassShown ?? !!bypass);
  if (!onYagni && !onBypass && !onAgentLang) return null;
  const title = accessTitle(on, pilotOn, bypassOn);
  const translating = !!translation && !!translation.agent && translation.agent !== translation.operator;
  return (
    <>
      <button ref={trigger} type="button" className={`composer-access ${bypassOn ? "bypass" : ""} ${open ? "on" : ""} ${compact ? "compact" : ""}`} onClick={() => setOpen(!open)}
        title={title} aria-label={`${t("composer.toggles.for")}: ${title}`} aria-haspopup="menu" aria-expanded={open} data-yagni={on ? "" : undefined} data-autopilot={pilotOn ? "" : undefined}>
        <Icon name="shield" size={14} />
        {!compact && <span className="access-word truncate">{t(bypassOn ? "composer.bypass" : "composer.access.checks")}</span>}
        {!compact && on && <span className="access-more">{t("composer.yagni")}</span>}
        {!compact && pilotOn && <span className="access-more truncate">{t("composer.autopilot")}</span>}
        {!compact && translating && <span className="access-more access-lang">{agentLangLabel(translation!)}</span>}
      </button>
      {open && (
        <Popover anchor={trigger.current} onClose={() => { setLangOpen(false); setOpen(false); }} className="access-menu" label={t("composer.toggles.for")}>
          {onBypass && <MenuRow className="bypass-row" label={t("composer.bypass")} sub={t("composer.bypass.hint")} data={{ toggle: "bypass" }}
            toggle={{ checked: bypassOn, onChange: (next) => { setBypassShown(next); onBypass(next); } }} />}
          {onYagni && <MenuRow className="yagni-row" label={t("composer.yagni")} sub={t("composer.yagni.hint")} data={{ toggle: "yagni" }}
            toggle={{ checked: on, onChange: (next) => { setShown(next); onYagni(next); } }} />}
          {offered && autopilot && <MenuRow className="autopilot-row" label={t("composer.autopilot")} sub={autopilotHint(autopilot, pilotOn)} data={{ toggle: "autopilot" }}
            toggle={{ checked: pilotOn, onChange: (next) => { setPilotShown(next); onAutopilot?.(next); } }} />}
          {onAgentLang && translation && (
            <>
              {(onBypass || onYagni) && <div className="menu-sep" role="separator" />}
              <div ref={langRow} className="access-lang-row">
                <MenuRow role="menuitem" label={t("agentlang.for.short")} aside={agentLangLabel(translation)} chevron data={{ action: "language" }} onSelect={() => setLangOpen((o) => !o)} />
              </div>
            </>
          )}
          {langOpen && onAgentLang && translation && (
            <AgentLangMenu anchor={langRow.current} state={translation} defaultAgent={translationDefault}
              onChoose={(choice) => { onAgentLang(choice); setLangOpen(false); setOpen(false); }} onClose={() => setLangOpen(false)} />
          )}
        </Popover>
      )}
    </>
  );
}
