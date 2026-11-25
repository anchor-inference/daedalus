// A layer over the whole window for one thing seen large: a picture, a drawing being annotated, a
// terminal. A toolbar along the top carries its name, its own controls and the way out.

import { Dialog } from "@base-ui/react/dialog";
import { useRef, type ReactNode } from "react";
import { Icon } from "../../icons";
import { t } from "../../i18n";
import { closeHandler, useFocusBack, useLayer } from "./layers";
import { STOP } from "./sheet";

/** `data` is written onto the layer as `data-*` attributes, for a layer whose state its own styles and checks read (the annotation editor's tool and mark count). */
export function FullscreenLayer({ label, onClose, toolbar, className = "", data, children }: { label: string; onClose: () => void; toolbar?: ReactNode; className?: string; data?: Record<string, string | number>; children: ReactNode }) {
  const isTop = useLayer(onClose);
  const opener = useRef<HTMLElement | null>(document.activeElement as HTMLElement | null);
  useFocusBack(() => opener.current);
  return (
    <Dialog.Root open modal onOpenChange={closeHandler(isTop, onClose)}>
      <Dialog.Portal className="overlay-root">
        <Dialog.Popup className={`fullscreen-layer ${className}`.trim()} aria-label={label} aria-modal="true" data-overlay-root="" {...STOP}
          {...Object.fromEntries(Object.entries(data ?? {}).map(([k, v]) => [`data-${k}`, String(v)]))}
          finalFocus={false}>
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
