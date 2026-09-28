// The corner preview: while the agent on screen has a browser open, a small live picture of it sits
// at the top right of the conversation, with the agent's cursor on it. A click opens the panel's
// Browser tab on the same page, live, and the card grows into it.
//
// It lives inside the conversation's column, never the page: with the panel open it is left of the
// panel, never over it, and a dual view gives each pane its own. It is not there while the Browser tab
// already shows the page, nor while the panel covers the chat. Below 520 px of column (960 on a touch
// screen, model.ts says why) it shrinks to a pill, so it never covers the words being read, and it can be dragged to any corner, which this
// device remembers. ✕ hides it until the group does something new or asks for the operator.

import { useCallback, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import type { BrowserGroup } from "../api";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { askTake, handOff, useLiveSnapshot, useLiveView } from "./data";
import { Favicon } from "./favicon";
import { agentName, domainOf, driveState, extraCount, nearestCorner, needsOf, needWords, PIP_PILL_BELOW, PIP_PILL_BELOW_TOUCH, pipGroup, pipHidden, readCorner, rememberCorner, type Corner, type DriveState } from "./model";
import { BrowserViewer } from "./viewer";
import { useMedia } from "../shell";

/** Groups hidden with ✕ this visit, and the activity they were hidden at. */
const HIDDEN = new Map<string, string>();
/** A drag shorter than this was a click. */
const DRAG_SLOP_PX = 4;
/** From the column's edges, and above whatever sits under the conversation (the composer, the dock). */
const INSET = { side: 12, top: 8, bottom: 12 };

export function BrowserPip({ groups, onOpen }: { groups: BrowserGroup[]; onOpen: () => void }) {
  const [, rerender] = useState(0);
  const group = pipGroup(groups);
  if (!group || pipHidden(group, HIDDEN)) return null;
  return (
    <PipCard
      key={group.id}
      group={group}
      extra={extraCount(groups, group)}
      onOpen={(take) => {
        if (take) askTake(group.id);
        onOpen();
      }}
      onHide={() => {
        HIDDEN.set(group.id, group.last_activity_at);
        rerender((n) => n + 1);
      }}
    />
  );
}

function PipCard({ group, extra, onOpen, onHide }: { group: BrowserGroup; extra: number; onOpen: (take: boolean) => void; onHide: () => void }) {
  const card = useRef<HTMLDivElement>(null);
  const [corner, setCorner] = useState<Corner>(() => readCorner());
  const [column, setColumn] = useState({ w: 0, h: 0, bottom: INSET.bottom });
  const [drag, setDrag] = useState<{ dx: number; dy: number } | null>(null);
  const touch = useMedia("(hover: none) and (pointer: coarse)");
  const pill = column.w > 0 && column.w < (touch ? PIP_PILL_BELOW_TOUCH : PIP_PILL_BELOW);
  const live = useLiveView(pill ? null : group.id, "thumb", { readOnly: true, box: () => ({ max_w: 320, max_h: 200 }) });
  const snap = useLiveSnapshot(live);
  const drive: DriveState = driveState({ ...group, needs_you: needsOf(group.needs_you, snap) }, snap.control);
  const tab = group.tabs.find((x) => x.id === group.active_tab) ?? group.tabs.find((x) => x.active) ?? group.tabs[0];
  const url = snap.tabs.find((x) => x.id === snap.active)?.url ?? tab?.url ?? "";
  const favicon = snap.tabs.find((x) => x.id === snap.active)?.favicon_url ?? tab?.favicon_url ?? "";

  // The column the card lives in, and how much of its foot the composer and the dock take.
  useLayoutEffect(() => {
    const el = card.current?.parentElement;
    if (!el) return;
    const measure = () => {
      const scroll = el.querySelector<HTMLElement>(":scope > .chat-scroll, :scope > .feed-scroll, :scope > .staff-term, :scope > .feed");
      const bottom = scroll ? Math.max(INSET.bottom, el.clientHeight - (scroll.offsetTop + scroll.offsetHeight) + INSET.bottom) : INSET.bottom;
      setColumn((c) => (c.w === el.clientWidth && c.h === el.clientHeight && c.bottom === bottom ? c : { w: el.clientWidth, h: el.clientHeight, bottom }));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    for (const child of Array.from(el.children)) if (child !== card.current) ro.observe(child);
    return () => ro.disconnect();
  }, []);

  const open = useCallback((take: boolean) => {
    const r = card.current?.querySelector(".bp-pip-frame")?.getBoundingClientRect();
    if (r) handOff(group.id, r);
    onOpen(take);
  }, [group.id, onOpen]);

  // Dragging to a corner. The card follows the pointer, and snaps to the nearest corner when let go.
  const start = useRef<{ x: number; y: number; id: number; moved: boolean } | null>(null);
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if ((e.target as HTMLElement).closest("button") || e.button !== 0) return;
    start.current = { x: e.clientX, y: e.clientY, id: e.pointerId, moved: false };
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    const s = start.current;
    if (!s || s.id !== e.pointerId) return;
    const dx = e.clientX - s.x;
    const dy = e.clientY - s.y;
    if (!s.moved && Math.hypot(dx, dy) < DRAG_SLOP_PX) return;
    s.moved = true;
    setDrag({ dx, dy });
  };
  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    const s = start.current;
    start.current = null;
    if (!s || s.id !== e.pointerId) return;
    if (!s.moved) {
      open(false);
      return;
    }
    const el = card.current;
    const parent = el?.parentElement;
    setDrag(null);
    if (!el || !parent) return;
    const r = el.getBoundingClientRect();
    const p = parent.getBoundingClientRect();
    const next = nearestCorner(r.left + r.width / 2 - p.left, r.top + r.height / 2 - p.top, p.width, p.height);
    setCorner(next);
    rememberCorner(next);
  };

  const needs = drive === "needs" ? needsOf(group.needs_you, snap) : null;
  const style: CSSProperties = {
    ...(corner[0] === "t" ? { top: INSET.top } : { bottom: column.bottom }),
    ...(corner[1] === "r" ? { right: INSET.side } : { left: INSET.side }),
    ...(drag ? { transform: `translate(${drag.dx}px, ${drag.dy}px)`, transition: "none" } : {}),
  };
  const domain = domainOf(url) || t("browser.blank");
  const label = needs ? t("browser.pip.needs", { what: needWords(needs) }) : t("browser.pip.open", { name: agentName(group), domain });
  return (
    <div
      ref={card}
      className={`bp-pip ${pill ? "pill" : ""} ${drive} ${drag ? "dragging" : ""}`}
      style={style}
      data-corner={corner}
      data-drive={drive}
      data-group={group.id}
      role="button"
      tabIndex={0}
      aria-label={label}
      title={label}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={() => { start.current = null; setDrag(null); }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open(false);
        }
      }}
    >
      {!pill && (
        <div className="bp-pip-frame">
          <BrowserViewer live={live} snap={snap} tier="thumb" interactive={false} compact agent={agentName(group)} />
          {needs && <div className="bp-pip-need"><Icon name="alert" size={12} /><span className="truncate">{needWords(needs)}</span></div>}
          <div className="bp-pip-actions">
            <button type="button" className="iconbtn small" onClick={() => open(false)} aria-label={t("browser.pip.expand")} title={t("browser.pip.expand")}><Icon name="expand" size={14} /></button>
            <button type="button" className="iconbtn small" onClick={() => open(true)} aria-label={t("browser.take")} title={t("browser.take")}><Icon name="user" size={14} /></button>
            <button type="button" className="iconbtn small" onClick={onHide} aria-label={t("browser.pip.hide")} title={t("browser.pip.hide")}><Icon name="close" size={14} /></button>
          </div>
        </div>
      )}
      <div className="bp-pip-foot">
        <Favicon url={favicon} page={url} size={14} />
        <span className="bp-pip-domain truncate">{domain}</span>
        {extra > 0 && <span className="bp-pip-more" title={plural("browser.pip.more", extra)}>+{extra}</span>}
        <span className={`bp-pip-state ${drive}`}>
          <i className="browser-dot-i" aria-hidden="true" />
          {drive === "needs" && <span>{t("browser.needs")}</span>}
          {drive === "you" && <span>{t("browser.drive.you")}</span>}
        </span>
        {pill && <button type="button" className="bp-pip-x" onClick={onHide} aria-label={t("browser.pip.hide")}><Icon name="close" size={12} /></button>}
      </div>
    </div>
  );
}

export { HIDDEN as hiddenPips };
