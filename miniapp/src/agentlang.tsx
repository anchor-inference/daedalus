import { ControlTrigger } from "./ui/control-trigger";
// The language the agent works in for this session, behind a chip beside the toggles. When it differs
// from the operator's own, the host translates each message on its way in and the agent's final answer
// on its way out, so the agent reads and writes one language and its prompt cache holds. The chip says
// the pair it translates between ("EN ⇄ RU"), or only the operator's code, muted, while nothing is
// translated: a chip that read "Off" told nothing about which language the agent is answering in.
// A phone's toolbar has no room for the chip beside the toggles, the model with its effort, the context
// ring, the microphone and the circle: there the choice is a row of the composer's "+" sheet, which
// opens the same sheet of languages (`chip={false}` with the open state held by the composer).

import { useEffect, useRef, useState } from "react";
import { Popover, Sheet } from "./ui/dialogs";
import { Icon } from "./icons";
import { t } from "./i18n";
import type { SessionTranslation } from "./api";

/** The codes the host accepts, in the order the menu lists them. */
export const AGENT_LANGUAGES = ["en", "ru", "uk", "de", "fr", "es", "it", "pt", "pl", "tr", "zh", "ja", "ko"] as const;

export function languageName(code: string): string {
  return code ? t(`lang.of.${code}`) : t("agentlang.off");
}

/** What the chip says: the pair while translating, the operator's code alone otherwise. */
export function agentLangLabel(state: SessionTranslation): string {
  const operator = (state.operator || "").toUpperCase();
  if (state.agent && state.agent !== state.operator) return `${state.agent.toUpperCase()} ⇄ ${operator}`;
  return operator || "—";
}

export type AgentLangChoice = { agent: string | null };

export function AgentLangSelect({ state, defaultAgent, onChoose, sheet, chip = true, open: held, onOpenChange }: {
  state: SessionTranslation;
  /** The settings' default agent language, named on the "follow the default" row ("" = off); unknown
   *  while the session has its own choice and the host does not name the default. */
  defaultAgent?: string;
  onChoose: (choice: AgentLangChoice) => void;
  /** Phones: a sheet instead of a popover. */
  sheet: boolean;
  /** Whether the chip itself is drawn; without it the list opens from `open`, set by its holder. */
  chip?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [own, setOwn] = useState(false);
  const open = held ?? own;
  const setOpen = (next: boolean) => (onOpenChange ?? setOwn)(next);
  // The choice answers at once; the host's word arrives with the next read and replaces it.
  const [shown, setShown] = useState<SessionTranslation | null>(null);
  useEffect(() => setShown(null), [state.agent, state.explicit, state.operator]);
  const current = shown ?? state;
  const active = !!current.agent && current.agent !== current.operator;
  const label = agentLangLabel(current);
  const title = active
    ? t("agentlang.title.on", { agent: languageName(current.agent), operator: languageName(current.operator) })
    : t("agentlang.title.off");
  const choose = (agent: string | null) => {
    // Following an unknown default shows the old pair until the host's answer names the new one.
    const next = agent === null ? defaultAgent ?? current.agent : agent;
    setShown({ ...current, agent: next, explicit: agent !== null, active: !!next && next !== current.operator });
    onChoose({ agent });
    setOpen(false);
  };
  const row = (key: string, chosen: boolean, name: string, sub: string, agent: string | null) => (
    <button key={key} type="button" role="menuitemradio" aria-checked={chosen} data-lang={key} className={`model-row mode-row agentlang-row ${chosen ? "on" : ""}`} onClick={() => choose(agent)}>
      <span className="grow model-text">
        <span className="truncate">{name}</span>
        {sub && <span className="sub clamp-2">{sub}</span>}
      </span>
      {chosen && <Icon name="check" size={16} />}
    </button>
  );
  const list = (
    <div className="mode-list">
      {/* Said once, above the list: under every language it made a menu of thirteen repeated lines. */}
      <div className="menu-heading sub">{t("agentlang.heading", { operator: languageName(current.operator) })}</div>
      {row("default", !current.explicit, defaultAgent === undefined ? t("agentlang.default.plain") : t("agentlang.default", { lang: languageName(defaultAgent) }), t("agentlang.default.sub"), null)}
      {row("off", current.explicit && !current.agent, t("agentlang.off"), t("agentlang.off.sub"), "")}
      {AGENT_LANGUAGES.filter((code) => code !== current.operator).map((code) =>
        row(code, current.explicit && current.agent === code, languageName(code), "", code))}
    </div>
  );
  return (
    <>
      {chip && <ControlTrigger ref={trigger} type="button" className={`agentlang-chip ${active ? "translating" : "off"} ${open ? "on" : ""}`} onClick={() => setOpen(!open)} title={title} aria-label={`${t("agentlang.for")}: ${title}`} aria-haspopup="menu" aria-expanded={open}>
        {/* The pair in two parts, so a narrow composer can keep the agent's code alone (styles.css). */}
        <span className="truncate">{active ? <><span className="agentlang-agent">{current.agent.toUpperCase()}</span><span className="agentlang-to">{label.slice(current.agent.length)}</span></> : label}</span>
      </ControlTrigger>}
      {open && sheet && (
        <Sheet title={t("agentlang.for")} onClose={() => setOpen(false)} className="mode-sheet">
          {list}
        </Sheet>
      )}
      {open && !sheet && (
        <Popover anchor={trigger.current} onClose={() => setOpen(false)} className="mode-menu" label={t("agentlang.for")}>
          {list}
        </Popover>
      )}
    </>
  );
}
