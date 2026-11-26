// Who can open this dialog from outside the app. The same three choices a hosted service has:
// only here, a private link, or anyone with the link. The page those links open is read-only.
// Below them, the live link (livelink.tsx): a link that hands the conversation itself over.

import { useState } from "react";
import { api, ShareMode, ShareView } from "./api";
import { useCopied } from "./ui/components";
import { Sheet } from "./ui/dialogs";
import { Button } from "./ui/primitives";
import { t } from "./i18n";
import { confirmAsync, errorText } from "./ui";
import { LiveLinkSection } from "./livelink";

const MODES: ShareMode[] = ["local", "key", "public"];

export function ShareSheet({
  sessionId,
  share,
  onClose,
  onChanged,
  toast,
}: {
  sessionId: string;
  share?: ShareView;
  onClose: () => void;
  onChanged: (share: ShareView) => void;
  toast: (text: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const mode = share?.mode ?? "local";

  async function setMode(next: ShareMode, rotate = false) {
    if (next === "public" && mode !== "public" && !(await confirmAsync(t("session.share.public.title"), { body: t("session.share.public.body"), action: t("session.share.public.action") }))) return;
    setBusy(true);
    try {
      const updated = await api.post<ShareView>(`/api/sessions/${sessionId}/share`, { mode: next, rotate_key: rotate });
      onChanged(updated);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  const [copied, copyValue] = useCopied();
  async function copy(text: string, what: "link" | "key") {
    if (!(await copyValue(text, what))) toast(t("svc.copyfail"));
  }

  return (
    <Sheet title={t("session.share.title")} onClose={onClose} size="md">
      <div className="access-options" role="radiogroup" aria-label={t("session.share.title")}>
        {MODES.map((item) => (
          <label key={item} className={`access-option ${mode === item ? "on" : ""}`}>
            <input type="radio" name="dialog-access" checked={mode === item} disabled={busy} onChange={() => setMode(item)} />
            <span>
              <b>{t(`svc.access.${item}`)}</b>
              <span className="sub">{t(`session.share.${item}.sub`)}</span>
            </span>
          </label>
        ))}
      </div>
      <p className="sub share-note">{t("session.share.shows")}</p>
      {!share?.public_base && mode !== "local" && <div className="sub share-warn">{t("svc.nopublic")}</div>}
      {mode !== "local" && share?.url && (
        <>
          <label className="link-label" htmlFor="share-url">{t("svc.link")}</label>
          <div className="share-field">
            <input id="share-url" className="field mono" readOnly value={share.url} onFocus={(e) => e.target.select()} aria-label={t("svc.sharelink")} />
            <Button icon={copied === "link" ? "check" : "copy"} onClick={() => void copy(share.url!, "link")}>{t("common.copy")}</Button>
          </div>
          {mode === "key" && share.key && (
            <>
              <label className="link-label" htmlFor="share-key">{t("svc.key")}</label>
              <div className="share-field">
                <input id="share-key" className="field mono" readOnly value={share.key} onFocus={(e) => e.target.select()} aria-label={t("svc.sharekey")} />
                <Button icon={copied === "key" ? "check" : "copy"} onClick={() => void copy(share.key!, "key")}>{t("common.copy")}</Button>
                <Button disabled={busy} onClick={() => setMode("key", true)} title={t("svc.newkey.title")}>{t("svc.newkey")}</Button>
              </div>
            </>
          )}
        </>
      )}
      <LiveLinkSection sessionId={sessionId} toast={toast} />
    </Sheet>
  );
}
