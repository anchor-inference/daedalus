// A row of a list: a title, a meta line under it, something before it and something after it. Its
// commands are reached three ways that all open the same menu: a long press, a right click and the
// ⋮ at its end, so an action is never reachable by a gesture alone. A horizontal swipe reveals up to
// three quick actions.

import { type CSSProperties, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { Icon, type IconName } from "../../icons";
import { t } from "../../i18n";
import { Menu, type MenuItem } from "./menu";

export type Preview = { title: ReactNode; meta?: ReactNode };

export type SwipeAction = { label: string; icon: IconName; tone?: "neutral" | "info" | "danger" | "accent"; onSelect: () => void };

const LONG_PRESS_MS = 450;
const SWIPE_W = 76;

/**
 * A long press without a library: a finger held still for 450 ms. Android also sends `contextmenu`
 * for the same press (and a mouse sends it for a right click), so both roads lead to one handler and
 * the click that follows the release is swallowed rather than opening the row under the sheet.
 */
export function useLongPress(onLong: () => void) {
  const timer = useRef<number | null>(null);
  const fired = useRef(false);
  // When the held finger last opened the sheet: the contextmenu Android sends for that same press
  // comes within a moment and is the same request. A flag alone stayed set after a right click (a
  // mouse's secondary press never reset it), and the next right click on that row did nothing.
  const firedAt = useRef(Number.NEGATIVE_INFINITY);
  const origin = useRef<{ x: number; y: number } | null>(null);
  const cancel = useCallback(() => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = null;
  }, []);
  useEffect(() => cancel, [cancel]);
  return {
    fired,
    cancel,
    handlers: {
      onPointerDown: (e: ReactPointerEvent) => {
        if (e.pointerType === "mouse" && e.button !== 0) return;
        fired.current = false;
        origin.current = { x: e.clientX, y: e.clientY };
        cancel();
        if (e.pointerType === "mouse") return;
        timer.current = window.setTimeout(() => { fired.current = true; firedAt.current = performance.now(); timer.current = null; onLong(); }, LONG_PRESS_MS);
      },
      onPointerMove: (e: ReactPointerEvent) => {
        const o = origin.current;
        if (o && Math.abs(e.clientX - o.x) + Math.abs(e.clientY - o.y) > 10) cancel();
      },
      onPointerUp: cancel,
      onPointerCancel: cancel,
      onContextMenu: (e: ReactMouseEvent) => {
        e.preventDefault();
        e.stopPropagation();
        cancel();
        if (performance.now() - firedAt.current < 1000) return;
        fired.current = true;
        onLong();
      },
    },
  };
}

export type RowProps = {
  title: ReactNode;
  meta?: ReactNode;
  lead?: ReactNode;
  trail?: ReactNode;
  onOpen: () => void;
  /** The chosen row of a list: the open chat, the selected task. */
  selected?: boolean;
  /** 32 for a line of its own, 44 for a title over a meta line, 56 for a large list; left out, 44
      with a meta line and 32 without on a desktop (a phone's own heights come from phone.css). */
  height?: 32 | 44 | 56;
  /** The row's commands: long-press, right click and the ⋮ all open them as a menu. */
  actions?: MenuItem[];
  /** A sheet of the row's own instead of the menu (a board task's sheet has a row of chips the
   *  menu cannot hold): long-press, right click and the ⋮ call this, and the caller draws the sheet. */
  onMenu?: () => void;
  preview?: Preview;
  /** Revealed by a swipe to the left (up to three), or `swipeStart` by a swipe to the right (one). */
  swipe?: SwipeAction[];
  swipeStart?: SwipeAction;
  /** One line only (52 px): a title without a meta line. */
  one?: boolean;
  /** More under the meta line: a command's text, answers. Presses on it do not open the row. */
  body?: ReactNode;
  /** The ⋮ beside the row; off where the row's commands are reachable elsewhere on the page (a
   *  selection mode, the detail sheet), so a dense list keeps its width for the words. */
  more?: boolean;
  unread?: boolean;
  current?: boolean;
  /** The accessible name of the ⋮; the title is used when it is a string. */
  label?: string;
  className?: string;
  data?: Record<string, string>;
};

