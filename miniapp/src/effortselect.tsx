// Named choices keep a discrete setting understandable without dragging an unlabeled track.
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { useLayer } from "./ui/dialogs";
import { Icon } from "./icons";
import { t } from "./i18n";
import { REASONING_EFFORTS, effortIndex } from "./models";

export type EffortOptionsProps = {
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
export function EffortOptions({ effort, thinking, onChoose }: EffortOptionsProps) {
  const name = useId();
  const current = REASONING_EFFORTS[effortIndex(effort)];
  return <fieldset className="effort-options">
    <legend className="menu-heading sub">{t("composer.effort.heading")}</legend>
    {["off", ...REASONING_EFFORTS].map((value) => {
      const checked = value === "off" ? !thinking : !!thinking && value === current;
      return <label key={value} className={`model-row mode-row effort-option ${checked ? "on" : ""}`}>
        <input type="radio" name={name} value={value} checked={checked} onChange={() => onChoose(value)} onClick={() => { if (checked) onChoose(value); }} />
        <span className="grow model-text">
          <span className="truncate">{t(`add.effort.${value}`)}</span>
          <span className="sub clamp-2">{t(`composer.effort.${value}.hint`)}</span>
        </span>
        {checked && <Icon name="check" size={16} />}
      </label>;
    })}
  </fieldset>;
}

/** Effort belongs to the model picker; its child panel stays in the same owned menu layer. */
export function EffortMenu({ effort, thinking, onChoose, inline }: EffortOptionsProps & { inline: boolean }) {
  const owner = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const current = REASONING_EFFORTS[effortIndex(effort)];
  const close = () => { setOpen(false); trigger.current?.focus({ preventScroll:true }); };
  useEffect(() => {
    if (!open) return;
    const outside = (e: PointerEvent) => { if (!owner.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [open]);
  return <div ref={owner} className="effort-branch">
    <button ref={trigger} type="button" role="menuitem" className={`model-row effort-entry ${open ? "on" : ""}`} aria-haspopup="dialog" aria-expanded={open}
      onClick={() => setOpen(!open)} onKeyDown={(e) => { if (e.key === "ArrowRight") { e.preventDefault(); setOpen(true); } }}>
      <span className="grow">{t("composer.effort.short")}</span>
      <span className="sub">{t(thinking ? `add.effort.${current}` : "add.effort.off")}</span><Icon name="chevron" size={14} />
    </button>
    {open && <EffortPanel anchor={trigger.current} inline={inline} onClose={close} effort={effort} thinking={thinking} onChoose={onChoose} />}
  </div>;
}

function EffortPanel({ anchor, inline, onClose, ...props }: EffortOptionsProps & { anchor: HTMLElement | null; inline: boolean; onClose: () => void }) {
  const panel = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null);
  useLayer(onClose);
  useLayoutEffect(() => {
    const reveal = () => {
      const container = anchor?.closest(".sheet-body, .model-menu");
      if (!container || !panel.current) return;
      const bounds = container.getBoundingClientRect();
      const child = panel.current.getBoundingClientRect();
      if (child.bottom > bounds.bottom) container.scrollTop += child.bottom - bounds.bottom;
      else if (child.top < bounds.top) container.scrollTop -= bounds.top - child.top;
    };
    const place = () => {
      if (!anchor || !panel.current) return;
      const menu = anchor.closest(".model-menu")?.getBoundingClientRect();
      if (inline || !menu) { setPosition(null); requestAnimationFrame(reveal); return; }
      const width = 300;
      const right = menu.right + 6;
      const left = menu.left - width - 6;
      if (right + width > innerWidth - 8 && left < 8) { setPosition(null); requestAnimationFrame(reveal); return; }
      const height = Math.min(panel.current.getBoundingClientRect().height, innerHeight - 16);
      setPosition({ left:right + width <= innerWidth - 8 ? right : left, top:Math.max(8, Math.min(anchor.getBoundingClientRect().top, innerHeight - height - 8)) });
    };
    place();
    const observer = new ResizeObserver(() => { place(); requestAnimationFrame(place); });
    if (panel.current) observer.observe(panel.current);
    const menu = anchor?.closest(".model-menu");
    if (menu) observer.observe(menu);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => { observer.disconnect(); window.removeEventListener("resize", place); window.removeEventListener("scroll", place, true); };
  }, [anchor, inline]);
  useEffect(() => { panel.current?.querySelector<HTMLInputElement>("input:checked")?.focus({ preventScroll:true }); }, []);
  return <div ref={panel} role="dialog" aria-label={t("composer.effort")} className={`effort-menu effort-submenu ${position ? "floating" : "inline"}`}
    data-side={position && anchor && position.left < anchor.getBoundingClientRect().left ? "left" : "right"}
    style={position ? { position:"fixed", ...position } : undefined}
    onKeyDown={(e) => {
      if (e.key === "ArrowLeft") { e.preventDefault(); e.stopPropagation(); onClose(); return; }
      if (!(e.target instanceof HTMLInputElement)) return;
      if (["ArrowUp", "ArrowDown", "ArrowRight", "Home", "End", "Enter"].includes(e.key)) e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); props.onChoose(e.target.value); }
      if (e.key === "Home" || e.key === "End") {
        e.preventDefault();
        const inputs = panel.current?.querySelectorAll<HTMLInputElement>("input[type='radio']");
        (e.key === "Home" ? inputs?.[0] : inputs?.[inputs.length - 1])?.click();
      }
    }}>
    <EffortOptions {...props} />
  </div>;
}
