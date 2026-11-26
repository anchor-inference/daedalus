// A diagram drawn large, in a sheet that takes the window: the drawing in an answer is as wide as the
// answer's column, which a flow of a dozen boxes outgrows. Diagrams are written into the page as
// markup by `diagrams.ts`, outside any React tree, so the sheet has a root of its own, made when the
// first one is opened and kept for the rest.

import { createRoot, type Root } from "react-dom/client";
import { RawHtml } from "./rawhtml";
import { t } from "./i18n";
import { Sheet } from "./ui/primitives";

let root: Root | null = null;

/** Shows `svg` (markup already scoped to its own ids) large. The sheet gives the focus back to `from`. */
export function openDiagramZoom(svg: string): void {
  if (!root) {
    const host = document.createElement("div");
    host.dataset.diagramHost = "";
    document.body.appendChild(host);
    root = createRoot(host);
  }
  const close = () => root?.render(null);
  root.render(
    <Sheet size="full" title={t("diagram.label")} className="diagram-zoom" onClose={close}>
      <RawHtml className="diagram-zoom-body" html={svg} />
    </Sheet>,
  );
}
