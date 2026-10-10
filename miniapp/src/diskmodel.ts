// What the disk-usage panel decides from the server's measurement, kept apart from the drawing so
// the rules (what is preselected, what can be ticked, how a size reads when it is only a floor)
// can be tested without a DOM.

import { bytes } from "./format";
import { t } from "./i18n";

export type DiskEntry = {
  path: string; bytes: number; files: number; newest: string | null;
  throwaway: boolean; tracked: boolean; protected: boolean; children: DiskEntry[];
};
export type WorkspaceDetail = {
  name: string; label: string; bytes: number; truncated: boolean; over: 0 | 1 | 2; entries: DiskEntry[];
};
export type DiskDetail = {
  limit_bytes: number; free_bytes: number; total_bytes: number; low_disk: boolean;
  workspaces: WorkspaceDetail[]; outside: { label: string; env: string }[];
};
export type CleanupResult = {
  freed_bytes: number; removed: string[]; refused: { path: string; reason: string }[]; bytes: number;
};

/** A size that the walk gave up on is a floor, not a number: "at least 23.0 GB". */
export function sizeText(n: number, truncated: boolean): string {
  return truncated ? t("disk.atLeast", { size: bytes(n) }) : bytes(n);
}

/** The bar's tone: the server's own verdict on the limit, so the two never disagree. */
export function tone(over: 0 | 1 | 2): "" | "attn" | "bad" {
  return over === 2 ? "bad" : over === 1 ? "attn" : "";
}

/** How full the bar is. With no limit there is nothing to measure against, so the bar is not drawn. */
export function fillPercent(used: number, limit: number): number {
  if (limit <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((100 * used) / limit)));
}

/** Folders that exist to be thrown away start ticked; nothing else does, protected least of all. */
export function defaultSelection(entries: DiskEntry[]): Set<string> {
  return new Set(entries.filter((entry) => entry.throwaway && !entry.protected && !entry.tracked).map((entry) => entry.path));
}

/** A tracked folder holds work git knows about, so it is ticked only after the extra consent. */
export function canTick(entry: DiskEntry, includeTracked: boolean): boolean {
  return !entry.protected && (!entry.tracked || includeTracked);
}

/** Unticking the consent also unticks every tracked folder it had allowed. */
export function withoutTracked(selected: Set<string>, entries: DiskEntry[]): Set<string> {
  const tracked = new Set(entries.filter((entry) => entry.tracked).map((entry) => entry.path));
  return new Set([...selected].filter((path) => !tracked.has(path)));
}

export function selectedBytes(selected: Set<string>, entries: DiskEntry[]): number {
  return entries.filter((entry) => selected.has(entry.path)).reduce((sum, entry) => sum + entry.bytes, 0);
}

export function reasonKey(reason: string): string {
  return ["outside", "symlink", "missing", "protected", "tracked", "root"].includes(reason) ? `disk.refused.${reason}` : "disk.refused.other";
}
