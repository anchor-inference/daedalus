import { useEffect, useRef, useState } from "react";
import { Excalidraw } from "@excalidraw/excalidraw";
import "@excalidraw/excalidraw/index.css";
import { api } from "../api";
import { relTimeLong } from "../format";
import { t } from "../i18n";
import { UI_OPTIONS, exposeView, fitOnOpen, useExcalidrawLang, useScheme, type Scene } from "./diagramparts";
import "./diagrams.css";

type SharedScene = { title: string; version: number; updated_at?: string; scene: Scene };

/** A diagram someone was sent a link to: read-only, in the reader's own theme and language, with no
 *  part of the app around it. */
export function SharedDiagram({ token }: { token: string }) {
  const theme = useScheme();
  const langCode = useExcalidrawLang();
  const [diagram, setDiagram] = useState<SharedScene | null>(null);
  const [failed, setFailed] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const tag = document.createElement("meta");
    tag.name = "robots";
    tag.content = "noindex, nofollow";
    document.head.appendChild(tag);
    return () => tag.remove();
  }, []);
  useEffect(() => {
    let active = true;
    api.get<SharedScene>(`/api/public/diagrams/${encodeURIComponent(token)}`).then((scene) => { if (active) setDiagram(scene); }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [token]);
  useEffect(() => {
    if (diagram) document.title = diagram.title;
  }, [diagram]);
  if (failed) return <div className="diagram-shared-state"><strong>{t("diagrams.share.unavailable")}</strong><span>{t("diagrams.share.unavailable.body")}</span></div>;
  if (!diagram) return <div className="diagram-shared-state">{t("common.loading")}</div>;
  return (
    <div className="diagram-shared">
      <header>
        <div className="diagram-shared-title">
          <strong>{diagram.title}</strong>
          {diagram.updated_at && <span>{t("diagrams.shared.updated", { time: relTimeLong(diagram.updated_at) })}</span>}
        </div>
        <span className="diagram-shared-made">{t("diagrams.shared.made")}</span>
      </header>
      <div ref={box} className="diagram-canvas">
        <Excalidraw excalidrawAPI={(handle) => { fitOnOpen(handle); exposeView(handle, box.current); }} viewModeEnabled theme={theme} langCode={langCode} UIOptions={UI_OPTIONS} initialData={{ elements: diagram.scene.elements as any, appState: { ...diagram.scene.appState, collaborators: new Map() } as any, files: diagram.scene.files as any, scrollToContent: true }} />
      </div>
    </div>
  );
}
