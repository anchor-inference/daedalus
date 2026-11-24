// Menus: a list of commands hung from a control (the overflow "…"), from a point (a right click), or
// drawn as a sheet of rows on a phone. One row design serves all three, MenuRow.

import { Menu as Base } from "@base-ui/react/menu";
import { cloneElement, useCallback, useEffect, useId, useRef, useState, type ReactElement, type ReactNode, type RefObject } from "react";
import { Icon, type IconName } from "../../icons";
import { t } from "../../i18n";
import { closeHandler, useFocusBack, useLayer } from "./layers";
import { type Anchor, SHEET_BELOW, anchorElement, useSheetForm } from "./responsive";
import { Sheet, SheetPreview } from "./sheet";
import { Switch } from "./switch";
import { useContextActions } from "./contextmenu";

/** Kept inside the window by sliding along the edge, 8 px short of it, rather than by turning over
    to the other alignment: a right click in a corner turned the menu round its point and left it 2 px
    from the edge, its last letters under the window frame. */
export const SHIFT = { side: "flip", align: "shift" } as const;

/** A command. `warn` marks one that reaches outside the sandbox (a terminal on the machine itself):
    the attention colour, not the failure one. `checked` makes it a checkbox, which keeps the menu
    open: it sets how the commands below it act. `ticks` marks one that resolves true once its work
    went through, a copy above all: the menu closes on the press, so the tick shows on the button
    that opened it. `aside` sits at the row's far end (a folder's environment and read-only badges in
    the file pane's folder list); `kbd` is the key that does the same from anywhere. */
export type MenuAction = { label: string; icon?: IconName; danger?: boolean; warn?: boolean; disabled?: boolean; hint?: string; checked?: boolean; ticks?: boolean; aside?: ReactNode; kbd?: string; onSelect: () => unknown };
/** A command that opens more commands beside it; on a phone its commands are listed under its name. */
export type MenuSubmenu = { label: string; icon?: IconName; disabled?: boolean; hint?: string; submenu: MenuItem[] };
/** A label over the commands that follow it, up to the next label or divider. */
export type MenuGroup = { group: string };
export type MenuItem = MenuAction | MenuSubmenu | MenuGroup | "-";

export function isAction(item: MenuItem): item is MenuAction {
  return item !== "-" && "onSelect" in item;
}

// ── the row ──────────────────────────────────────────────────────────────────────────────────

export type MenuRowProps = {
  icon?: IconName;
  /** A page's own glyph, drawn instead of the icon's (a custom page in the More sheet). */
  iconPath?: string | null;
  label: ReactNode;
  sub?: ReactNode;
  kbd?: string;
  /** A choice among several: the row carries a tick while it is the chosen one. */
  checked?: boolean;
  /** An on/off setting in the row, a switch at its end. */
  toggle?: { checked: boolean; onChange: (next: boolean) => void };
  chevron?: boolean;
  danger?: boolean;
  warn?: boolean;
  disabled?: boolean;
  aside?: ReactNode;
  onSelect?: () => void;
  /** More at the row's end, after the aside (a badge, a count). */
  children?: ReactNode;
  className?: string;
  data?: Record<string, string>;
  role?: string;
};

/** The inside of a row: the same spans whether a Base UI item, a sheet's button or a plain div
    holds them, so a menu, a phone sheet and a settings list read as one list. */
export function menuRowBody({ icon, iconPath, label, sub, kbd, checked, toggle, chevron, aside, children, subId }: MenuRowProps & { subId?: string }): ReactNode {
  return (
    <>
      {(icon || iconPath) && <Icon name={icon ?? "file"} size={16} path={iconPath} />}
      <span className="menu-row-main">
        <span className="menu-row-t">{label}</span>
        {sub && <span className="menu-row-sub" id={subId}>{sub}</span>}
      </span>
      {aside !== undefined && aside !== null && <span className="menu-row-aside">{aside}</span>}
      {children}
      {kbd && <kbd className="kbd menu-row-kbd">{kbd}</kbd>}
      {toggle && <Switch checked={toggle.checked} onChange={toggle.onChange} label={typeof label === "string" ? label : ""} />}
      {checked !== undefined && <span className={`menu-row-check ${checked ? "on" : ""}`} aria-hidden="true">{checked && <Icon name="check" size={16} />}</span>}
      {chevron && <span className="menu-row-chev" aria-hidden="true"><Icon name="forward" size={14} /></span>}
    </>
  );
}

function rowClass({ danger, warn, checked, className }: MenuRowProps): string {
  return ["menu-row", danger ? "danger" : warn ? "warn" : "", checked ? "on" : "", className ?? ""].filter(Boolean).join(" ");
}

