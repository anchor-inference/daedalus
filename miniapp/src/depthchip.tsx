// The level the auto mode ran an operator's message at, on the message itself, and the correction.
// A correction re-runs nothing: what was answered stays answered, and the level chosen here is
// spent by the next message, so a wrong guess costs one click rather than a mode left switched on.

import { useRef, useState } from "react";
import { api, type TurnDepth } from "./api";
import { errorText } from "./ui";
import { Popover } from "./ui/dialogs";
import { Icon } from "./icons";
import { t } from "./i18n";

export const DEPTH_LEVELS = ["quick", "normal", "long"] as const;

export function DepthChip({ depth, sessionId, toast }: { depth: TurnDepth; sessionId: string; toast: (text: string) => void }) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const how = t(`turn.depth.source.${depth.source}`);
  const choose = async (level: string) => {
    setOpen(false);
    try {
      await api.post(`/api/sessions/${sessionId}/depth`, { level });
      toast(t("turn.depth.next", { level:t(`turn.depth.${level}`) }));
    } catch (e) {
      toast(errorText(e));
    }
  };
  return <>
    <button ref={trigger} type="button" className={`msg-origin msg-depth ${depth.level}`} data-depth={depth.level}
      title={t("turn.depth.title", { level:t(`turn.depth.${depth.level}`), how })} aria-haspopup="menu" aria-expanded={open}
      onClick={() => setOpen(!open)}>
      {t(`turn.depth.${depth.level}`)}
    </button>
    {open && <Popover anchor={trigger.current} onClose={() => setOpen(false)} className="depth-menu" label={t("turn.depth.menu")}>
      <div className="menu-heading sub">{t("turn.depth.menu")}</div>
      {DEPTH_LEVELS.map((level) => (
        <button key={level} type="button" role="menuitem" className={`model-row mode-row ${level === depth.level ? "on" : ""}`} data-level={level} onClick={() => choose(level)}>
          <span className="grow model-text">
            <span className="truncate">{t(`turn.depth.${level}`)}</span>
            <span className="sub clamp-2">{t(`turn.depth.${level}.hint`)}</span>
          </span>
          {level === depth.level && <Icon name="check" size={16} />}
        </button>
      ))}
    </Popover>}
  </>;
}
