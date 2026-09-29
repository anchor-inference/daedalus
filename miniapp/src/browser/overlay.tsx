// The agent's hand, drawn by the app over the picture of the page: a pointer that glides to where the
// agent is about to act, a ripple where it clicks, a frame around the element with its name, and a chip
// that says what is being typed or pressed.
//
// Nothing of this is in the page. An overlay injected into the page would be in the agent's own
// snapshots and screenshots, a hostile page could hide it or fake one, and it would move the layout
// the agent is reading. Here it is placed from the frame's metadata (geometry.ts), in the viewer's own
// layer, and the thumbnail draws the same thing at its own scale.

import { memo, type CSSProperties } from "react";
import { plural, t } from "../i18n";
import { boxToView, pageToView, type Rect } from "./geometry";
import type { ActionEvent, FrameMeta } from "./protocol";

const CLICKS = new Set(["click", "double_click", "right_click", "check", "uncheck", "select", "upload"]);

export const CursorOverlay = memo(function CursorOverlay({ action, meta, rect, parked, compact, agent }: {
  action: (ActionEvent & { done?: boolean }) | null;
  meta: FrameMeta | null;
  rect: Rect;
  /** A person drives: the agent's pointer greys out where it last was. */
  parked: boolean;
  /** The thumbnail: the pointer and the frame only, no words. */
  compact?: boolean;
  /** The name on the pointer's tag. */
  agent: string;
}) {
  if (!meta || !action || rect.w <= 0) return null;
  const point = action.point ?? (action.box ? { x: action.box.x + action.box.w / 2, y: action.box.y + action.box.h / 2 } : null);
  if (!point) return null;
  const at = pageToView(meta, rect, point);
  const box = action.box ? boxToView(meta, rect, action.box) : null;
  const cursorStyle: CSSProperties = { transform: `translate3d(${at.x}px, ${at.y}px, 0)` };
  // A person driving makes the agent's last keystrokes history: the chips go, the waiting tag stays.
  const typing = action.kind === "type" && !compact && !parked;
  const pressing = action.kind === "press" && !!action.keys && !compact && !parked;
  return (
    <div className={`bv-overlay ${compact ? "compact" : ""} ${parked ? "parked" : ""}`} aria-hidden="true" data-action={action.id} data-kind={action.kind}>
      {box && (
        <div key={`box-${action.id}`} className="bv-box" style={{ left: box.x, top: box.y, width: box.w, height: box.h }} data-box={`${Math.round(box.x)},${Math.round(box.y)},${Math.round(box.w)},${Math.round(box.h)}`}>
          {!compact && action.name && <span className="bv-box-label">{action.name}</span>}
        </div>
      )}
      {CLICKS.has(action.kind) && !parked && <span key={`ripple-${action.id}`} className="bv-ripple" style={{ left: at.x, top: at.y }} />}
      {(typing || pressing) && box && (
        <span key={`chip-${action.id}`} className="bv-chip" style={{ left: box.x, top: box.y + box.h + 6 }}>
          {typing ? plural("browser.cursor.typing", action.text_len ?? 0) : <kbd>{action.keys}</kbd>}
        </span>
      )}
      <div className="bv-cursor" style={cursorStyle} data-x={Math.round(at.x * 10) / 10} data-y={Math.round(at.y * 10) / 10}>
        <svg viewBox="0 0 24 24" width={compact ? 14 : 22} height={compact ? 14 : 22} aria-hidden="true">
          <path className="bv-cursor-arrow" d="M4 2.5l15.2 8.3-6.6 1.7-3.1 6.9z" />
        </svg>
        {/* The chip under a field already says who is typing; the tag would sit on top of it. A
            parked pointer has no tag at all: the banner above the page says the agent is waiting,
            and a dark pill left under a search box read as the site's own suggestion list. */}
        {!compact && !parked && !typing && !pressing && <span className="bv-cursor-tag">{agent}</span>}
      </div>
    </div>
  );
});
