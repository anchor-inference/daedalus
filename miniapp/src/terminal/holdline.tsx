// The words at the left of a pane's foot, and the one control that goes with them: who is typing into
// this terminal, and how the operator takes the keyboard or gives it back. Only the active pane says
// it; the others leave the foot to their status line, so a grid of four is not four sentences.

import { api, type TerminalView } from "../api";
import { t } from "../i18n";
import { errorText } from "../ui";
import { keyboardOf, holdOf } from "./hold";
import type { TerminalState } from "./instance";

export function PaneHold({ row, state, onRestart, toast }: { row: TerminalView | null; state: TerminalState | undefined; onRestart: () => void; toast: (text: string) => void }) {
  if (!row) return null;
  const hold = holdOf(row, !!state?.exit);
  const taken = keyboardOf(row, state?.keyboard.owner) === "human";
  const hand = async (owner: "auto" | "human") => {
    try {
      await api.terminalKeyboard(row.id, owner);
    } catch (error) {
      toast(errorText(error));
    }
  };
  const words = hold === "agent" ? t(taken ? "term.hold.taken" : "term.hold.agent") : t(`term.hold.${hold}`);
  return (
    <span className="tp-hold" data-hold={hold} data-taken={taken ? "1" : undefined}>
      <span className="tp-hold-words">{words}</span>
      {hold === "agent" && (
        <>
          <span className="tp-hold-sep" aria-hidden="true">·</span>
          <button type="button" className="tp-hold-take" data-action={taken ? "give-back" : "take"} onClick={() => void hand(taken ? "auto" : "human")}>{t(taken ? "term.hold.back" : "term.hold.take")}</button>
        </>
      )}
      {hold === "ended" && (
        <>
          <span className="tp-hold-sep" aria-hidden="true">·</span>
          <button type="button" className="tp-hold-take" data-action="restart" onClick={onRestart}>{t("term.restart")}</button>
        </>
      )}
    </span>
  );
}
