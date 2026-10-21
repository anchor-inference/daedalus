// The decisions of the Computer control's scope picker, apart from its page so they are tested on
// their own (computerscope.test.ts): which scope a choice makes, whether a grant may be asked for,
// where a preview picture is read from, and how a display is named to the operator.

import { t } from "./i18n";

export const COMPUTER = "/api/computer";
export const COMPUTER_APPS = "/api/computer/apps";
export const COMPUTER_PREVIEW = "/api/computer/preview";

/** The choices of how long, in minutes, and the bound the host holds a grant to. */
export const PRESETS = [10, 30, 60, 120] as const;
export const MAX_MINUTES = 240;

/** A preview is read again this often while its card is on screen, so a window that changed shows
 *  the change without the operator reopening the picker; off screen nothing is read at all. */
export const PREVIEW_REFRESH_MS = 4000;
/** The longest edge of a card's picture, and of the large one beside the choice. */
export const THUMB_EDGE = 320;
export const LARGE_EDGE = 720;

export type ScopeKind = "window" | "app" | "display" | "desktop";
export type ComputerScope = { kind: ScopeKind; window?: string; app?: string; display?: string };
export type ComputerWindow = { id: string; title: string; focused?: boolean; minimized?: boolean };
export type ComputerApp = { id: string; name: string; reachable?: boolean; elevated?: boolean; windows: ComputerWindow[] };
/** One monitor as the daemon describes it: bounds in desktop units, `pixels_per_unit` captured
 *  pixels per unit (2 on a Retina display), `scale` the factor the system shows the operator. */
export type ComputerDisplay = { id: string; name?: string; bounds: { x: number; y: number; w: number; h: number }; primary?: boolean; scale?: number; pixels_per_unit?: number };

/** What a picture is of: a window or a display, never both. */
export type PreviewTarget = { window: string } | { display: string };

/** The scope a choice makes, or none while the tab needs a target that is not chosen yet. The whole
 *  desktop needs none. */
export function buildScope(kind: ScopeKind, picked: string): ComputerScope | null {
  if (kind === "desktop") return { kind };
  if (!picked) return null;
  if (kind === "window") return { kind, window: picked };
  if (kind === "app") return { kind, app: picked };
  return { kind, display: picked };
}

export function validMinutes(minutes: number): boolean {
  return Number.isInteger(minutes) && minutes >= 1 && minutes <= MAX_MINUTES;
}

/** Where a picture is read from. The host proxies the launcher, which asks the daemon; a protected
 *  window answers with a refusal, never a picture. */
export function previewUrl(target: PreviewTarget, max: number = THUMB_EDGE): string {
  const query = new URLSearchParams();
  if ("window" in target) query.set("window", target.window);
  else query.set("display", target.display);
  query.set("max", String(Math.max(16, Math.min(960, Math.round(max)))));
  return `${COMPUTER_PREVIEW}?${query.toString()}`;
}

/** "Display 2" when the system gives no name, so two unnamed monitors are still told apart. */
export function displayName(display: ComputerDisplay, index: number): string {
  const name = display.name?.trim();
  return name || t("cu.display.n", { n: index + 1 });
}

/** The resolution the operator knows the monitor by: captured pixels, not desktop units, so a 4K
 *  panel at 150 % reads 3840 × 2160 rather than 2560 × 1440. */
export function displayResolution(display: ComputerDisplay): string {
  const factor = display.pixels_per_unit && display.pixels_per_unit > 0 ? display.pixels_per_unit : 1;
  return `${Math.round(display.bounds.w * factor)} × ${Math.round(display.bounds.h * factor)}`;
}

/** The system's scale as the operator sets it ("150 %"), or nothing at 100 %. */
export function displayScale(display: ComputerDisplay): string {
  const scale = display.scale ?? 1;
  return scale && Math.abs(scale - 1) > 0.01 ? `${Math.round(scale * 100)} %` : "";
}

/** The primary monitor first, then left to right, top to bottom, as they stand on the desk. */
export function sortedDisplays(displays: ComputerDisplay[]): ComputerDisplay[] {
  return [...displays].sort((a, b) => Number(!!b.primary) - Number(!!a.primary) || a.bounds.x - b.bounds.x || a.bounds.y - b.bounds.y);
}

/** One card of the Window tab: the window, the app it belongs to, and why it cannot be chosen. */
export type WindowCard = { id: string; title: string; app: string; minimized: boolean; focused: boolean; blocked: string };

export function windowCards(apps: ComputerApp[]): WindowCard[] {
  return apps.flatMap((app) => app.windows.map((w) => ({
    id: w.id,
    title: w.title || t("cu.window.untitled"),
    app: app.name,
    minimized: !!w.minimized,
    focused: !!w.focused,
    blocked: app.reachable === false ? t("cu.app.elevated") : "",
  })));
}

/** The window an app's card shows: its focused one, else its first that is not minimized. */
export function appFace(app: ComputerApp): ComputerWindow | null {
  return app.windows.find((w) => w.focused) ?? app.windows.find((w) => !w.minimized) ?? app.windows[0] ?? null;
}

/** What a chosen target's picture is, for the large preview; the whole desktop has a picture per display. */
export function chosenPreview(kind: ScopeKind, picked: string, apps: ComputerApp[]): PreviewTarget | null {
  if (!picked) return null;
  if (kind === "window") return { window: picked };
  if (kind === "display") return { display: picked };
  if (kind === "app") {
    const face = apps.find((a) => a.id === picked);
    const w = face ? appFace(face) : null;
    return w ? { window: w.id } : null;
  }
  return null;
}

/** A choice that went away (the window was closed, the monitor unplugged) is dropped, rather than a
 *  grant being asked for something no longer on the screen. */
export function stillThere(kind: ScopeKind, picked: string, apps: ComputerApp[], displays: ComputerDisplay[]): boolean {
  if (!picked || kind === "desktop") return true;
  if (kind === "display") return displays.some((d) => d.id === picked);
  if (kind === "app") return apps.some((a) => a.id === picked && a.reachable !== false);
  return apps.some((a) => a.reachable !== false && a.windows.some((w) => w.id === picked));
}
