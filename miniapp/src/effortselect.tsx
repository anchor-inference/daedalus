// The reasoning effort in one word, as the composer's model chip and the chat's header say it. The
// choice itself is a row of the model picker (modelselect.tsx), not a menu of its own: as a child
// panel beside the picker it was pushed against the foot of the window, away from its row.
import { t } from "./i18n";
import { REASONING_EFFORTS, effortIndex } from "./models";

/** The effort as the composer names it: the level, or off when the model is not thinking. */
export function effortWord(effort: string | undefined, thinking: boolean | undefined): string {
  if (!thinking) return t("add.effort.off");
  return t(`add.effort.${REASONING_EFFORTS[effortIndex(effort)]}`);
}
