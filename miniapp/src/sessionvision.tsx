// The vision model of one session: which model looks at the pictures this chat's tools are shown.
// It was only the global Settings → Models → Image understanding, so a chat about screenshots of small
// print could not have a stronger model read them without changing it for every other chat. Here the
// session picks its own, "default" first, naming the global model it stands for; the host keeps the
// choice apart from the session's model, so returning the model to the default leaves it be.

import { useEffect, useState } from "react";
import { api, type SessionDetail } from "./api";
import { t } from "./i18n";
import { ModelPicker, type ModelCatalogue } from "./modelitems";
import { WithEffort, inheritedDefault } from "./taskeffort";
import { errorText } from "./ui";

type VisionView = Pick<SessionDetail, "vision_preset" | "vision_label" | "vision_default" | "vision_default_label" | "vision_effort" | "vision_default_effort">;

/** What "default" means today, as the first row and the line's state say it. */
export function visionDefaultWord(view: VisionView): string {
  return view.vision_default ? t("details.vision.default", { name: view.vision_default_label || view.vision_default }) : t("details.vision.default.none");
}

/** The line's state in the Details list: the session's own model, or the default in quiet words. */
export function visionValue(view: VisionView): { text: string; own: boolean } {
  return view.vision_preset ? { text: view.vision_label || view.vision_preset, own: true } : { text: visionDefaultWord(view), own: false };
}

/** The page the line opens: the picker of image-capable models, saved on every change. */
export function SessionVisionModel({ sessionId, detail, toast, onSaved }: { sessionId: string; detail: VisionView; toast: (text: string) => void; onSaved?: () => void }) {
  const [view, setView] = useState<VisionView>(detail);
  const [catalogue, setCatalogue] = useState<ModelCatalogue | null>(null);
  const [busy, setBusy] = useState(false);
  // The detail is polled; a newer answer replaces what this page drew, and a save's answer comes first.
  useEffect(() => setView(detail), [detail.vision_preset, detail.vision_label, detail.vision_default, detail.vision_default_label, detail.vision_effort, detail.vision_default_effort]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    let current = true;
    // The models, their providers and prices come from the settings, as every model picker's rows do.
    api.get<ModelCatalogue>("/api/settings").then((s) => { if (current && s?.presets) setCatalogue(s); }).catch(() => undefined);
    return () => { current = false; };
  }, []);
  async function choose(change: { preset: string } | { effort: string }) {
    setBusy(true);
    try {
      setView(await api.post<VisionView>(`/api/sessions/${encodeURIComponent(sessionId)}/vision`, change));
      onSaved?.();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const value = view.vision_preset ?? "";
  // Only the models that see images are offered; the one in use stays listed so the row names it.
  return (
    <div className="dt-vision" data-setting="session.vision">
      <p className="dt-hint">{t("details.vision.sub")}</p>
      {catalogue ? (
        // The effort follows Settings → Models while the session has none of its own, and says so.
        <WithEffort id={`vision-effort-${sessionId}`} value={view.vision_effort} onChange={(effort) => void choose({ effort })} fallback={inheritedDefault(view.vision_default_effort)} disabled={busy}>
          <ModelPicker
            id={`vision-${sessionId}`}
            label={t("details.vision")}
            catalogue={catalogue}
            value={value}
            onChange={(preset) => void choose({ preset })}
            defaultOption={{ label: visionDefaultWord(view) }}
            keep={(id, preset) => !!preset.images || id === value}
            disabled={busy}
          />
        </WithEffort>
      ) : <div className="empty">…</div>}
    </div>
  );
}
