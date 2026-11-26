// The language the agent works in for this session, a row of the composer's access menu. When it differs
// from the operator's own, the host translates each message on its way in and the agent's final answer
// on its way out, so the agent reads and writes one language and its prompt cache holds. The row says
// the pair it translates between ("EN ⇄ RU"), or only the operator's code while nothing is translated:
// a word that read "Off" told nothing about which language the agent is answering in. It used to be a
// chip of its own in the composer's row that read "EN" with no word, and on a phone a row of the "+"
// sheet; one row in the access menu is the same place at every size.

import { MenuRow, Popover } from "./ui/primitives";
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

/** The languages, beside the access menu's row that opened them (a sheet of its own on a phone). */
export function AgentLangMenu({ anchor, state, defaultAgent, onChoose, onClose }: {
  anchor: HTMLElement | null;
  state: SessionTranslation;
  /** The settings' default agent language, named on the "follow the default" row ("" = off); unknown
   *  while the session has its own choice and the host does not name the default. */
  defaultAgent?: string;
  onChoose: (choice: AgentLangChoice) => void;
  onClose: () => void;
}) {
  const row = (key: string, chosen: boolean, name: string, sub: string, agent: string | null) => (
    <MenuRow key={key} className="agentlang-row" role="menuitemradio" checked={chosen} label={name} sub={sub || undefined} data={{ lang: key }} onSelect={() => onChoose({ agent })} />
  );
  return (
    <Popover anchor={anchor} onClose={onClose} className="agentlang-menu" side="right" align="end" label={t("agentlang.for")}>
      {/* Said once, above the list: under every language it made a menu of thirteen repeated lines. */}
      <div className="menu-group">{t("agentlang.heading", { operator: languageName(state.operator) })}</div>
      {row("default", !state.explicit, defaultAgent === undefined ? t("agentlang.default.plain") : t("agentlang.default", { lang: languageName(defaultAgent) }), t("agentlang.default.sub"), null)}
      {row("off", state.explicit && !state.agent, t("agentlang.off"), t("agentlang.off.sub"), "")}
      {AGENT_LANGUAGES.filter((code) => code !== state.operator).map((code) =>
        row(code, state.explicit && state.agent === code, languageName(code), "", code))}
    </Popover>
  );
}
