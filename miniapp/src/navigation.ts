// How the keyboard moves through the More launcher, with no DOM in sight: navmenu.tsx measures its
// items and asks this where an arrow goes, and a test can ask the same without a browser.

/** An item's box on screen: left, top, width, height. */
export type Box = { x: number; y: number; w: number; h: number };

/** The search field above every item: what Up from the top row returns, and where Down starts. */
export const SEARCH = -1;

/** Where focus goes from `index` among `boxes` on a key: an item's index, `SEARCH`, or null when the
 *  key is not one of ours or there is nowhere to go.
 *
 *  By position rather than by a column count: the launcher's tiles are four across, its catalogue
 *  three (two in a narrow window, one on a phone) and its foot a row of its own, and a fixed grid
 *  sent Down from a tile to whatever item happened to share its index. Here Down goes to the nearest
 *  row below and, in it, to the item whose middle is closest; Right past a row's end carries on into
 *  the next row, the way reading does. */
export function moveInGrid(boxes: Box[], index: number, key: string): number | null {
  const count = boxes.length;
  if (count === 0) return null;
  if (index < 0) return key === "ArrowDown" ? 0 : null;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  const from = boxes[index];
  if (!from) return null;
  const mid = (b: Box) => ({ x: b.x + b.w / 2, y: b.y + b.h / 2 });
  const here = mid(from);
  const sameRow = (b: Box) => Math.abs(mid(b).y - here.y) < Math.min(b.h, from.h) / 2;
  if (key === "ArrowLeft" || key === "ArrowRight") {
    const sign = key === "ArrowRight" ? 1 : -1;
    let best: number | null = null;
    let distance = Infinity;
    boxes.forEach((b, i) => {
      if (i === index || !sameRow(b)) return;
      const d = (mid(b).x - here.x) * sign;
      if (d > 0 && d < distance) {
        distance = d;
        best = i;
      }
    });
    if (best !== null) return best;
    const next = index + sign;
    return next >= 0 && next < count ? next : null;
  }
  if (key === "ArrowUp" || key === "ArrowDown") {
    const sign = key === "ArrowDown" ? 1 : -1;
    const ahead = boxes.map((b, i) => ({ i, c: mid(b), b })).filter(({ c }) => (c.y - here.y) * sign > from.h / 2);
    if (!ahead.length) return sign < 0 ? SEARCH : null;
    const rowY = ahead.reduce((near, it) => (Math.abs(it.c.y - here.y) < Math.abs(near - here.y) ? it.c.y : near), ahead[0].c.y);
    const row = ahead.filter(({ c, b }) => Math.abs(c.y - rowY) < b.h / 2);
    return row.reduce((best, it) => (Math.abs(it.c.x - here.x) < Math.abs(best.c.x - here.x) ? it : best), row[0]).i;
  }
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
