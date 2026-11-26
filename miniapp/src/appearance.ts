// The look of this browser: one named theme, then the reader's own adjustments.
//
// Colours are written as inline custom properties. A stylesheet block for the light scheme would
// otherwise win over a later attribute whenever both are set, and the terminal can read only a
// computed hex — a var() or a color-mix() comes back as its own default. Inline values beat both
// scheme blocks and stay hex. Nothing here is stored on the host: a font address is fetched by
// this browser alone.

// Six neutral frames. A theme recolours the frame (the greys and the text) and nothing else: violet,
// cyan and orange keep one meaning on every theme (selection and the primary action, running and
// added, needs-you and removed), so no theme writes them, and none paints a gradient. The tinted and
// colour themes went for that reason; a stored id that is no longer here falls back to Dark.
export const THEME_IDS = ["ink", "paper", "sazha", "grafit", "slanets", "pepel"] as const;
export type ThemeId = (typeof THEME_IDS)[number];
export const FONT_ROLES = ["ui", "prose", "code"] as const;
export type FontRole = (typeof FONT_ROLES)[number];

export type Theme = {
  id: ThemeId;
  /** Light themes paint dark text. The rest paint light text. */
  light: boolean;
  /** The frame: the sidebar, settings and the drawer (`--bg`). */
  side: string;
  /** The page under the conversation and the lists (`--wash`). */
  wash: string;
  /** A card or the composer lying on the page. */
  card: string;
  /** What floats: a popover, a menu, a dialog. */
  surface: string;
  /** A solid fill and its two steps. */
  surface2: string;
  surface3: string;
  surface4: string;
  /** Code, terminals and diffs; the terminal's background. */
  inset: string;
  fg: string;
  fg2: string;
  fg3: string;
};

/** The three accents and the status colours, one set per scheme (ui/tokens.css declares the same
 *  values and says why the light ones sit a step under the drawn ones). Hex, for the terminal. */
type Accents = { accent: string; accentInk: string; primary: string; primaryInk: string; primaryHover: string; live: string; attention: string };
export const DARK_ACCENTS: Accents = { accent: "#a998ff", accentInk: "#141414", primary: "#765eee", primaryInk: "#ffffff", primaryHover: "#6a51e6", live: "#4fcbe0", attention: "#f59e4c" };
export const LIGHT_ACCENTS: Accents = { accent: "#5a41d8", accentInk: "#ffffff", primary: "#6a52e8", primaryInk: "#ffffff", primaryHover: "#5b42dc", live: "#0a778c", attention: "#ae530a" };

/** The sixteen ANSI colours of each scheme, in order. A theme puts black, white and bright white on its
 *  own ramp and keeps the rest: they are what programs choose, content rather than chrome. */
const DARK_ANSI = ["#262626", "#f86a6b", "#5bcc80", "#f3af49", "#5ba5f6", "#bf93f1", "#5cc9c2", "#a8a8a8", "#636363", "#ff8f8e", "#86e3a4", "#f8c877", "#8cc0fa", "#d4b5f6", "#8adcd6", "#ededed"];
const LIGHT_ANSI = ["#1b1b1b", "#d02c2a", "#218041", "#a75e0f", "#1f6dd8", "#7f47c7", "#007f80", "#6b6b6b", "#595959", "#e0524d", "#25b865", "#c98a26", "#4a84f0", "#b066dd", "#13b3a0", "#bebebe"];

