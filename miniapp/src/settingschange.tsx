// The card under an answer whose turn changed Daedalus's settings: what changed, from what to what,
// when it takes effect, and — while the change waits for the operator — Apply and Reject.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import { useEvent } from "./events";
import { t } from "./i18n";
import { Icon } from "./icons";
import type { Activity, ToolItem } from "./turns";
import { errorText } from "./ui";

export type SettingsChangeStatus = "pending" | "applied" | "rejected" | "expired" | "stale" | "failed" | "withdrawn";
export type SettingsChangeLine = { path: string; old: unknown; new: unknown; apply_at: string; kind: "auto" | "approval" | "never" };
export type SettingsChangeView = {
  id: string;
  session_id: string;
  reason: string;
  changes: SettingsChangeLine[];
  kind: "auto" | "approval";
  status: SettingsChangeStatus;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  via: string;
  error: string;
};

/** The tool's answer names the change on its first line; the card is drawn from that id alone. */
const CHANGE_ID_RE = /^Settings change: (sc[0-9a-f]{6,16})\b/;

/** The settings changes a turn made, in order: the steps that answered with a change's id. */
export function settingsChangeIds(activity: Activity[]): string[] {
  const ids: string[] = [];
  for (const item of activity) {
    if (item.kind !== "tool" || item.name !== "SettingsChange" || item.running || item.error) continue;
    const id = CHANGE_ID_RE.exec((item as ToolItem).result ?? "")?.[1];
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** A value as the operator reads it: words for nothing and for a switch, JSON for a list or a map. */
export function shownValue(value: unknown): string {
  if (value === null || value === undefined) return t("setchange.unset");
  if (typeof value === "boolean") return t(value ? "setchange.on" : "setchange.off");
  if (typeof value === "string") return value === "" ? t("setchange.empty") : value;
  if (typeof value === "number") return String(value);
  return JSON.stringify(value);
}

/** When the change takes effect, said once for the card: the latest of its lines wins. */
export function applyAt(lines: SettingsChangeLine[]): string {
  const order = ["next_step", "reconnect", "restart"];
  const latest = lines.reduce((worst, line) => (order.indexOf(line.apply_at) > order.indexOf(worst) ? line.apply_at : worst), "next_step");
  return t(`setchange.at.${order.includes(latest) ? latest : "next_step"}`);
}

export function SettingsChangeCard({ changeId }: { changeId: string }) {
  const [view, setView] = useState<SettingsChangeView | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setView(await api.get<SettingsChangeView>(`/api/settings/changes/${encodeURIComponent(changeId)}`));
    } catch (e) {
      // A change the host no longer keeps (forgotten after its month) is said as gone, not as an error.
      if (e instanceof ApiError && e.status === 404) setView(null);
      else setFailed(errorText(e));
    }
  }, [changeId]);
  useEffect(() => {
    void load();
  }, [load]);
  // Answered from the notification, a push or another window: the card learns it from the entry's
  // resolution, which names the change by its request.
  useEvent(["notify.resolved"], (event) => {
    const ref = String((event.payload as { request_ref?: unknown })?.request_ref ?? "");
    if (ref.startsWith("settings:") && ref.endsWith(`:${changeId}`)) void load();
  }, [changeId, load]);
  async function decide(action: "apply" | "reject") {
    setBusy(true);
    setFailed(null);
    try {
      setView(await api.post<SettingsChangeView>(`/api/settings/changes/${encodeURIComponent(changeId)}/${action}`));
    } catch (e) {
      // Answered elsewhere first: the host says how, with the change as it now stands.
      if (e instanceof ApiError && e.status === 409 && typeof e.data.status === "string") setView(e.data as unknown as SettingsChangeView);
      else setFailed(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  if (view === undefined && !failed) return null;
  if (!view) {
    return (
      <div className="setchange gone" data-change={changeId}>
        <div className="setchange-head">
          <Icon name="settings" size={16} />
          <span className="setchange-title">{t("setchange.title")}</span>
        </div>
        <div className="setchange-note">{failed ? t("setchange.failed.load", { error: failed }) : t("setchange.gone")}</div>
      </div>
    );
  }
  const pending = view.status === "pending";
  return (
    <div className={`setchange ${view.status}`} data-change={view.id} data-status={view.status}>
      <div className="setchange-head">
        <Icon name="settings" size={16} />
        <span className="setchange-title">{t(view.kind === "auto" ? "setchange.title.auto" : "setchange.title")}</span>
        <span className={`pill ${pillClass(view.status)}`}>{t(`setchange.status.${view.status}`)}</span>
      </div>
      {view.reason && <div className="setchange-reason">{view.reason}</div>}
      <ul className="setchange-lines">
        {view.changes.map((line) => (
          <li key={line.path} className="setchange-line">
            <code className="setchange-path">{line.path}</code>
            <span className="setchange-values">
              <span className="setchange-old">{shownValue(line.old)}</span>
              <span className="setchange-arrow" aria-hidden="true">→</span>
              <span className="setchange-new">{shownValue(line.new)}</span>
            </span>
          </li>
        ))}
      </ul>
      <div className="setchange-foot">
        <span className="setchange-when">{statusLine(view)}</span>
        {pending && (
          <span className="setchange-actions">
            <button type="button" className="btn small" data-action="reject" disabled={busy} onClick={() => void decide("reject")}>{t("setchange.reject")}</button>
            <button type="button" className="btn small primary" data-action="apply" disabled={busy} onClick={() => void decide("apply")}>{t("setchange.apply")}</button>
          </span>
        )}
      </div>
      {failed && <div className="setchange-note error">{failed}</div>}
    </div>
  );
}

function pillClass(status: SettingsChangeStatus): string {
  if (status === "applied") return "done";
  if (status === "pending") return "pending";
  if (status === "rejected") return "rejected";
  if (status === "failed") return "failed";
  return "closed";
}

function statusLine(view: SettingsChangeView): string {
  const when = applyAt(view.changes);
  if (view.status === "pending") return t("setchange.when.pending", { when });
  if (view.status === "applied") return t("setchange.when.applied", { when });
  if (view.status === "failed") return t("setchange.when.failed", { error: view.error || "?" });
  return t(`setchange.when.${view.status}`);
}
