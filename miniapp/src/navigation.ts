// What the menu holds and how the keyboard moves through it, with no DOM in sight: the popover in
// navmenu.tsx renders these and a test can ask them questions without a browser.

import { SelfDevMode, visibleScreens } from "./capabilities";
import type { Screen } from "./router";

export const DESKTOP_PLACES: Screen[] = ["inbox", "board", "calendar", "diagrams", "changes", "terminals", "voice", "schedules"];

/** The destinations in the order the old rail listed them, one group per key of `nav.group.*`. */
export const GROUPS: { key: string; items: Screen[] }[] = [
  { key: "work", items: ["agents", "voice", "inbox", "board", "calendar", "diagrams", "terminals", "harnesses"] },
  { key: "autonomy", items: ["changes", "schedules", "services"] },
  { key: "knowledge", items: ["memory"] },
  { key: "observe", items: ["usage", "health"] },
];

/** The groups this installation really has: an empty one is not drawn. */
export function menuSections(selfdev: SelfDevMode): { key: string; items: Screen[] }[] {
  return GROUPS.map((g) => ({ key: g.key, items: visibleScreens(g.items, selfdev) })).filter((g) => g.items.length > 0);
}

/** Where focus goes from `index` of `count` items on a key, or null when the key is not one of ours. Arrows wrap. */
export function moveIndex(index: number, key: string, count: number): number | null {
  if (count === 0) return null;
  // Nothing focused yet: down starts at the top, up at the bottom.
  if (index < 0 && (key === "ArrowDown" || key === "ArrowUp")) return key === "ArrowDown" ? 0 : count - 1;
  if (key === "ArrowDown") return (index + 1) % count;
  if (key === "ArrowUp") return (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

export type Shortcut = "menu" | "sidebar" | "newchat" | null;

/** The Latin letter a key press stands for, whatever the layout. Comparing `key` alone left every
 * letter shortcut dead on a Russian layout, where Ctrl+K arrives as "л"; `code` names the key. */
export function latinKey(e: { key: string; code?: string }): string {
  return e.code && /^Key[A-Z]$/.test(e.code) ? e.code.slice(3).toLowerCase() : e.key.toLowerCase();
}

/** Ctrl/⌘ ⇧ M opens the menu, Ctrl/⌘ \ folds the sidebar, Ctrl/⌘ ⇧ O starts a new chat. Anywhere,
 *  a text field included: all three carry a modifier. Not Ctrl+N for the chat: the browser keeps
 *  that one for a new window and never hands it to the page. */
export function shortcutFor(e: { key: string; code?: string; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean; altKey: boolean }): Shortcut {
  if (!(e.metaKey || e.ctrlKey) || e.altKey) return null;
  if (e.shiftKey && latinKey(e) === "m") return "menu";
  if (e.shiftKey && latinKey(e) === "o") return "newchat";
  if (!e.shiftKey && e.key === "\\") return "sidebar";
  return null;
}
