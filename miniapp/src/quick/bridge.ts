// What the desktop shell binds for Quick Chat, the appshot and the tray (desktop/shell/preload.js).
// Absent in a browser, inside Telegram, and in a desktop application older than these.

export type DeskSettings = { quickChat: string; appshot: string; hideToTray: boolean };

export type ShortcutReport = { accelerator: string; ok: boolean; reason: "" | "off" | "invalid" | "taken" };

export type DeskInfo = {
  settings: DeskSettings;
  defaults: DeskSettings;
  report: { quickChat?: ShortcutReport; appshot?: ShortcutReport };
  /** Fields of the last change that were refused, and why. */
  errors: { quickChat?: "invalid" | "duplicate"; appshot?: "invalid" | "duplicate" };
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
  onShown: (callback: (detail: { appshot?: boolean }) => void) => () => void;
};
