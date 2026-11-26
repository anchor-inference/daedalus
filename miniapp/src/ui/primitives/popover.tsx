// A small box anchored to a control, a text range or a point: the composer's `+`, its model list,
// a chat's search. Below 1024 px, or inside a column narrower than the box, it is a bottom sheet.

import { Popover as Base } from "@base-ui/react/popover";
import { useRef, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { closeHandler, useFocusBack, useLayer } from "./layers";
import { type Anchor, SHEET_BELOW, anchorElement, useSheetForm } from "./responsive";
import { Sheet } from "./sheet";
import { SHIFT } from "./menu";

export type PopoverProps = {
  anchor: Anchor | null;
  /** Drawing the popover is opening it; `open={false}` keeps the element in the tree closed. */
  open?: boolean;
  onClose: () => void;
  label: string;
  /** Left out, the side with more room: the composer sits at the foot of the window, so its menus
      open upward, and a control near the top opens downward. */
  side?: "top" | "bottom" | "left" | "right";
  align?: "start" | "center" | "end";
  /** A fixed box (the model picker is one size whatever it lists). */
  width?: number;
  height?: number;
  /** How far the box stands from its anchor, in pixels. */
  offset?: number;
  /** `dialog` for a box that holds a field and groups of links, which a menu's role may not: a screen
      reader in a menu takes the letter keys for itself, and a field could not be typed into. */
  role?: "menu" | "dialog";
  /** The window width below which it is a sheet. */
  sheetBelow?: number;
  className?: string;
  children: ReactNode;
};

/** What takes the keyboard when the box opens: the search field of a list that has one, else the
    current choice, else the first control. */
const FIRST = ".model-search, [aria-checked='true'], input:checked, button[aria-current='true'], button:not(:disabled), input, textarea, [role='slider']";

export function Popover({ anchor, open = true, onClose, label, side, align = "start", width, height, offset, role = "menu", sheetBelow = SHEET_BELOW, className = "", children }: PopoverProps) {
  const sheet = useSheetForm(anchor, sheetBelow, width);
  if (!open) return null;
  if (sheet) {
    return (
      <Sheet title={label} onClose={onClose} size="sm" className={`pop-sheet ${className}`}>
        <div className={`menu pop in-sheet ${className}`} role={role} aria-label={label}>{children}</div>
      </Sheet>
    );
  }
  return <Floating anchor={anchor} onClose={onClose} label={label} side={side} align={align} width={width} height={height} offset={offset} role={role} className={className}>{children}</Floating>;
}

function Floating({ anchor, onClose, label, side, align, width, height, offset = 6, role = "menu", className, children }: Omit<PopoverProps, "open" | "sheetBelow">) {
  const isTop = useLayer(onClose);
  const popup = useRef<HTMLDivElement>(null);
  const element = anchorElement(anchor);
  // The side is chosen once, when the box opens: re-chosen on every layout it flipped under the
  // pointer as the list inside it filtered down.
  const placed = useRef(side ?? roomier(anchor));
  useFocusBack(() => (element instanceof HTMLElement ? element : null));
  const style: CSSProperties = { width, height };
  return (
    <Base.Root open modal={false} onOpenChange={closeHandler(isTop, onClose, (target) => element?.contains(target) ?? false)}>
      <Base.Portal>
        <Base.Positioner className="layer-pos popover-pos" anchor={anchor ?? undefined} side={placed.current} align={align} sideOffset={offset} collisionPadding={8} collisionAvoidance={SHIFT}>
          <Base.Popup
            ref={popup}
            className={`menu pop ${className}`}
            role={role}
            aria-label={label}
            style={style}
            initialFocus={() => popup.current?.querySelector<HTMLElement>(FIRST) ?? true}
            finalFocus={false}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => arrows(e, popup.current, onClose)}
          >
            {children}
          </Base.Popup>
        </Base.Positioner>
      </Base.Portal>
    </Base.Root>
  );
}

function roomier(anchor: Anchor | null): "top" | "bottom" {
  if (!anchor) return "bottom";
  const r = anchor.getBoundingClientRect();
  return r.top >= window.innerHeight - r.bottom ? "top" : "bottom";
}

/** The arrows walk the box's buttons, as they do in a menu; Tab leaves and closes it. A text field
    keeps its own arrows except Down, which steps from a search field into the list under it. */
function arrows(e: KeyboardEvent<HTMLElement>, box: HTMLElement | null, onClose: () => void) {
  if (e.defaultPrevented || !box) return;
  if (e.key === "Tab") { onClose(); return; }
  if (e.target instanceof HTMLInputElement && e.target.type !== "radio" && e.key !== "ArrowDown") return;
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
  const buttons = Array.from(box.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"));
  if (!buttons.length) return;
  const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
  const next = e.key === "Home" ? buttons[0] : e.key === "End" ? buttons[buttons.length - 1] : e.key === "ArrowDown" ? buttons[(i + 1) % buttons.length] : buttons[(i - 1 + buttons.length) % buttons.length];
  next.focus();
  e.preventDefault();
}
