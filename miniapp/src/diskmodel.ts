// What the disk-usage panel decides from the server's measurement, kept apart from the drawing so
// the rules (what is preselected, what can be ticked, how a size reads when it is only a floor)
// can be tested without a DOM.

import { t } from "./i18n";

export type DiskEntry = {
  path: string; bytes: number; files: number; newest: string | null;
  throwaway: boolean; tracked: boolean; protected: boolean; children: DiskEntry[];
};
/** `limit_bytes` is the limit in force for this workspace, 0 for none; `own_limit` says whether the
 *  operator set it for this workspace or it is the default every other workspace follows. */
export type WorkspaceDetail = {
  name: string; label: string; bytes: number; truncated: boolean; limit_bytes: number; own_limit: boolean;
  over: 0 | 1 | 2; entries: DiskEntry[];
};
export type DiskDetail = {
  default_limit_bytes: number; free_bytes: number; total_bytes: number; low_disk: boolean;
  workspaces: WorkspaceDetail[]; outside: { label: string; env: string }[];
};
export type LimitResult = { name: string; limit_bytes: number; own_limit: boolean; default_limit_bytes: number; over: 0 | 1 | 2 };
export type CleanupResult = {
  freed_bytes: number; removed: string[]; refused: { path: string; reason: string }[]; bytes: number;
};

const UNITS = ["fmt.bytes.b", "fmt.bytes.kb", "fmt.bytes.mb", "fmt.bytes.gb", "fmt.bytes.tb"] as const;

/**
 * A size in decimal units, the way the limit is written in the settings and the way the host's own
 * notices say it. The rest of the app counts in powers of 1024 under the same letters, which showed a
 * 20 GB limit as "18.6 GB" next to a size read off a disk; here a gigabyte is 10^9 bytes, and a round
 * limit reads round ("20 GB", not "20.0 GB").
 */
export function diskBytes(n: number): string {
  let value = Math.max(0, n);
  let step = 0;
  // 999.5 rounds up to 1000 in the unit below, which must be said in the unit above.
  while (step < UNITS.length - 1 && value >= 999.5) { value /= 1000; step++; }
  const text = step === 0 || value >= 99.95 ? String(Math.round(value)) : value.toFixed(1).replace(/\.0$/, "");
  return t(UNITS[step], { n: text });
}

/** A limit as a person reads it: a size, or that there is none. */
export function limitText(limit: number): string {
  return limit > 0 ? diskBytes(limit) : t("disk.noLimit");
}

/** A size that the walk gave up on is a floor, not a number: "at least 23 GB". */
export function sizeText(n: number, truncated: boolean): string {
  return truncated ? t("disk.atLeast", { size: diskBytes(n) }) : diskBytes(n);
}

export type LimitChoice = { id: string; label: string; gb: number };

const GB = 1e9;
/** The largest limit the host accepts, in GB. */
export const MOST_LIMIT_GB = 1_000_000;
const tidy = (gb: number) => Math.round(gb * 1000) / 1000;

/** The quick ways to change a limit: ten more, twice as much, two round ones. Relative ones need a limit
 *  to be relative to; one that would land on the limit already in force, or on another choice, is left out. */
export function limitChoices(limit: number): LimitChoice[] {
  const current = tidy(limit / GB);
  const out: LimitChoice[] = [];
  const add = (id: string, label: string, gb: number) => {
    const value = tidy(gb);
    if (value > 0 && value <= MOST_LIMIT_GB && value !== current && !out.some((choice) => choice.gb === value)) out.push({ id, label, gb: value });
  };
  if (limit > 0) {
    add("plus", t("disk.limit.plus", { n: 10 }), current + 10);
    add("double", t("disk.limit.times", { n: 2 }), current * 2);
  }
  add("g50", t("disk.limit.gb", { n: 50 }), 50);
  add("g100", t("disk.limit.gb", { n: 100 }), 100);
  return out;
}

/** What was typed as a limit in GB: a positive number the host accepts, or null. Zero is the "No limit"
 *  choice, never a number typed by accident. */
export function parseLimitGb(text: string): number | null {
  if (!text.trim()) return null;
  const value = Number(text.replace(",", "."));
  return Number.isFinite(value) && value > 0 && value <= MOST_LIMIT_GB ? tidy(value) : null;
}

/** The workspace as the server left it after its limit changed, so the panel redraws without asking again. */
export function withLimit(detail: DiskDetail, result: LimitResult): DiskDetail {
  return {
    ...detail,
    default_limit_bytes: result.default_limit_bytes,
    workspaces: detail.workspaces.map((workspace) => workspace.name === result.name
      ? { ...workspace, limit_bytes: result.limit_bytes, own_limit: result.own_limit, over: result.over }
      : workspace),
  };
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
