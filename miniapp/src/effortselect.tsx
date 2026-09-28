// Named choices keep a discrete setting understandable without dragging an unlabeled track.
import { useId, useRef, useState } from "react";
import { Popover } from "./dialogs";
import { Icon } from "./icons";
import { t } from "./i18n";
import { REASONING_EFFORTS, effortIndex } from "./models";

export type EffortSelectProps = {
  effort?: string;
  thinking?: boolean;
  onChoose: (effort: string) => void;
};

/**
 * The efforts as rows drawn like the mode's and the model's: a small heading, the name over its hint,
 * and a check on the chosen one. The three composer menus had three headings and two marks (a radio
 * here, a check there), so each had to be learned apart. The radio input stays for its keyboard and
 * its semantics, laid invisibly over the whole row, which is also the row's tap target.
 */
export function EffortOptions({ effort, thinking, onChoose }: EffortSelectProps) {
  const name = useId();
  const current = REASONING_EFFORTS[effortIndex(effort)];
  return <fieldset className="effort-options">
    <legend className="menu-heading sub">{t("composer.effort.heading")}</legend>
    {["off", ...REASONING_EFFORTS].map((value) => {
      const checked = value === "off" ? !thinking : !!thinking && value === current;
      return <label key={value} className={`model-row mode-row effort-option ${checked ? "on" : ""}`}>
        <input type="radio" name={name} value={value} checked={checked} onChange={() => onChoose(value)} />
        <span className="grow model-text">
          <span className="truncate">{t(`add.effort.${value}`)}</span>
          <span className="sub clamp-2">{t(`composer.effort.${value}.hint`)}</span>
        </span>
        {checked && <Icon name="check" size={16} />}
      </label>;
    })}
  </fieldset>;
}

export function EffortSelect({ effort, thinking, onChoose }: EffortSelectProps) {
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const current = REASONING_EFFORTS[effortIndex(effort)];
  return <>
    <button ref={trigger} type="button" className={`effort-select ${open ? "on" : ""}`} onClick={() => setOpen(!open)}
      title={t("composer.effort")} aria-label={t("composer.effort")} aria-haspopup="dialog" aria-expanded={open}>
      <span className="truncate">{thinking ? t(`add.effort.${current}`) : t("add.effort.off")}</span><Icon name="chevron" size={12} />
    </button>
    {open && <Popover anchor={trigger.current} onClose={() => setOpen(false)} className="effort-menu" align="right" label={t("composer.effort")}>
      <EffortOptions effort={effort} thinking={thinking} onChoose={(value) => { onChoose(value); setOpen(false); }} />
    </Popover>}
  </>;
}
