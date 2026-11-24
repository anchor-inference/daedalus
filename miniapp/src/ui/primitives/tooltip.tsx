// The name of an icon-only control, shown after the pointer rests on it for 600 ms, with its key.

import { Tooltip as Base } from "@base-ui/react/tooltip";
import type { ReactElement, ReactNode } from "react";

export function Tooltip({ label, kbd, side = "bottom", children }: { label: string; kbd?: string; side?: "top" | "bottom" | "left" | "right"; children: ReactElement<Record<string, unknown>> }) {
  return (
    <Base.Root>
      <Base.Trigger delay={600} render={children} />
      <Base.Portal>
        <Base.Positioner className="layer-pos tooltip-pos" side={side} sideOffset={6} collisionPadding={8}>
          <Base.Popup className="tooltip">
            {label}
            {kbd && <kbd className="kbd">{kbd}</kbd>}
          </Base.Popup>
        </Base.Positioner>
      </Base.Portal>
    </Base.Root>
  );
}

/** One provider for the app, so moving from one tooltip's control to the next shows the second at
    once instead of waiting out the delay again. */
export function TooltipProvider({ children }: { children: ReactNode }) {
  return <Base.Provider delay={600} closeDelay={0}>{children}</Base.Provider>;
}
