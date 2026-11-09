import { useEffect, useState } from "react";
import { api } from "../api";
import { relTimeLong } from "../format";
import { t } from "../i18n";
import { Board } from "./Board";
import { normalizeDoc, type SharedCanvasDoc } from "./model";
import { publicAttachmentUrl } from "./parts";
import { SnapshotFrame } from "./frames";
import "./canvas.css";

/** A canvas someone was sent a link to: read only, the latest version, pan and zoom and nothing else.
 *  A live page is shown as the still copy the board last took, never as a running page. */
export function SharedCanvas({ token }: { token: string }) {
  const [canvas, setCanvas] = useState<SharedCanvasDoc | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    api.get<SharedCanvasDoc>(`/api/public/canvases/${encodeURIComponent(token)}`).then((found) => { if (active) setCanvas({ ...found, doc: normalizeDoc(found.doc) }); }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [token]);
  useEffect(() => {
    if (canvas) document.title = canvas.title;
  }, [canvas]);
  if (failed) return <div className="cv-shared-state"><strong>{t("canvas.share.unavailable")}</strong><span>{t("canvas.share.unavailable.body")}</span></div>;
  if (!canvas) return <div className="cv-shared-state">{t("common.loading")}</div>;
  return (
    <div className="cv-shared">
      <header>
        <div className="cv-shared-title">
          <strong>{canvas.title}</strong>
          {canvas.updated_at && <span>{t("canvas.shared.updated", { time: relTimeLong(canvas.updated_at) })}</span>}
        </div>
        <span className="cv-shared-made">{t("canvas.shared.made")}</span>
      </header>
      <Board doc={canvas.doc} label={canvas.title} readOnly fileUrl={(fileId) => publicAttachmentUrl(token, fileId)} renderFrame={(node) => <SnapshotFrame node={node} token={token} capturedAt={canvas.snapshots?.[node.id]} />} />
    </div>
  );
}
