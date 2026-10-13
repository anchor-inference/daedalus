// What the views share about structured entries: the types and groups read once for the screen, the
// one queue of ticks for the device, a type's icon, a status as a word and a colour, a progress pill.

import { useEffect, useState, useSyncExternalStore, type CSSProperties } from "react";
import { t } from "../i18n";
import { Icon, ICON_NAMES, type IconName } from "../icons";
import { prime, useQuery } from "../store";
import type { EventStatus, Group, Progress, TypeDef } from "./blocks";
import { GROUPS, TYPES, eventKey, patchBlocks, refreshPlanner } from "./data";
import { PatchQueue } from "./patchqueue";

export function useTypes(): TypeDef[] {
  return useQuery<{ types: TypeDef[] }>(TYPES, { staleMs: 60000 }).data?.types ?? [];
}

export function useGroups(): Group[] {
  return useQuery<{ groups: Group[] }>(GROUPS, { pollMs: 60000, staleMs: 10000 }).data?.groups ?? [];
}

/** The device's one queue of ticks. The host's answer replaces the open event at once (were it read
 *  again instead, the tick would blink off between leaving the queue and the read coming back), and
 *  the lists are read again, so a chip's "12/47" follows. */
export const ticks = new PatchQueue(async (eventId, ops) => {
  const saved = await patchBlocks(eventId, ops);
  if (saved && typeof saved === "object" && Array.isArray(saved.blocks)) prime(eventKey(eventId), saved);
  refreshPlanner();
});

// What waits is tried again when the browser says the network is back, and every twenty seconds
// while anything waits, because a host that restarted says nothing to the browser.
if (typeof window !== "undefined") {
  window.addEventListener("online", () => { void ticks.flush(); });
  window.setInterval(() => { if (ticks.size) void ticks.flush(); }, 20000);
}

export function useQueued(): number {
  return useSyncExternalStore((fire) => ticks.subscribe(fire), () => ticks.size, () => ticks.size);
}

/** A type's icon: one of the app's icons by name, or the emoji the type was given, or a dot. */
export function TypeIcon({ icon, color, size = 16, className = "" }: { icon?: string; color?: string; size?: number; className?: string }) {
  const style = { "--c": color ?? "var(--fg-3)" } as CSSProperties;
  if (icon && (ICON_NAMES as string[]).includes(icon)) return <span className={`cal-ticon ${className}`} style={style} aria-hidden="true"><Icon name={icon as IconName} size={size} /></span>;
  if (icon && [...icon].length <= 2 && !/^[\w-]+$/.test(icon)) return <span className={`cal-ticon emoji ${className}`} style={{ ...style, fontSize: size }} aria-hidden="true">{icon}</span>;
  return <span className={`cal-ticon dot ${className}`} style={style} aria-hidden="true" />;
}

export function statusLabel(status: EventStatus | undefined): string {
  return t(`cal.status.${status ?? "planned"}`);
}

export function statusIcon(status: EventStatus | undefined): IconName {
  return status === "done" ? "check" : status === "skipped" ? "forward" : status === "cancelled" ? "close" : "clock";
}

/** "12/47" with a thin bar under it; the whole pill turns the success colour once everything is done. */
export function ProgressPill({ progress, className = "" }: { progress: Progress | null | undefined; className?: string }) {
  if (!progress || !progress.total) return null;
  const done = progress.done >= progress.total;
  return (
    <span className={`cal-progress ${done ? "complete" : ""} ${className}`} title={t("cal.progress.named", { done: progress.done, total: progress.total })}>
      <span className="cal-progress-text">{done && <Icon name="check" size={11} />}{progress.done}/{progress.total}</span>
      <span className="cal-progress-bar" aria-hidden="true"><span style={{ width: `${Math.round((progress.done / progress.total) * 100)}%` }} /></span>
    </span>
  );
}

/** A width that follows the element: whether a table is drawn as cards depends on the room the sheet
 *  gives it, not on the window. */
export function useWidth(element: HTMLElement | null): number {
  const [width, setWidth] = useState(() => element?.clientWidth ?? 0);
  useEffect(() => {
    if (!element) return;
    setWidth(element.clientWidth);
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, [element]);
  return width;
}

const COLLAPSED = "daedalus.calendar.collapsed";

function readCollapsed(): Record<string, boolean> {
  try { return JSON.parse(localStorage.getItem(COLLAPSED) || "{}") as Record<string, boolean>; } catch { return {}; }
}

/** Whether a block is folded, remembered on this device by the block's id: the reader folds the
 *  recipe they know by heart once, not every time the meal opens. Unremembered, the block's own
 *  `collapsed` decides. */
export function useCollapsed(): [(id: string, fallback?: boolean) => boolean, (id: string, folded: boolean) => void] {
  const [map, setMap] = useState<Record<string, boolean>>(readCollapsed);
  const get = (id: string, fallback = false) => (id in map ? map[id] : fallback);
  const set = (id: string, folded: boolean) => {
    setMap((cur) => {
      const next = { ...cur, [id]: folded };
      // The newest five hundred: a device that opens a plan a week for a year keeps no more than that.
      const keys = Object.keys(next);
      const kept = keys.length > 500 ? Object.fromEntries(keys.slice(-500).map((k) => [k, next[k]])) : next;
      try { localStorage.setItem(COLLAPSED, JSON.stringify(kept)); } catch { /* the fold lasts the visit */ }
      return kept;
    });
  };
  return [get, set];
}
