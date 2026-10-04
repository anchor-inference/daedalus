import { useEffect, useState } from "react";
import { Excalidraw } from "@excalidraw/excalidraw";
import "@excalidraw/excalidraw/index.css";
import { api } from "../api";
import { t, useLang } from "../i18n";
import "./diagrams.css";

type SharedScene = { title: string; version: number; scene: { elements: any[]; appState: Record<string, unknown>; files: Record<string, unknown> } };

export function SharedDiagram({ token }: { token: string }) {
  useLang();
  const [diagram, setDiagram] = useState<SharedScene | null>(null);
  const [failed, setFailed] = useState(false);
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
  if (failed) return <div className="diagram-shared-state">{t("diagrams.share.unavailable")}</div>;
  if (!diagram) return <div className="diagram-shared-state">{t("common.loading")}</div>;
  return <div className="diagram-shared"><header><strong>{diagram.title}</strong><span>{t("diagrams.version", { n: diagram.version })}</span></header><div className="diagram-canvas"><Excalidraw viewModeEnabled initialData={{ elements: diagram.scene.elements as any, appState: { ...diagram.scene.appState, collaborators: new Map() } as any, files: diagram.scene.files as any }} /></div></div>;
}
