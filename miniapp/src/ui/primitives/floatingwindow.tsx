// A floating panel over the chat on a desktop: dragged by its title bar, kept inside the screen,
// and left where the viewer last put it. The prompt coach and the side question are drawn in it.

import { type PointerEvent as ReactPointerEvent, type ReactNode, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Icon, type IconName } from "../../icons";
import { t } from "../../i18n";
import { COMPOSER_CLEARANCE, EDGE, type Point, clampPosition, defaultPosition, readPosition, windowSize, writePosition } from "./floating";

/**
 * The desktop's floating panel: dragged by its title bar, kept inside the screen, and left where the
 * viewer last put it. Minimised, it is a bar in the corner and the session inside stays mounted, so a
 * run streaming in it is not dropped and picked up again. The side question's window is the same panel
 * under its own title and with a place of its own.
 */
export function FloatingWindow({ title, icon, positionKey, className, minimised, onMinimise, onClose, menu, children }: { title: string; icon: IconName; positionKey: string; className?: string; minimised: boolean; onMinimise: () => void; onClose: () => void; menu: ReactNode; children: ReactNode }) {
  const viewport = () => ({ width: window.innerWidth, height: window.innerHeight });
  const [size, setSize] = useState(() => windowSize(viewport()));
  const [at, setAt] = useState<Point>(() => {
    const remembered = readPosition(positionKey);
    const box = windowSize(viewport());
    return remembered ? clampPosition(remembered, box, viewport()) : defaultPosition(box, viewport());
  });
  useEffect(() => {
    const onResize = () => {
      const box = windowSize(viewport());
      setSize(box);
      setAt((now) => clampPosition(now, box, viewport()));
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  const drag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (minimised || event.button !== 0 || (event.target as HTMLElement).closest("button")) return;
    event.preventDefault();
    const from = { x: event.clientX - at.x, y: event.clientY - at.y };
    let last = at;
    const move = (e: PointerEvent) => {
      last = clampPosition({ x: e.clientX - from.x, y: e.clientY - from.y }, size, viewport());
      setAt(last);
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      writePosition(last, positionKey);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  };
  const style = minimised ? { right: EDGE * 1.5, bottom: COMPOSER_CLEARANCE } : { left: at.x, top: at.y, width: size.width, height: size.height };
  return createPortal(
    <div className={`coach-window ${minimised ? "min" : ""} ${className ?? ""}`} style={style} role="dialog" aria-label={title}>
      <div className="coach-head" onPointerDown={drag} title={minimised ? undefined : t("coach.drag")}>
        <Icon name={icon} size={16} />
        <span className="coach-title grow truncate">{title}</span>
        {menu}
        <button type="button" className="iconbtn small" onClick={onMinimise} aria-label={t(minimised ? "coach.restore" : "coach.minimise")} title={t(minimised ? "coach.restore" : "coach.minimise")}>
          <Icon name={minimised ? "expand" : "down"} size={16} />
        </button>
        <button type="button" className="iconbtn small" onClick={onClose} aria-label={t("common.close")} title={t("common.close")}>
          <Icon name="close" size={16} />
        </button>
      </div>
      <div className="coach-frame" hidden={minimised}>{children}</div>
    </div>,
    document.body,
  );
}
