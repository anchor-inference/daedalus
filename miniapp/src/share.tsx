// Who can open this dialog from outside the app. The same three choices a hosted service has:
// only here, a private link, or anyone with the link. The page those links open is read-only.

import { useState } from "react";
import { api, ShareMode, ShareView } from "./api";
import { copyText } from "./ui/components";
import { Sheet } from "./ui/dialogs";
import { Icon } from "./icons";
import { t } from "./i18n";
import { confirmAsync, errorText } from "./ui";

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
      toast(t(next === "local" ? "session.share.mode.local" : next === "public" ? "session.share.mode.public" : rotate ? "session.share.mode.newkey" : "session.share.mode.key"));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function copy(text: string, what: "link" | "key") {
    toast((await copyText(text)) ? t(`svc.copied.${what}`) : t("svc.copyfail"));
  }

  return (
    <Sheet title={t("session.share.title")} onClose={onClose} size="narrow">
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
          <label className="field">{t("svc.link")}</label>
          <div className="share-field">
            <input className="field mono" readOnly value={share.url} onFocus={(e) => e.target.select()} aria-label={t("svc.sharelink")} />
            <button className="btn small" onClick={() => copy(share.url!, "link")}><Icon name="copy" size={13} /> {t("common.copy")}</button>
          </div>
          {mode === "key" && share.key && (
            <>
              <label className="field">{t("svc.key")}</label>
              <div className="share-field">
                <input className="field mono" readOnly value={share.key} onFocus={(e) => e.target.select()} aria-label={t("svc.sharekey")} />
                <button className="btn small" onClick={() => copy(share.key!, "key")}><Icon name="copy" size={13} /> {t("common.copy")}</button>
                <button className="btn small" disabled={busy} onClick={() => setMode("key", true)} title={t("svc.newkey.title")}>{t("svc.newkey")}</button>
              </div>
            </>
          )}
        </>
      )}
    </Sheet>
  );
}
