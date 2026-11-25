// Where a floating window stands: its size from the screen's, its first place above the composer on
// the right, and the place the viewer last left it, kept inside the screen. Plain functions, so the
// rules are tested without a document.

export type Point = { x: number; y: number };
export type Size = { width: number; height: number };

/** How far the window keeps from every edge of the screen. */
export const EDGE = 16;

/**
 * The room a resting composer takes at the bottom of the chat. The window first stands above it, and
 * minimised it waits there: in the corner itself it covered the chat's Send button, which is the very
 * thing pressed after the prompt is inserted.
 */
export const COMPOSER_CLEARANCE = 132;

/** The window's size on a desktop: room for a question card and a few turns, never more than the screen. */
export function windowSize(viewport: Size): Size {
  return { width: Math.min(460, viewport.width - 2 * EDGE), height: Math.max(240, Math.min(620, viewport.height - EDGE - COMPOSER_CLEARANCE)) };
}

/** Where the window first stands: on the right, above the composer. */
export function defaultPosition(size: Size, viewport: Size): Point {
  return clampPosition({ x: viewport.width - size.width - EDGE * 1.5, y: viewport.height - size.height - COMPOSER_CLEARANCE }, size, viewport);
}

/**
 * The position moved back inside the screen. A window dragged half off, or remembered from a larger
 * screen, would otherwise keep its title bar — the only way to move it back — out of reach.
 */
export function clampPosition(at: Point, size: Size, viewport: Size): Point {
  const maxX = Math.max(EDGE, viewport.width - size.width - EDGE);
  const maxY = Math.max(EDGE, viewport.height - size.height - EDGE);
  return { x: Math.round(Math.min(Math.max(at.x, EDGE), maxX)), y: Math.round(Math.min(Math.max(at.y, EDGE), maxY)) };
}

/** The position this viewer last left the window at, or null; storage that throws or holds junk is no position.
 *  Each kind of window keeps its own, so two open at once do not stand on top of each other. */
export function readPosition(key: string): Point | null {
  try {
    const raw = JSON.parse(window.localStorage.getItem(key) ?? "null") as unknown;
    if (raw && typeof raw === "object" && Number.isFinite((raw as Point).x) && Number.isFinite((raw as Point).y)) return { x: (raw as Point).x, y: (raw as Point).y };
  } catch {
    /* a private window or blocked storage: the corner it is */
  }
  return null;
}

export function writePosition(at: Point, key: string): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(at));
  } catch {
    /* remembered for this visit only */
  }
}
