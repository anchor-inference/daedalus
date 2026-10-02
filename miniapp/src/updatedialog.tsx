// The update dialog: what the new desktop release is, and the one next step that gets it installed.
//
// The operator used to update the desktop application from a terminal. Everything here is a button
// or a link instead, and the command the launcher still reports is never drawn: a command on screen
// reads as "you have to type this", which is the thing the dialog exists to retire.

import { type ReactNode, useState } from "react";
import { t } from "./i18n";
import { Icon } from "./icons";
import { Sheet } from "./ui/dialogs";
import { relTimeLong } from "./format";
import { Row } from "./settingsrow";
import { checkLauncher, closeUpdate, downloadFraction, downloadProgress, failureText, openUpdate, releaseVersion, startDownload, startInstall, updateStage, useUpdates } from "./updates";

export function UpdateHost() {
  const { state, dialogOpen } = useUpdates();
  if (!dialogOpen || !state?.connected || !state.upgrade) return null;
  return <UpdateDialog />;
}

function UpdateDialog() {
  const { state } = useUpdates();
  // `closing` outlives the state: once the install has started the window is about to go, and a poll
  // that still reports "ready" must not put the button back under the operator's pointer.
  const [busy, setBusy] = useState<"" | "download" | "install">("");
  const [closing, setClosing] = useState(false);
  const [error, setError] = useState("");
  if (!state?.upgrade) return null;
  const upgrade = state.upgrade;
  const stage = updateStage(state);
  const download = state.download;
  const next = releaseVersion(upgrade.to);
  const current = state.version || releaseVersion(upgrade.from);

  const begin = async () => {
    setBusy("download");
    setError("");
    try {
      await startDownload();
    } catch (e) {
      setError(failureText(e));
    } finally {
      setBusy("");
    }
  };
  const install = async () => {
    setBusy("install");
    setError("");
    try {
      await startInstall();
      setClosing(true);
    } catch (e) {
      setError(failureText(e));
    } finally {
      setBusy("");
    }
  };

  const notes = (
    <a className="update-notes" href={upgrade.url} target="_blank" rel="noreferrer">
      {t("update.notes")}
      <Icon name="external" size={14} />
    </a>
  );
  let body: ReactNode;
  let action: ReactNode = null;
  if (closing) {
    body = <div className="update-closing" role="status"><span className="update-spinner" aria-hidden />{t("update.closing")}</div>;
  } else if (stage === "release") {
    body = <p className="update-text">{t(upgrade.package === "appimage" ? "update.release.appimage" : "update.release.package")}</p>;
    action = <a className="btn primary" href={upgrade.url} target="_blank" rel="noreferrer" data-update-action="release">{t("update.release.open")}</a>;
  } else if (stage === "elsewhere") {
    body = <p className="update-text">{t("update.elsewhere")}</p>;
    action = <a className="btn" href={upgrade.url} target="_blank" rel="noreferrer" data-update-action="release">{t("update.release.open")}</a>;
  } else if (stage === "ready" || stage === "direct") {
    body = <p className="update-text">{t(stage === "direct" ? "update.direct" : "update.ready")}</p>;
    action = <button className="btn primary" disabled={!!busy} onClick={() => void install()} data-update-action="install">{t(busy === "install" ? "update.installing" : "update.install")}</button>;
  } else if (stage === "downloading") {
    const fraction = downloadFraction(download);
    body = (
      <div className="update-progress" role="status">
        <span className="update-bar" role="progressbar" aria-label={t("update.downloading")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(fraction * 100)}>
          <i style={{ width: `${(fraction * 100).toFixed(1)}%` }} />
        </span>
        <span className="update-bytes" data-update-bytes>{download && download.total > 0 ? downloadProgress(download.done, download.total) : t("update.downloading")}</span>
      </div>
    );
  } else if (stage === "failed") {
    body = <p className="update-text update-error" role="alert">{download?.error || t("update.failed.generic")}</p>;
    action = <button className="btn primary" disabled={!!busy} onClick={() => void begin()} data-update-action="retry">{t("update.retry")}</button>;
  } else {
    action = <button className="btn primary" disabled={!!busy} onClick={() => void begin()} data-update-action="download">{t("update.download")}</button>;
  }

  return (
    <Sheet title={t("update.title", { version: next })} size="narrow" className="update-sheet" onClose={closeUpdate}>
      <div className="update-head">
        <span className="update-current">{t("update.current", { version: current })}</span>
        {notes}
      </div>
      {body}
      {error && <p className="update-text update-error" role="alert">{error}</p>}
      {!closing && stage !== "release" && stage !== "elsewhere" && <p className="update-promise">{t("update.promise")}</p>}
      {action && <div className="sheet-foot">{action}</div>}
    </Sheet>
  );
}

/** Settings → About's card for the desktop application: its version, whether a newer one exists, and
 *  the two buttons that act on it. Without a launcher beside the server there is no application to
 *  speak of, and the card says so in one row rather than disappearing, so its absence is explained. */
export function DesktopAppCard({ toast }: { toast: (text: string) => void }) {
  const { state, checking } = useUpdates();
  const check = async () => {
    try {
      await checkLauncher();
    } catch (e) {
      toast(failureText(e));
    }
  };
  const value = (text: string) => <span className="settings-value mono" data-app-version>{text}</span>;
  let status = "";
  if (state?.connected) {
    if (state.error) status = t("update.status.error", { error: state.error });
    else if (state.upgrade) status = t("update.status.available", { version: releaseVersion(state.upgrade.to) });
    else if (state.checked_at) status = t("update.status.latest");
    else status = t("update.status.never");
    if (state.checked_at) status = `${status} · ${t("update.status.checked", { when: relTimeLong(state.checked_at) })}`;
  }
  return (
    <div className="card" data-desktop-app>
      <div className="section-title" style={{ marginTop: 0 }}>{t("update.card")}</div>
      <Row title={t("update.version")}>{value(state?.connected ? state.version || "—" : t("update.notconnected"))}</Row>
      {state?.connected && (
        <Row title={t("update.updates")} desc={<span data-update-status>{status}</span>}>
          <span className="update-row-actions">
            <button className="btn small" disabled={checking} onClick={() => void check()} data-update-check>{t(checking ? "update.checking" : "update.check")}</button>
            {state.upgrade && <button className="btn small primary" onClick={openUpdate} data-update-open>{t("update.open")}</button>}
          </span>
        </Row>
      )}
    </div>
  );
}
