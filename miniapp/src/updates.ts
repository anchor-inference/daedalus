// The desktop application's own updates, as the launcher beside the server reports them.
//
// One state for the whole page: the rail's button, the update dialog and Settings → About read the
// same answer, so a check started in Settings lights the rail and a download started in the dialog
// shows its progress in Settings as well. Each of them polling on its own would have asked the
// launcher three times over and could show three different answers for a few seconds.

import { useSyncExternalStore } from "react";
import { ApiError, api } from "./api";
import { t } from "./i18n";

export type LauncherDownload = { state: "idle" | "running" | "done" | "failed"; done: number; total: number; error: string; ready: boolean };
export type LauncherUpgrade = { from: string; to: string; url: string; command?: string; package: string };
export type LauncherState = {
  connected: boolean;
  version?: string;
  upgrade?: LauncherUpgrade | null;
  installable?: boolean;
  checked_at?: string;
  error?: string;
  download?: LauncherDownload;
  /** The launcher predates downloading ahead (0.15.1 and older): its upgrade fetches the release
   *  itself after the window has closed, so the dialog offers to install straight away. */
  direct?: boolean;
};

/** What the dialog offers, derived from the launcher's answer alone so it can be tested without a page.
 *
 *  - `none`: nothing to offer (no launcher, or up to date);
 *  - `release`: this copy cannot replace itself (an AppImage or a system package): the new installer is
 *    downloaded from the release page;
 *  - `elsewhere`: the update is installed from the desktop window on the computer the agent runs on,
 *    not from this browser;
 *  - `direct`: a launcher too old to download ahead, which installs in one step;
 *  - `download`, `downloading`, `ready`, `failed`: the in-app flow. */
export type UpdateStage = "none" | "release" | "elsewhere" | "direct" | "download" | "downloading" | "ready" | "failed";

export function updateStage(state: LauncherState | null): UpdateStage {
  if (!state?.connected || !state.upgrade) return "none";
  if (state.upgrade.package) return "release";
  if (!state.installable) return "elsewhere";
  if (state.direct) return "direct";
  const download = state.download;
  if (download?.ready) return "ready";
  if (download?.state === "running") return "downloading";
  if (download?.state === "failed") return "failed";
  return "download";
}

/** A release tag as a person reads it: "desktop-v0.15.2" is "0.15.2". */
export function releaseVersion(tag: string | undefined | null): string {
  return (tag ?? "").replace(/^desktop-v/, "").replace(/^v/, "");
}

/** "120 / 222 MB": both sides in whole megabytes, the unit once, since the two always share it. */
export function downloadProgress(done: number, total: number): string {
  const mb = (n: number) => Math.round(Math.max(0, n) / (1 << 20));
  return t("update.progress", { done: mb(done), total: mb(total) });
}

/** The share of the download that has arrived, 0 to 1; 0 while the size is not known yet. */
export function downloadFraction(download: LauncherDownload | undefined): number {
  if (!download || download.total <= 0) return 0;
  return Math.min(1, Math.max(0, download.done / download.total));
}

const RECHECK_MS = 30 * 60 * 1000;
const PROGRESS_MS = 1000;

let state: LauncherState | null = null;
let dialogOpen = false;
let checking = false;
const listeners = new Set<() => void>();
let snapshot: { state: LauncherState | null; dialogOpen: boolean; checking: boolean } = { state, dialogOpen, checking };

function emit(): void {
  snapshot = { state, dialogOpen, checking };
  for (const listener of listeners) listener();
}

let progressTimer: number | undefined;

/** Takes an answer in; while a download runs, asks again in a second so the bar moves without anyone
 *  holding a timer of their own. */
function accept(next: LauncherState, fromWrite = false): void {
  // A server answering with something else must not wipe what is known. After a write the state is
  // read once more; a read is never retried from here, or a bad answer would ask again in a loop.
  if (typeof next?.connected !== "boolean") {
    if (fromWrite) void refreshLauncher();
    return;
  }
  state = next;
  window.clearTimeout(progressTimer);
  if (next.download?.state === "running") progressTimer = window.setTimeout(() => void refreshLauncher(), PROGRESS_MS);
  emit();
}

export async function refreshLauncher(): Promise<void> {
  try {
    accept(await api.get<LauncherState>("/api/system/launcher"));
  } catch {
    // An older server without the route, or one that is restarting: the last answer stands, and the
    // next focus or the half-hourly check asks again.
  }
}

/** Asks the launcher to look for a release now. Throws the server's reason so the caller can show it. */
export async function checkLauncher(): Promise<void> {
  checking = true;
  emit();
  try {
    accept(await api.post<LauncherState>("/api/system/launcher/check"), true);
  } finally {
    checking = false;
    emit();
  }
}

/** Starts the background download and verification; the progress arrives through the state. */
export async function startDownload(): Promise<void> {
  accept(await api.post<LauncherState>("/api/system/launcher/download"), true);
}

/** Hands the install to the launcher. The desktop window closes by itself within seconds after this. */
export async function startInstall(): Promise<void> {
  await api.post<{ started: boolean }>("/api/system/launcher/install");
}

/** The reason a refusal gives, or a generic sentence when it gave none a person can read. */
export function failureText(error: unknown): string {
  if (error instanceof ApiError && error.message) return error.message;
  return t("update.failed.generic");
}

export function openUpdate(): void {
  dialogOpen = true;
  emit();
}

export function closeUpdate(): void {
  dialogOpen = false;
  emit();
}

let started = false;

/** The first reader starts the checks: once now, every half hour, and whenever the window comes back,
 *  which is when an operator who left it open overnight looks at it again. */
function start(): void {
  if (started) return;
  started = true;
  void refreshLauncher();
  window.setInterval(() => void refreshLauncher(), RECHECK_MS);
  window.addEventListener("focus", () => void refreshLauncher());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  start();
  return () => listeners.delete(listener);
}

export function useUpdates(): { state: LauncherState | null; dialogOpen: boolean; checking: boolean } {
  return useSyncExternalStore(subscribe, () => snapshot, () => snapshot);
}
