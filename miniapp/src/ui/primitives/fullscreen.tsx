// A layer over the whole window for one thing seen large: a picture, a drawing being annotated, a
// terminal. A toolbar along the top carries its name, its own controls and the way out.

import { Dialog } from "@base-ui/react/dialog";
import { useRef, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { Icon } from "../../icons";
import { t } from "../../i18n";
import { closeHandler, useFocusBack, useLayer } from "./layers";
import { STOP } from "./sheet";

export type FullscreenLayerProps = {
  label: string;
  onClose: () => void;
  toolbar?: ReactNode;
  className?: string;
  /** Keys pressed anywhere in the layer, the bar's buttons included: a viewer's arrows page it. */
  onKeyDown?: (event: KeyboardEvent<HTMLElement>) => void;
  /** The layer's own box, for a viewer that fades as it is pulled away. */
  style?: CSSProperties;
  children: ReactNode;
};

/** STOP stops a key at the layer's root, so a viewer's own handler is called before it. */
export function FullscreenLayer({ label, onClose, toolbar, className = "", onKeyDown, style, children }: FullscreenLayerProps) {
  const isTop = useLayer(onClose);
  const opener = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null);
  useFocusBack(() => opener.current);
  return (
    <Dialog.Root open modal onOpenChange={closeHandler(isTop, onClose)}>
      <Dialog.Portal className="overlay-root">
        <Dialog.Popup className={`fullscreen-layer ${className}`.trim()} aria-label={label} aria-modal="true" data-overlay-root="" {...STOP}
          style={style} finalFocus={false}
          onKeyDown={(event) => { onKeyDown?.(event); STOP.onKeyDown(event); }}>
          <div className="fullscreen-bar">
            <Dialog.Title className="fullscreen-title">{label}</Dialog.Title>
            {toolbar}
            <Dialog.Close className="iconbtn" aria-label={t("common.close")} title={t("common.close")}><Icon name="close" size={16} /></Dialog.Close>
          </div>
          <div className="fullscreen-body">{children}</div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
