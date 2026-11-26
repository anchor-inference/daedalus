// A modal task: a dialog in the middle of a desktop, a sheet from the bottom of a phone. Base UI's
// Dialog does the focus trap, the focus return and the outside press; the app's layer stack decides
// which layer Escape and an outside press belong to (layers.ts says why).

import { Dialog } from "@base-ui/react/dialog";
import { useRef, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { useMoreBelow } from "../../edgefade";
import { Icon } from "../../icons";
import { t } from "../../i18n";
import { closeHandler, useFocusBack, useLayer } from "./layers";

/** The five widths a sheet comes in on a desktop: 440, 560, 720, 960 and the whole window. There
    were five widths before as well (440, 460, 480, 820, 1100), none of them named for a use. */
export type SheetSize = "sm" | "md" | "lg" | "xl" | "full";

export type SheetProps = {
  title?: ReactNode;
  /** The accessible name when the title is not a string, or there is none. */
  ariaLabel?: string;
  onClose: () => void;
  children: ReactNode;
  /** `lg` when left out. */
  size?: SheetSize;
  className?: string;
  /** More in the head row, after the title: a count, a switch, a second line. */
  head?: ReactNode;
  /** The sheet's decision, kept in view at the foot of the body while the body scrolls. */
  footer?: ReactNode;
  /** What the sheet is about, above its body: the row a long press was made on. */
  preview?: { title: ReactNode; meta?: ReactNode };
};

export function Sheet({ title, ariaLabel, onClose, children, size = "lg", className = "", head, footer, preview }: SheetProps) {
  const isTop = useLayer(onClose);
  // The control that opened the sheet, read on the first render: by the time the popup mounts, the
  // focus has already moved into it.
  const opener = useRef<HTMLElement | null>(typeof document === "undefined" ? null : document.activeElement as HTMLElement | null);
  const panel = useRef<HTMLDivElement>(null);
  useFocusBack(() => opener.current);
  return (
    <Dialog.Root open modal onOpenChange={closeHandler(isTop, onClose)}>
      <Dialog.Portal className="overlay-root">
        {/* The portal is `.overlay-root`, a child of the body, as every layer's root has been: the
            phone's composer keeps its toolbar open while the focus is in one. The viewport is the scrim and the box that
            places the sheet, as `.sheet-backdrop` always was: the checks and the screens' own rules
            address the sheet as `.sheet-backdrop .sheet`. */}
        <Dialog.Viewport className="sheet-backdrop" data-overlay-root="" {...STOP}>
          <Dialog.Popup
            ref={panel}
            className={`sheet ${size} ${title ? "" : "untitled"} ${className}`.replace(/\s+/g, " ").trim()}
            aria-label={typeof title === "string" ? title : ariaLabel ?? (typeof preview?.title === "string" ? preview.title : undefined)}
            aria-modal="true"
            initialFocus={() => keepInnerFocus(panel)}
            finalFocus={false}
          >
            <div className="grip" aria-hidden="true" />
            <div className="sheet-head">
              {title && <Dialog.Title render={<h3 />}>{title}</Dialog.Title>}
              {head}
              <Dialog.Close className="iconbtn small" aria-label={t("common.close")} title={t("common.close")}><Icon name="close" size={16} /></Dialog.Close>
            </div>
            <SheetBody footer={footer}>
              {preview && <SheetPreview title={preview.title} meta={preview.meta} />}
              {children}
            </SheetBody>
          </Dialog.Popup>
        </Dialog.Viewport>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

/** React carries an event out of a portal to the component that declared it, so a press inside a
    sheet declared in a row reached the row's own click and opened it again under the sheet. */
export const STOP = {
  onClick: (e: { stopPropagation: () => void }) => e.stopPropagation(),
  onKeyDown: (e: { stopPropagation: () => void }) => e.stopPropagation(),
  onMouseDown: (e: { stopPropagation: () => void }) => e.stopPropagation(),
  onPointerDown: (e: { stopPropagation: () => void }) => e.stopPropagation(),
};

/** A field that took the focus as it mounted (an `autoFocus` name field) keeps it; otherwise the
    sheet itself takes it, as it always has, so a reader's first Tab starts at the sheet's top. */
function keepInnerFocus(panel: RefObject<HTMLElement | null>): HTMLElement | true {
  const box = panel.current;
  const active = document.activeElement as HTMLElement | null;
  if (box && active && active !== box && box.contains(active)) return active;
  return box ?? true;
}

/** The body is its own component because the portal draws it a commit after the sheet: measured
    from the sheet, the "more below" watch started on an element that did not exist yet. */
function SheetBody({ children, footer }: { children: ReactNode; footer?: ReactNode }) {
  const body = useRef<HTMLDivElement>(null);
  useMoreBelow(body);
  return (
    <>
      <div ref={body} className="sheet-body">
        {children}
        {footer && <div className="sheet-foot">{footer}</div>}
      </div>
      {/* Shown by the stylesheet only while the body has more below its fold (data-more) and no
          sticky footer: a touch screen draws no scrollbar, and a task sheet whose actions sat under
          the fold ended at a clean edge that looked like its end. A tap scrolls most of a screen on. */}
      <div className="sheet-more-anchor">
        <button type="button" className="sheet-more" tabIndex={-1} aria-label={t("common.more.below")} title={t("common.more.below")} onClick={() => body.current?.scrollBy({ top: body.current.clientHeight * 0.8, behavior: "smooth" })}>
          <Icon name="chevron" size={16} />
        </button>
      </div>
    </>
  );
}

/** What a sheet of commands is about, above them: the row a long press was made on. */
export function SheetPreview({ title, meta }: { title: ReactNode; meta?: ReactNode }) {
  return (
    <div className="sheet-preview">
      <div className="sheet-preview-t">{title}</div>
      {meta && <div className="sheet-preview-m">{meta}</div>}
    </div>
  );
}

/** A hand-drawn overlay rendered in the document, never where it was declared. Kept for the
    screens' own overlays until each moves onto a primitive.

    An ancestor with a transform, a filter or `contain` becomes the containing block of a
    `position: fixed` descendant, and its `overflow: hidden` clips it. A sheet declared inside a
    pressable card (`:active { transform: scale(…) }`) therefore snapped to the card under the
    pressed mouse button and lost its body to the card's clip; the release landed outside the
    control, so the choice never registered. React still bubbles events through a portal to the
    component tree, so the root stops its clicks and key presses from reaching the row that
    declared it — a row that opens on click must not open under its own dialog. */
export function Overlay({ children }: { children: ReactNode }) {
  return createPortal(
    <div className="overlay-root" data-overlay-root="" {...STOP}>
      {children}
    </div>,
    document.body,
  );
}
