// What the desktop shell binds for Quick Chat, the appshot and the tray (desktop/shell/preload.js).
// Absent in a browser, inside Telegram, and in a desktop application older than these.

export type DeskSettings = { quickChat: string; appshot: string; region: string; voice: string; hideToTray: boolean };

export type ShortcutName = "quickChat" | "appshot" | "region" | "voice";

export type ShortcutReport = { accelerator: string; ok: boolean; reason: "" | "off" | "invalid" | "taken" };

export type DeskInfo = {
  settings: DeskSettings;
  defaults: DeskSettings;
  report: Partial<Record<ShortcutName, ShortcutReport>>;
  /** Fields of the last change that were refused, and why. */
  errors: Partial<Record<ShortcutName, "invalid" | "duplicate">>;
  platform: string;
  wayland: boolean;
  tray: { ready: boolean; note: "" | "gnome" | "unavailable" };
  /** macOS's screen-recording permission: granted, denied, restricted, not-determined; "granted" elsewhere. */
  screen: string;
};

export type DeskBridge = {
  settings: () => Promise<DeskInfo | null>;
  setSettings: (patch: Partial<DeskSettings>) => Promise<DeskInfo | null>;
  status: (status: { running: number; waiting: number; companion: { paused: boolean } | null }) => void;
  openScreenSettings: () => void;
  onAction: (callback: (name: string) => void) => () => void;
  onNavigate: (callback: (path: string) => void) => () => void;
};

/** A picture, or why there is none. "dismissed" is the operator pressing Esc over the area overlay,
 *  which needs no word from Quick Chat. */
export type AppshotResult =
  | { ok: true; name: string; type: string; bytes: Uint8Array; width: number; height: number }
  | { ok: false; error: string; detail: string };

export type WindowChoice = { id: string; name: string; thumbnail: string };

export type QuickBridge = {
  hide: () => void;
  resize: (height: number) => void;
  open: (path: string) => void;
  pending: () => Promise<AppshotResult[]>;
  windows: () => Promise<{ ok: true; windows: WindowChoice[] } | { ok: false; error: string; detail: string } | null>;
  capture: (windowId?: string) => Promise<AppshotResult | null>;
  /** The area appshot: an overlay on every display to drag a rectangle on. Absent in a shell older than it. */
  region?: () => Promise<AppshotResult | null>;
  /** Whether the voice shortcut asked for dictation since the page last looked; asking clears it. */
  voice?: () => Promise<boolean>;
  onShown: (callback: (detail: { appshot?: boolean; voice?: boolean }) => void) => () => void;
};