export const THEMES: Theme[] = [
  // Dark and Light, the pair the app opens in (ui/tokens.css names the role of each step).
  { id: "ink", light: false, side: "#111111", wash: "#161616", card: "#1f1f1f", surface: "#262626", surface2: "#1f1f1f", surface3: "#262626", surface4: "#2e2e2e", inset: "#101010", fg: "#ededed", fg2: "#a8a8a8", fg3: "#8e8e8e" },
  { id: "paper", light: true, side: "#f5f5f4", wash: "#ffffff", card: "#ffffff", surface: "#ffffff", surface2: "#f5f5f4", surface3: "#efefee", surface4: "#e7e7e6", inset: "#f6f6f5", fg: "#1b1b1b", fg2: "#595959", fg3: "#6b6b6b" },
  // The grey family, as frames: a blacker one, two lighter ones (one a little cool) and a slate.
  { id: "sazha", light: false, side: "#0a0a0a", wash: "#0e0e0e", card: "#171717", surface: "#1e1e1e", surface2: "#171717", surface3: "#1e1e1e", surface4: "#262626", inset: "#080808", fg: "#ededed", fg2: "#a8a8a8", fg3: "#8e8e8e" },
  { id: "grafit", light: false, side: "#1a1a1d", wash: "#1f1f22", card: "#28282c", surface: "#2f2f33", surface2: "#28282c", surface3: "#2f2f33", surface4: "#37373c", inset: "#18181b", fg: "#ececee", fg2: "#b4b4ba", fg3: "#9a9aa1" },
  { id: "slanets", light: false, side: "#11161c", wash: "#151b22", card: "#1c242d", surface: "#232c37", surface2: "#1c242d", surface3: "#232c37", surface4: "#2b3541", inset: "#0f1419", fg: "#e7edf4", fg2: "#a9b6c6", fg3: "#909eaf" },
  { id: "pepel", light: false, side: "#232326", wash: "#29292c", card: "#313135", surface: "#38383c", surface2: "#313135", surface3: "#38383c", surface4: "#424247", inset: "#1f1f22", fg: "#f4f4f6", fg2: "#c8c8ce", fg3: "#adadb4" },
];

const BY_ID = new Map(THEMES.map((item) => [item.id, item]));

export function themeById(id: string | null | undefined): Theme {
  return BY_ID.get(id as ThemeId) ?? BY_ID.get("ink")!;
}

/** Faces the search offers. `google` is the Google Fonts family spec; a local face is already bundled. */
export const FONT_CATALOG: { family: string; google?: string; local?: boolean }[] = [
  { family: "Manrope", google: "Manrope:wght@400;500;600" },
  { family: "Literata", google: "Literata:opsz,wght@7..72,400;7..72,650" },
  { family: "Alegreya", google: "Alegreya:wght@400;600" },
  { family: "Source Serif 4", google: "Source+Serif+4:opsz,wght@8..60,400;8..60,600" },
  { family: "IBM Plex Sans", google: "IBM+Plex+Sans:wght@400;500" },
  { family: "IBM Plex Serif", google: "IBM+Plex+Serif:wght@400;600" },
  { family: "PT Sans", google: "PT+Sans:wght@400;700" },
  { family: "PT Serif", google: "PT+Serif:wght@400;700" },
  { family: "Nunito Sans", google: "Nunito+Sans:wght@400;600" },
  { family: "Rubik", google: "Rubik:wght@400;500" },
  { family: "Merriweather", google: "Merriweather:wght@400;700" },
  { family: "JetBrains Mono", local: true },
  { family: "IBM Plex Mono", google: "IBM+Plex+Mono:wght@400;500" },
  { family: "Source Code Pro", google: "Source+Code+Pro:wght@400;500" },
];

export type Scale = "sm" | "md" | "lg" | "xl";
export type Leading = "tight" | "normal" | "open";
/** The conversation's width: 720, 840 or 960 px, or the whole room between the sidebar and the panel up to 1400 px.
 *  The first three keep the names the earlier narrow, usual and wide choices were stored under, so a stored
 *  choice keeps its meaning without a conversion. */
export type Column = "narrow" | "normal" | "wide" | "full";
export const COLUMNS: readonly Column[] = ["narrow", "normal", "wide", "full"];
/** The stripe each choice draws, in pixels; the usual one is the stylesheet's own and writes nothing. */
export const COLUMN_WIDTH: Record<Column, number> = { narrow: 720, normal: 840, wide: 960, full: 1400 };
export type Radius = "sharp" | "normal" | "round";
/** Answer text in pixels, one per step; `null` is "usual", which follows the interface step. */
export const PROSE_MIN = 12;
export const PROSE_MAX = 22;
export type ProseSize = number | null;

/** What the reader may recolour: the frame and the text, never an accent. */
export type ColorKey = "bg" | "surface" | "fg" | "fg2";
export const COLOR_KEYS: readonly ColorKey[] = ["bg", "surface", "fg", "fg2"];

