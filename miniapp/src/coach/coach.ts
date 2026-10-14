// The prompt coach's window, the parts that are arithmetic and words rather than drawing: what the coach
// is told first, and where the floating window stands.

import { t } from "../i18n";

/** The coach as `GET/POST /api/sessions/<id>/coach` describes it. */
export type CoachResult = { title: string; prompt: string; path: string; at: string };
export type CoachView = { session_id: string; parent_id: string; finished: boolean; result: CoachResult | null; created?: boolean };

/**
 * The coach's first turn, in the reader's language: the coach answers in the language it is spoken to
 * in, and a draft alone, in whatever language the draft is, would leave that to chance. An empty draft
 * asks for help writing one, and the coach starts by asking what it is for.
 */
export function seedMessage(draft: string): string {
  const text = draft.trim();
  return text ? t("coach.seed", { draft: text }) : t("coach.seed.empty");
}

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

export const POSITION_KEY = "daedalus.coach.position";

/** The position this viewer last left the window at, or null; storage that throws or holds junk is no position.
 *  Each kind of window keeps its own, so two open at once do not stand on top of each other. */
export function readPosition(key = POSITION_KEY): Point | null {
  try {
    const raw = JSON.parse(window.localStorage.getItem(key) ?? "null") as unknown;
    if (raw && typeof raw === "object" && Number.isFinite((raw as Point).x) && Number.isFinite((raw as Point).y)) return { x: (raw as Point).x, y: (raw as Point).y };
  } catch {
    /* a private window or blocked storage: the corner it is */
  }
  return null;
}

export function writePosition(at: Point, key = POSITION_KEY): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(at));
  } catch {
    /* remembered for this visit only */
  }
}