function dataAttrs(data?: Record<string, string>) {
  return Object.fromEntries(Object.entries(data ?? {}).map(([k, v]) => [`data-${k}`, v]));
}

/** One row of a menu or a sheet: 32 px on a desktop, 44 on a phone, a 16 px icon and an 8 px gap. */
export function MenuRow(props: MenuRowProps) {
  const { onSelect, disabled, toggle, checked, data, role, label, sub } = props;
  const subId = useId();
  // The second line describes the row rather than naming it: a command is found by its own words,
  // and "Pause" stayed "Pause" when a phone's sheet showed its hint under it.
  const named = typeof label === "string" && sub ? { "aria-label": label, "aria-describedby": subId } : {};
  if (!onSelect && !toggle) return <div className={rowClass(props)} {...dataAttrs(data)}>{menuRowBody(props)}</div>;
  const press = onSelect ?? (() => toggle?.onChange(!toggle.checked));
  const semantic = role ?? (toggle ? "menuitemcheckbox" : checked === undefined ? undefined : "menuitemradio");
  return (
    <button type="button" {...dataAttrs(data)} className={rowClass(props)} disabled={disabled} onClick={press}
      role={semantic} aria-checked={toggle ? toggle.checked : checked} {...named}>
      {menuRowBody({ ...props, toggle: undefined, subId })}
      {toggle && <span className={`switch ${toggle.checked ? "on" : ""}`} aria-hidden="true"><span className="switch-track"><span className="switch-knob" /></span></span>}
    </button>
  );
}

// ── the menu ─────────────────────────────────────────────────────────────────────────────────

export type MenuProps = {
  /** A control that opens the menu; the menu then keeps its own open state. */
  trigger?: ReactElement<{ onClick?: (e: React.MouseEvent) => void }>;
  /** Or an anchor, with the open state held by the caller. */
  anchor?: Anchor | null;
  open?: boolean;
  onClose?: () => void;
  items: MenuItem[];
  label: string;
  side?: "top" | "bottom";
  align?: "start" | "center" | "end";
  width?: number;
  /** On a phone: what the commands are about, above them. */
  preview?: { title: ReactNode; meta?: ReactNode };
  title?: ReactNode;
  sheetBelow?: number;
  className?: string;
  /** The tick on the trigger after a `ticks` command went through. */
  onTicked?: () => void;
};

export function Menu({ trigger, anchor, open: controlled, onClose, items, label, side, align = "end", width, preview, title, sheetBelow = SHEET_BELOW, className = "", onTicked }: MenuProps) {
  const [own, setOwn] = useState(false);
  const button = useRef<HTMLElement>(null);
  const open = trigger ? own : controlled ?? true;
  const close = useCallback(() => (trigger ? setOwn(false) : onClose?.()), [trigger, onClose]);
  const at = trigger ? button.current : anchor ?? null;
  const sheet = useSheetForm(at, sheetBelow, width) || !at;
  const opener = trigger && cloneElement(trigger as ReactElement<Record<string, unknown>>, {
    ref: button,
    "aria-haspopup": "menu",
    "aria-expanded": own,
    onClick: (e: React.MouseEvent) => { e.stopPropagation(); trigger.props.onClick?.(e); setOwn((o) => !o); },
  });
  const body = !open ? null : sheet
    ? <MenuSheet items={items} label={label} preview={preview} title={title} onClose={close} onTicked={onTicked} className={className} />
    : <MenuFloating anchor={at} items={items} label={label} side={side} align={align} width={width} onClose={close} onTicked={onTicked} className={className} />;
  return <>{opener}{body}</>;
}

/** The commands as rows of a bottom sheet: the destructive ones moved last, after a divider,
    whatever order the menu that also lists them uses; a submenu's commands under its name. */
