// The export of one conversation over an agent link: "Zip" saves the transcript, its data and the files still kept
// here; "To notes" puts the transcript into the notes of the chat that owns the conversation. One row, the same in
// the Agents tab and in the owner's view of a link, so the operator finds it in the same place either way.

import { useState } from "react";
import { LinkExportTarget, linkExportNote, linkExportZip } from "./api";
import { Icon } from "./icons";
import { lang, t } from "./i18n";
import { errorText } from "./ui";
import "./linkfiles.css";

/** Hands a blob to the browser as a download under `name`. */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  // A download reads the blob after the click returns; a minute is long past that.
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export function LinkExport({ target, toast }: { target: LinkExportTarget; toast: (text: string) => void }) {
  // One at a time: a second click while a large zip is still being built would build it twice.
  const [busy, setBusy] = useState<"" | "zip" | "note">("");
  // Said in the row, not in the short line: that line is for what went wrong (toastguard.test.ts).
  const [noted, setNoted] = useState(false);
  async function run(which: "zip" | "note") {
    if (busy) return;
    setBusy(which);
    setNoted(false);
    try {
      if (which === "zip") {
        const { blob, name } = await linkExportZip(target, lang());
        saveBlob(blob, name);
      } else {
        await linkExportNote(target, lang());
        setNoted(true);
      }
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy("");
    }
  }
  return (
    <div className="link-export" data-link-export={target.kind}>
      <span className="link-export-title">{t("link.export")}</span>
      <button type="button" className="btn small ghost" data-link-export-zip="" disabled={!!busy} aria-busy={busy === "zip"} title={t("link.export.zip.title")} onClick={() => void run("zip")}>
        <Icon name="download" size={14} />{t("link.export.zip")}
      </button>
      <button type="button" className="btn small ghost" data-link-export-note="" disabled={!!busy} aria-busy={busy === "note"} title={t("link.export.note.title")} onClick={() => void run("note")}>
        <Icon name="note" size={14} />{t("link.export.note")}
      </button>
      {noted && <span className="link-export-done" data-link-export-done="" role="status">{t("link.export.saved")}</span>}
    </div>
  );
}