export type Prefs = {
  follow: boolean;
  theme: ThemeId;
  dark: ThemeId;
  light: ThemeId;
  colors: Partial<Record<ColorKey, string>>;
  wash: boolean;
  fontUi: string;
  fontProse: string;
  fontCode: string;
  fontUiUrl: string;
  fontProseUrl: string;
  fontCodeUrl: string;
  scale: Scale;
  prose: ProseSize;
  leading: Leading;
  column: Column;
  radius: Radius;
  motion: "full" | "reduce";
  contrast: "normal" | "high";
  code: "theme" | "dark";
  bubble: "card" | "plain";
  /** The agent's texts between its steps: drawn as replies that split the steps ("split"), or
   *  folded into the steps with the tool calls, as the transcript once did ("merged"). */
  replies: "split" | "merged";
};

export const DEFAULT_PREFS: Prefs = {
  follow: true,
  theme: "ink",
  dark: "ink",
  light: "paper",
  colors: {},
  wash: true,
  fontUi: "",
  fontProse: "",
  fontCode: "",
  fontUiUrl: "",
  fontProseUrl: "",
  fontCodeUrl: "",
  scale: "md",
  prose: null,
  leading: "normal",
  column: "normal",
  radius: "normal",
  motion: "full",
  contrast: "normal",
  code: "theme",
  bubble: "card",
  replies: "split",
};

const KEY = "daedalus.appearance";
const SCHEME = "daedalus.scheme";

const HEX = /^#[0-9a-f]{6}$/i;

export function isThemeId(id: string): id is ThemeId {
  return BY_ID.has(id as ThemeId);
}

/** A pasted face is either a Google stylesheet or an https .woff2. Anything else is refused. */
export function fontUrlAllowed(raw: string): boolean {
  if (!raw.trim()) return true;
  try {
    const url = new URL(raw.trim());
    if (url.protocol !== "https:") return false;
    if (url.hostname === "fonts.googleapis.com" || url.hostname === "fonts.gstatic.com") return true;
    return /\.woff2$/i.test(url.pathname);
  } catch {
    return false;
  }
}

export function googleStylesheet(family: string): string {
  const known = FONT_CATALOG.find((face) => face.family.toLowerCase() === family.trim().toLowerCase());
  const spec = known?.google ?? `${encodeURIComponent(family.trim())}:wght@400;600`;
  return `https://fonts.googleapis.com/css2?family=${spec}&display=swap`;
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}

function hex(value: string | undefined, fallback: string): string {
  return value && HEX.test(value) ? value : fallback;
}

function channel(value: string, index: number): number {
  return parseInt(value.slice(1 + index * 2, 3 + index * 2), 16);
}

export function mixHex(from: string, to: string, amount: number): string {
  if (!HEX.test(from) || !HEX.test(to)) return from;
  const pair = [0, 1, 2].map((i) => Math.round(channel(from, i) + (channel(to, i) - channel(from, i)) * amount));
  return `#${pair.map((n) => n.toString(16).padStart(2, "0")).join("")}`;
}

function luminance(value: string): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(channel(value, 0)) + 0.7152 * lin(channel(value, 1)) + 0.0722 * lin(channel(value, 2));
}

export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** What "usual" is drawn at: 15 px on a desktop and 16 on a phone, moved with the interface step. */
export function usualProse(scale: Scale, phone: boolean): number {
  return Math.round((phone ? 16 : 15) * SCALE[scale] * 10) / 10;
}

/** The earlier choices were "auto", "17" and "19"; they are now plain pixel sizes, and "auto" is null.
 *  Anything else that is not a whole size in range falls back to usual rather than to a clamp. */
function proseFromStored(value: unknown): ProseSize {
  const size = typeof value === "string" ? Number(value) : value;
  if (typeof size !== "number" || !Number.isInteger(size)) return DEFAULT_PREFS.prose;
  return size >= PROSE_MIN && size <= PROSE_MAX ? size : DEFAULT_PREFS.prose;
}

function readJson(): Partial<Prefs> | null {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as Partial<Prefs>) : null;
  } catch {
    return null;
  }
}

