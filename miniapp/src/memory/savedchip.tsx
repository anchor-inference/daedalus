// "Saved to memory" under a turn whose Remember calls kept something, as claude.ai's chip: the agent
// is told not to announce its saves, so this is where the operator sees them. A tap opens the entry
// the first note went to.

import type { Activity, ToolItem } from "../turns";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { pathFor } from "../router";
import { go } from "../ui/index";

const SAVED = /^(created|merged) memory (\S+)/;

/** The ids of the records a turn's settled Remember calls created or folded into; a refusal, an error
 *  or a note memory already held (``skipped``) saves nothing and is not counted. */
export function savedMemories(activity: Activity[]): string[] {
  const ids: string[] = [];
  for (const item of activity) {
    if (item.kind !== "tool" || item.name !== "Remember" || item.running || item.error || !item.result) continue;
    const found = SAVED.exec((item as ToolItem).result!.trim());
    if (found && !ids.includes(found[2])) ids.push(found[2]);
  }
  return ids;
}

export function SavedToMemory({ activity }: { activity: Activity[] }) {
  const ids = savedMemories(activity);
  if (!ids.length) return null;
  const href = pathFor("memory", null, { record: ids[0] });
  return (
    <a className="memory-saved-chip" href={href} onClick={(e) => go(e, href)} data-saved={ids.length}>
      <Icon name="bulb" size={12} /> {ids.length > 1 ? plural("memory.saved.many", ids.length) : t("memory.saved.one")}
    </a>
  );
}