function MenuSheet({ items, label, preview, title, onClose, onTicked, className }: { items: MenuItem[]; label: string; preview?: MenuProps["preview"]; title?: ReactNode; onClose: () => void; onTicked?: () => void; className: string }) {
  const flat: MenuItem[] = items.flatMap((item) => (item !== "-" && "submenu" in item ? [{ group: item.label } as MenuGroup, ...item.submenu] : [item]));
  const safe = flat.filter((item) => !(isAction(item) && item.danger));
  const danger = flat.filter((item): item is MenuAction => isAction(item) && !!item.danger);
  const pick = (item: MenuAction) => {
    if (item.checked === undefined) onClose();
    const done = item.onSelect();
    if (item.ticks) void Promise.resolve(done).then((ok) => { if (ok === true) onTicked?.(); });
  };
  const row = (item: MenuItem, key: string) => {
    if (item === "-") return <div key={key} className="menu-sep" role="separator" />;
    if ("group" in item) return <div key={key} className="menu-group" role="presentation">{item.group}</div>;
    if (!isAction(item)) return null;
    return <MenuRow key={key} icon={item.icon} label={item.label} sub={item.hint} aside={item.aside} kbd={item.kbd} danger={item.danger} warn={item.warn} disabled={item.disabled}
      role={item.checked === undefined ? "menuitem" : "menuitemcheckbox"} checked={item.checked} onSelect={() => pick(item)} />;
  };
  const shown = trimSeparators(safe);
  return (
    <Sheet title={title} ariaLabel={typeof preview?.title === "string" ? preview.title : label} onClose={onClose} className={`menu-sheet ${className}`}>
      {preview && <SheetPreview title={preview.title} meta={preview.meta} />}
      <div className="menu in-sheet" role="menu" aria-label={label}>
        {shown.map((item, i) => row(item, `s${i}`))}
        {danger.length > 0 && shown.length > 0 && <div className="menu-sep" role="separator" />}
        {danger.map((item, i) => row(item, `d${i}`))}
      </div>
    </Sheet>
  );
}

function trimSeparators(items: MenuItem[]): MenuItem[] {
  const out = items.filter((item, i) => item !== "-" || (i > 0 && items[i - 1] !== "-"));
  while (out[0] === "-") out.shift();
  while (out[out.length - 1] === "-") out.pop();
  return out;
}

function MenuFloating({ anchor, items, label, side, align, width, onClose, onTicked, className }: { anchor: Anchor | null; items: MenuItem[]; label: string; side?: "top" | "bottom"; align: "start" | "center" | "end"; width?: number; onClose: () => void; onTicked?: () => void; className: string }) {
  const isTop = useLayer(onClose);
  const popup = useRef<HTMLDivElement>(null);
  const element = anchorElement(anchor);
  // A control in the lower half opens its menu upward: a menu that runs off the bottom edge has to
  // be scrolled to. Chosen once, on open.
  const placed = useRef(side ?? lowerHalf(anchor));
  useFocusBack(() => (element instanceof HTMLElement ? element : null));
  useFirstItem(popup);
  return (
    <Base.Root open modal={false} onOpenChange={closeHandler(isTop, onClose, (target) => element?.contains(target) ?? false)}>
      <Base.Portal>
        <Base.Positioner className="layer-pos menu-pos" anchor={anchor ?? undefined} side={placed.current} align={align} sideOffset={4} collisionPadding={8} collisionAvoidance={SHIFT}>
          <Base.Popup ref={popup} className={`menu ${className}`} aria-label={label} style={{ width }}
            finalFocus={false}
            onClick={(e) => e.stopPropagation()}
            onFocus={toFirstItem}
            onKeyDown={(e) => ring(e, popup.current)}>
            <Items items={items} onClose={onClose} onTicked={onTicked} />
          </Base.Popup>
        </Base.Positioner>
      </Base.Portal>
    </Base.Root>
  );
}

function Items({ items, onClose, onTicked }: { items: MenuItem[]; onClose: () => void; onTicked?: () => void }) {
  return (
    <>
      {items.map((item, i) => {
        if (item === "-") return <Base.Separator key={i} className="menu-sep" />;
        if ("group" in item) return <div key={i} className="menu-group" role="presentation">{item.group}</div>;
        const body = menuRowBody({ icon: item.icon, label: item.label, aside: "aside" in item ? item.aside : undefined, kbd: "kbd" in item ? item.kbd : undefined, chevron: "submenu" in item });
        if (!isAction(item)) {
          return (
            <Base.SubmenuRoot key={i}>
              <Base.SubmenuTrigger render={<button type="button" />} nativeButton className="menu-row" disabled={item.disabled} title={item.hint}>{body}</Base.SubmenuTrigger>
              <Base.Portal>
                <Base.Positioner className="layer-pos menu-pos" sideOffset={2} collisionPadding={8}>
                  <Base.Popup className="menu"><Items items={item.submenu} onClose={onClose} onTicked={onTicked} /></Base.Popup>
                </Base.Positioner>
              </Base.Portal>
            </Base.SubmenuRoot>
          );
        }
        const run = () => {
          const done = item.onSelect();
          if (item.ticks) void Promise.resolve(done).then((ok) => { if (ok === true) onTicked?.(); });
        };
        const cls = rowClass({ label: item.label, danger: item.danger, warn: item.warn });
        if (item.checked !== undefined) {
          return (
            <Base.CheckboxItem key={i} render={<button type="button" />} nativeButton className={cls} checked={item.checked} disabled={item.disabled} title={item.hint}
              onCheckedChange={run}>
              {body}
              <span className={`menu-check ${item.checked ? "on" : ""}`} aria-hidden="true">{item.checked && <Icon name="check" size={14} />}</span>
            </Base.CheckboxItem>
          );
        }
        return (
          <Base.Item key={i} render={<button type="button" />} nativeButton className={cls} disabled={item.disabled} title={item.hint}
            onClick={run}>
            {body}
          </Base.Item>
        );
      })}
    </>
  );
}