export function readPrefs(): Prefs {
  const stored = readJson();
  if (!stored) return { ...DEFAULT_PREFS, colors: {} };
  const colors: Prefs["colors"] = {};
  for (const key of COLOR_KEYS) {
    const value = stored.colors?.[key];
    if (value && HEX.test(value)) colors[key] = value;
  }
  return {
    ...DEFAULT_PREFS,
    ...stored,
    follow: typeof stored.follow === "boolean" ? stored.follow : DEFAULT_PREFS.follow,
    wash: typeof stored.wash === "boolean" ? stored.wash : DEFAULT_PREFS.wash,
    theme: isThemeId(stored.theme ?? "") ? stored.theme! : DEFAULT_PREFS.theme,
    dark: isThemeId(stored.dark ?? "") && !themeById(stored.dark).light ? stored.dark! : DEFAULT_PREFS.dark,
    light: isThemeId(stored.light ?? "") && themeById(stored.light).light ? stored.light! : DEFAULT_PREFS.light,
    scale: oneOf(stored.scale, ["sm", "md", "lg", "xl"] as const, DEFAULT_PREFS.scale),
    prose: proseFromStored(stored.prose),
    leading: oneOf(stored.leading, ["tight", "normal", "open"] as const, DEFAULT_PREFS.leading),
    column: oneOf(stored.column, COLUMNS, DEFAULT_PREFS.column),
    radius: oneOf(stored.radius, ["sharp", "normal", "round"] as const, DEFAULT_PREFS.radius),
    motion: oneOf(stored.motion, ["full", "reduce"] as const, DEFAULT_PREFS.motion),
    contrast: oneOf(stored.contrast, ["normal", "high"] as const, DEFAULT_PREFS.contrast),
    code: oneOf(stored.code, ["theme", "dark"] as const, DEFAULT_PREFS.code),
    bubble: oneOf(stored.bubble, ["card", "plain"] as const, DEFAULT_PREFS.bubble),
    replies: oneOf(stored.replies, ["split", "merged"] as const, DEFAULT_PREFS.replies),
    colors,
    fontUiUrl: fontUrlAllowed(stored.fontUiUrl ?? "") ? (stored.fontUiUrl ?? "") : "",
    fontProseUrl: fontUrlAllowed(stored.fontProseUrl ?? "") ? (stored.fontProseUrl ?? "") : "",
    fontCodeUrl: fontUrlAllowed(stored.fontCodeUrl ?? "") ? (stored.fontCodeUrl ?? "") : "",
  };
}

export type AppearanceEnv = { scheme: "dark" | "light" | null; prefersDark: boolean };

export function resolvedTheme(prefs: Prefs, env: AppearanceEnv): Theme {
  if (!prefs.follow) return themeById(prefs.theme);
  const mode = env.scheme === "dark" || env.scheme === "light" ? env.scheme : env.prefersDark ? "dark" : "light";
  return themeById(mode === "dark" ? prefs.dark : prefs.light);
}

const FS: Record<string, number> = { "--fs-11": 12, "--fs-12": 12, "--fs-13": 13, "--fs-14": 14, "--fs-15": 15, "--fs-16": 16, "--fs-18": 18, "--fs-mono": 13 };
const SCALE: Record<Scale, number> = { sm: 0.92, md: 1, lg: 1.1, xl: 1.18 };
/** The terminal a light theme keeps dark when asked to: Dark's inset and text. */
const DARK_TERMINAL = { bg: "#101010", fg: "#ededed" };

function ansiFor(base: Theme, forceDark: boolean): string[] {
  const ansi = (base.light && !forceDark ? LIGHT_ANSI : DARK_ANSI).slice();
  if (!base.light) {
    ansi[0] = base.surface;
    ansi[7] = base.fg2;
    ansi[15] = base.fg;
  }
  return ansi;
}

