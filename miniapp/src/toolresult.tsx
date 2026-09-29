// One tool result under its step: the short preview the listing carries, and the whole of it on request.

import { useState } from "react";
import { api } from "./api";
import { copyText, fmtInt } from "./components";
import { t } from "./i18n";
import type { ToolItem } from "./turns";
import { errorText } from "./ui";

/** The whole of a tool result as the endpoint returns it: the text, and whether that is all of it. */
export type FullResult = { text: string; complete: boolean };

/** Offers the text as a file: a result of a hundred thousand characters is easier kept than read here. */
function saveText(text: string, name: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name.replace(/[^\w.-]+/g, "_");
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/**
 * A tool result with its "show all". ``initial`` and ``keep`` let the owner hold the fetched text
 * across the row being unmounted, which a windowed chat does to every row scrolled out of view.
 */
export function ToolResultView({ sessionId, item, toast, initial, keep }: { sessionId: string; item: ToolItem; toast: (text: string) => void; initial: FullResult | null; keep: (full: FullResult) => void }) {
  const [full, setFull] = useState<FullResult | null>(initial);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  // The host has not saved this step yet (its run is still going): not a failure, a wait.
  const [pending, setPending] = useState(false);
  const text = full?.text ?? item.result ?? "";
  // Whether there is more of it is the server's answer, not a comparison of lengths: what is on
  // screen is a redacted preview and the length is the text's, so a redaction that shortens the
  // preview would otherwise offer to fetch a result that is already whole. The length may be
  // unknown: a result compaction masked is measured only once its original is read back.
  const clipped = full === null && !!item.clipped;
  async function loadAll() {
    setLoading(true);
    setFailed(null);
    setPending(false);
    try {
      const r = await api.get<{ content?: unknown; complete?: boolean; pending?: boolean }>(`/api/sessions/${sessionId}/tool-results/${encodeURIComponent(item.id)}`);
      if (r.pending) {
        setPending(true);
        return;
      }
      // An answer without the text is a failure to say, not a chat to take down with it.
      if (typeof r.content !== "string") throw new Error(t("session.result.empty"));
      const whole = { text: r.content, complete: r.complete !== false };
      keep(whole);
      setFull(whole);
    } catch (e) {
      // A failed fetch used to be taken as the whole text: the preview was kept as "full", the
      // button went away, and a cut result looked complete. The preview stays a preview now.
      setFailed(errorText(e));
    } finally {
      setLoading(false);
    }
  }
  async function copyAll() {
    toast(t((await copyText(text)) ? "common.copied" : "session.result.copy.failed"));
  }
  const approval = item.error ? /Approval key: ([0-9a-f]{12})/.exec(text) : null;
  const [granted, setGranted] = useState(false);
  async function allowOnce() {
    if (!approval) return;
    try {
      await api.post(`/api/sessions/${sessionId}/policy/grant`, { key: approval[1] });
      setGranted(true);
    } catch {
      setGranted(false);
    }
  }
  return (
    <>
      {/* Above the text, not under it: under a whole result of fifty thousand characters the copy
          and download were a full scroll away from the button that had just opened it. */}
      {full !== null && (
        <div className="result-tools">
          <span className="num">{t("session.result.size", { n: fmtInt(full.text.length) })}</span>
          <button type="button" className="btn small" onClick={copyAll}>{t("session.result.copy")}</button>
          <button type="button" className="btn small" onClick={() => saveText(text, `${item.name || "result"}-${item.id}.txt`)}>{t("common.download")}</button>
        </div>
      )}
      {/* Focusable once whole, so a long result can be scrolled to its end from the keyboard too. */}
      <pre className={`result ${item.error ? "error" : ""} ${full !== null ? "full" : ""}`} tabIndex={full !== null ? 0 : undefined}>{text}</pre>
      {approval && (
        <button type="button" className="btn small" onClick={allowOnce} disabled={granted} title={t("session.allow.title")}>
          {t(granted ? "session.allowed.once" : "session.allow.once", { key: approval[1] })}
        </button>
      )}
      {clipped && (
        <button type="button" className="btn small" onClick={loadAll} disabled={loading}>
          {loading ? t("common.loading") : pending ? t("common.retry") : item.length != null ? t("session.showall", { n: fmtInt(item.length) }) : t("session.showall.masked")}
        </button>
      )}
      {pending && <div className="result-note">{t("session.result.pending")}</div>}
      {failed && <div className="result-note error">{t("session.result.failed", { error: failed })}</div>}
      {full !== null && !full.complete && <div className="result-note">{t("session.result.partial")}</div>}
    </>
  );
}