function lowerHalf(anchor: Anchor | null): "top" | "bottom" {
  if (!anchor) return "bottom";
  return anchor.getBoundingClientRect().top > window.innerHeight / 2 ? "top" : "bottom";
}

/** The keyboard starts on the first command, however the menu was opened. Base UI starts it there
    only for a keyboard open; for a right click or a press of the "…" it focuses the popup itself, a
    moment after it mounts, and the arrows then started from nowhere. So whenever the popup itself
    takes the focus, the first command takes it from the popup. */
/** The same, without waiting for Base UI: a key pressed the moment the menu showed (End, straight
    after Shift+F10) reached the page behind it while the popup was still waiting for the focus. The
    first frames after the popup is drawn give the focus to its first command. */
function useFirstItem(popup: RefObject<HTMLElement | null>) {
  useEffect(() => {
    let frame = 0;
    let tries = 0;
    const go = () => {
      const box = popup.current;
      const shown = box !== null && box.getClientRects().length > 0;
      if (shown && !(box.contains(document.activeElement) && document.activeElement !== box)) box.querySelector<HTMLElement>(ENABLED)?.focus({ preventScroll: true });
      if (!shown && ++tries < 30) frame = requestAnimationFrame(go);
    };
    frame = requestAnimationFrame(go);
    return () => cancelAnimationFrame(frame);
  }, [popup]);
}

function toFirstItem(e: React.FocusEvent<HTMLElement>) {
  if (e.target === e.currentTarget) e.currentTarget.querySelector<HTMLElement>(ENABLED)?.focus({ preventScroll: true });
}

/** A command the keyboard can start on: Base UI marks a disabled one with aria-disabled and keeps it
    focusable, so the arrows reach it and a reader hears why it is off; the first focus and the Tab
    ring pass it by. */
const ENABLED = "button:not(:disabled):not([aria-disabled='true'])";

/** Tab is part of the ring, not a way out of it: this menu carries the app's navigation, and a Tab
    that walked into the page behind it would leave an open menu the reader cannot see. Home and End
    go to the first and the last command. */
function ring(e: React.KeyboardEvent<HTMLElement>, box: HTMLElement | null) {
  if (!box || !["Tab", "Home", "End"].includes(e.key)) return;
  const buttons = Array.from(box.querySelectorAll<HTMLButtonElement>(ENABLED));
  if (!buttons.length) return;
  const i = buttons.indexOf(document.activeElement as HTMLButtonElement);
  // Home and End as a native list has them; Base UI's menu leaves them to the page.
  const to = e.key === "Home" ? 0 : e.key === "End" ? buttons.length - 1 : e.shiftKey ? (i - 1 + buttons.length) % buttons.length : (i + 1) % buttons.length;
  buttons[to].focus();
  e.preventDefault();
}

// ── the overflow button ──────────────────────────────────────────────────────────────────────

/** The "…" button and its menu. With `trigger`, the button's content is that (a title with a
    chevron) rather than an icon. `contextSelector` names the row a right click on it opens the same
    commands from. */
export function OverflowMenu({ items, label, icon = "more", small, className, trigger: content, contextSelector }: { contextSelector?: string; items: MenuItem[]; label?: string; icon?: IconName; small?: boolean; className?: string; trigger?: ReactNode }) {
  const name = label ?? t("dlg.menu");
  const [open, setOpen] = useState(false);
  const [ticked, setTicked] = useState(false);
  const tickTimer = useRef(0);
  useEffect(() => () => window.clearTimeout(tickTimer.current), []);
  const tick = () => {
    setTicked(true);
    window.clearTimeout(tickTimer.current);
    tickTimer.current = window.setTimeout(() => setTicked(false), 1600);
  };
  const button = useRef<HTMLButtonElement>(null);
  useContextActions(button, items, contextSelector);
  return (
    <>
      <button ref={button} type="button" className={`${content ? "" : `iconbtn ${small ? "small" : ""}`} ${open ? "on" : ""} ${className ?? ""}`} aria-label={name} title={name} aria-haspopup="menu" aria-expanded={open}
        onClick={(e) => { e.stopPropagation(); setOpen((o) => !o); }}>
        {ticked ? <Icon name="check" size={16} /> : content ?? <Icon name={icon} size={16} />}
      </button>
      <Menu anchor={button.current} open={open && button.current !== null} onClose={() => setOpen(false)} items={items} label={name} onTicked={tick} />
    </>
  );
}