/** Every custom property the theme owns. Absent keys are removed, so a step back to "usual" restores the stylesheet. */
export function appearanceTokens(prefs: Prefs, env: AppearanceEnv): Record<string, string> {
  const base = resolvedTheme(prefs, env);
  const accents = base.light ? LIGHT_ACCENTS : DARK_ACCENTS;
  const bg = hex(prefs.colors.bg, base.side);
  const surface = hex(prefs.colors.surface, base.surface);
  const fg = hex(prefs.colors.fg, base.fg);
  const fg2 = prefs.contrast === "high" ? mixHex(hex(prefs.colors.fg2, base.fg2), fg, 0.55) : hex(prefs.colors.fg2, base.fg2);
  const fg3 = prefs.contrast === "high" ? mixHex(base.fg3, fg, 0.4) : base.fg3;
  const forceDark = prefs.code === "dark" && base.light;
  const ansi = ansiFor({ ...base, fg, fg2, surface }, forceDark);
  const line = base.light ? "rgba(0,0,0,.09)" : "rgba(255,255,255,.08)";
  const lineStrong = prefs.contrast === "high" ? (base.light ? "rgba(0,0,0,.30)" : "rgba(255,255,255,.28)") : base.light ? "rgba(0,0,0,.17)" : "rgba(255,255,255,.15)";
  const out: Record<string, string> = {
    "--bg": bg,
    "--wash": prefs.wash ? base.wash : bg,
    "--surface-1": surface,
    "--surface-2": base.surface2,
    "--card": prefs.colors.surface ? surface : base.card,
    "--inset": base.inset,
    "--surface-3": base.surface3,
    "--surface-4": base.surface4,
    "--line": line,
    "--line-strong": lineStrong,
    "--fg": fg,
    "--fg-2": fg2,
    "--fg-3": fg3,
    "--accent": accents.accent,
    "--accent-ink": accents.accentInk,
    "--primary": accents.primary,
    "--primary-ink": accents.primaryInk,
    "--primary-hover": accents.primaryHover,
    "--live": accents.live,
    "--attention": accents.attention,
    "--ok": accents.live,
    "--warn": accents.attention,
    "--bad": accents.attention,
    "--info": accents.live,
    // Code is ink a step under the text, on every theme: tinted amber, a block of it read as a warning.
    "--code-block": mixHex(fg, base.side, 0.14),
    "--term-bg": forceDark ? DARK_TERMINAL.bg : base.inset,
    "--term-fg": forceDark ? DARK_TERMINAL.fg : fg,
  };
  ansi.forEach((color, i) => {
    out[`--ansi-${i}`] = color;
  });
  if (prefs.scale !== "md") {
    for (const [name, size] of Object.entries(FS)) out[name] = `${Math.round(size * SCALE[prefs.scale] * 10) / 10}px`;
  }
  if (prefs.prose !== null) out["--fs-prose"] = `${prefs.prose}px`;
  if (prefs.leading === "tight") out["--lh-prose"] = "1.45";
  if (prefs.leading === "open") out["--lh-prose"] = "1.8";
  if (prefs.column !== "normal") out["--chat-w"] = `${COLUMN_WIDTH[prefs.column]}px`;
  // The whole room keeps a little more of it on each side than a fixed stripe needs to.
  if (prefs.column === "full") out["--chat-pad"] = "32px";
  if (prefs.radius === "sharp") {
    out["--radius-xs"] = "2px";
    out["--radius-sm"] = "4px";
    out["--radius-md"] = "6px";
    out["--radius-lg"] = "8px";
    out["--radius-xl"] = "12px";
    out["--radius-bubble"] = "10px";
  }
  if (prefs.radius === "round") {
    out["--radius-xs"] = "8px";
    out["--radius-sm"] = "12px";
    out["--radius-md"] = "16px";
    out["--radius-lg"] = "20px";
    out["--radius-xl"] = "26px";
    out["--radius-bubble"] = "24px";
  }
  const stack = (family: string) => (family ? `"${family.replace(/"/g, "")}", var(--font)` : "");
  if (prefs.fontUi && !isWoff(prefs.fontUiUrl)) out["--font"] = `"${prefs.fontUi.replace(/"/g, "")}", -apple-system, "Segoe UI", sans-serif`;
  if (prefs.fontProse && !isWoff(prefs.fontProseUrl)) out["--font-prose"] = stack(prefs.fontProse);
  if (prefs.fontCode && !isWoff(prefs.fontCodeUrl)) out["--mono"] = `"${prefs.fontCode.replace(/"/g, "")}", ui-monospace, monospace`;
  if (isWoff(prefs.fontUiUrl)) out["--font"] = `"Daedalus UI", -apple-system, sans-serif`;
  if (isWoff(prefs.fontProseUrl)) out["--font-prose"] = `"Daedalus Prose", var(--font)`;
  if (isWoff(prefs.fontCodeUrl)) out["--mono"] = `"Daedalus Code", ui-monospace, monospace`;
  return out;
}