export function Row({ title, meta, lead, trail, onOpen, selected, height, actions, onMenu, preview, swipe, swipeStart, one, body, more = true, unread, current, label, className = "", data }: RowProps) {
  const [sheetOpen, setSheetOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const [dragging, setDragging] = useState(false);
  const hasActions = !!onMenu || (!!actions && actions.some((a) => a !== "-"));
  const sheet = sheetOpen && !onMenu;
  const setSheet = (open: boolean) => { if (open && onMenu) onMenu(); else setSheetOpen(open); };
  const press = useLongPress(() => { if (hasActions) setSheet(true); });
  const drag = useRef<{ x: number; y: number; base: number; horizontal: boolean | null } | null>(null);
  const leftMax = swipe?.length ? -SWIPE_W * Math.min(3, swipe.length) : 0;
  const rightMax = swipeStart ? SWIPE_W : 0;
  const name = label ?? (typeof title === "string" ? title : t("dlg.menu"));
  const attrs = Object.fromEntries(Object.entries(data ?? {}).map(([k, v]) => [`data-${k}`, v]));

  const down = (e: ReactPointerEvent<HTMLDivElement>) => {
    press.handlers.onPointerDown(e);
    if ((leftMax || rightMax) && e.pointerType !== "mouse") drag.current = { x: e.clientX, y: e.clientY, base: offset, horizontal: null };
  };
  const move = (e: ReactPointerEvent<HTMLDivElement>) => {
    press.handlers.onPointerMove(e);
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x, dy = e.clientY - d.y;
    if (d.horizontal === null && Math.abs(dx) + Math.abs(dy) > 10) d.horizontal = Math.abs(dx) > Math.abs(dy);
    if (!d.horizontal) return;
    setDragging(true);
    setOffset(Math.max(leftMax, Math.min(rightMax, d.base + dx)));
  };
  const up = () => {
    press.handlers.onPointerUp();
    const d = drag.current;
    drag.current = null;
    if (!d?.horizontal) return;
    setDragging(false);
    // Past half of the actions' width the row stays open on them; short of it, it springs back.
    setOffset((o) => (o < leftMax / 2 ? leftMax : o > rightMax / 2 && rightMax ? rightMax : 0));
    press.fired.current = true;
  };
  const click = () => {
    if (press.fired.current) { press.fired.current = false; return; }
    if (offset !== 0) { setOffset(0); return; }
    onOpen();
  };
  const run = (action: SwipeAction) => { setOffset(0); action.onSelect(); };

  return (
    <div className={`list-row-wrap ${offset !== 0 ? "swiped" : ""}`} {...attrs}>
      {swipeStart && offset > 0 && (
        <div className="list-row-swipe start">
          <button type="button" className={`list-row-swipe-act ${swipeStart.tone ?? "accent"}`} onClick={() => run(swipeStart)}><Icon name={swipeStart.icon} size={18} />{swipeStart.label}</button>
        </div>
      )}
      {swipe && offset < 0 && (
        <div className="list-row-swipe end">
          {swipe.slice(0, 3).map((action) => (
            <button key={action.label} type="button" className={`list-row-swipe-act ${action.tone ?? "neutral"}`} onClick={() => run(action)}><Icon name={action.icon} size={18} />{action.label}</button>
          ))}
        </div>
      )}
      <div
        className={`list-row h${height ?? (meta && !one ? 44 : 32)} ${one ? "one" : ""} ${unread ? "unread" : ""} ${current || selected ? "current" : ""} ${sheet ? "pressed" : ""} ${className}`.replace(/\s+/g, " ").trim()}
        role="link"
        tabIndex={0}
        aria-current={current ? "page" : undefined}
        style={offset !== 0 || dragging ? ({ transform: `translateX(${offset}px)`, transition: dragging ? "none" : undefined } as CSSProperties) : undefined}
        onClick={click}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); }
          else if (hasActions && (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10"))) { e.preventDefault(); setSheet(true); }
        }}
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={() => { press.handlers.onPointerCancel(); drag.current = null; setDragging(false); setOffset(0); }}
        onContextMenu={hasActions ? press.handlers.onContextMenu : undefined}
      >
        {lead && <span className="list-row-lead">{lead}</span>}
        <span className="list-row-main">
          <span className="list-row-t">{title}</span>
          {meta && !one && <span className="list-row-m">{meta}</span>}
          {body && <span className="list-row-b" onClick={stopOnControl} onPointerDown={stopOnControl}>{body}</span>}
        </span>
        {trail !== undefined && <span className="list-row-trail">{trail}</span>}
        {hasActions && more && (
          <button type="button" className="list-row-more" aria-label={`${name}: ${t("dlg.menu")}`} title={t("dlg.menu")} aria-haspopup="menu"
            onClick={(e) => { e.stopPropagation(); setSheet(true); }} onPointerDown={(e) => e.stopPropagation()}>
            <Icon name="vdots" size={18} />
          </button>
        )}
      </div>
      {sheet && actions && <Menu items={actions} label={name} preview={preview ?? { title, meta }} onClose={() => setSheet(false)} />}
    </div>
  );
}

/** A press on a control inside a row's body is the control's, not the row's: no open, no long press. */
function stopOnControl(e: { target: EventTarget; stopPropagation: () => void }) {
  if (e.target instanceof Element && e.target.closest("button, a, input, textarea, select")) e.stopPropagation();
}