function isWoff(url: string): boolean {
  return fontUrlAllowed(url) && /\.woff2$/i.test(url);
}

function currentEnv(): AppearanceEnv {
  let scheme: AppearanceEnv["scheme"] = null;
  try {
    const stored = localStorage.getItem(SCHEME);
    scheme = stored === "dark" || stored === "light" ? stored : null;
  } catch {
    /* private mode */
  }
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)")?.matches ?? true;
  return { scheme, prefersDark };
}

const OWNED = new Set<string>();

function loadFace(id: string, href: string | null, family: string | null) {
  const root = document.head;
  const prev = document.getElementById(id);
  if (!href) {
    prev?.remove();
    return;
  }
  if (/\.woff2$/i.test(href) && family) {
    prev?.remove();
    const face = new FontFace(family, `url("${href}")`);
    void face.load().then((loaded) => document.fonts.add(loaded)).catch(() => undefined);
    return;
  }
  if (prev instanceof HTMLLinkElement && prev.href === href) return;
  prev?.remove();
  const link = document.createElement("link");
  link.id = id;
  link.rel = "stylesheet";
  link.href = href;
  root.appendChild(link);
}

function fontHref(family: string, url: string): string | null {
  if (url && fontUrlAllowed(url)) return url;
  if (!family) return null;
  const known = FONT_CATALOG.find((face) => face.family === family);
  if (known?.local) return null;
  return googleStylesheet(family);
}

/** Paint the document from the saved preferences. Safe to call before the first render. */
export function applyAppearance(prefs: Prefs = readPrefs(), env: AppearanceEnv = typeof window === "undefined" ? { scheme: null, prefersDark: true } : currentEnv()): void {
  if (typeof document === "undefined") return;
  const tokens = appearanceTokens(prefs, env);
  const root = document.documentElement;
  const next = new Set(Object.keys(tokens));
  for (const name of OWNED) if (!next.has(name)) root.style.removeProperty(name);
  for (const [name, value] of Object.entries(tokens)) root.style.setProperty(name, value);
  OWNED.clear();
  for (const name of next) OWNED.add(name);
  const theme = resolvedTheme(prefs, env);
  root.dataset.scheme = theme.light ? "light" : "dark";
  root.dataset.theme = theme.id;
  if (prefs.motion === "reduce") root.dataset.motion = "reduce";
  else delete root.dataset.motion;
  if (prefs.bubble === "plain") root.dataset.user = "plain";
  else delete root.dataset.user;
  loadFace("daedalus-font-ui", fontHref(prefs.fontUi, prefs.fontUiUrl), isWoff(prefs.fontUiUrl) ? "Daedalus UI" : null);
  loadFace("daedalus-font-prose", fontHref(prefs.fontProse, prefs.fontProseUrl), isWoff(prefs.fontProseUrl) ? "Daedalus Prose" : null);
  loadFace("daedalus-font-code", fontHref(prefs.fontCode, prefs.fontCodeUrl), isWoff(prefs.fontCodeUrl) ? "Daedalus Code" : null);
}

export function writePrefs(prefs: Prefs): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    /* private mode: the choice lasts for the visit */
  }
  applyAppearance(prefs);
  window.dispatchEvent(new Event("daedalus-appearance"));
}

export function updatePrefs(patch: Partial<Prefs>): Prefs {
  const current = readPrefs();
  const next: Prefs = { ...current, ...patch, colors: patch.colors ? { ...current.colors, ...patch.colors } : current.colors };
  writePrefs(next);
  return next;
}

export function resetColors(): Prefs {
  const next = { ...readPrefs(), colors: {} };
  writePrefs(next);
  return next;
}
