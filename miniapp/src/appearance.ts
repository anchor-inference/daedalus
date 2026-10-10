// The look of this browser: one named theme, then the reader's own adjustments.
//
// Colours are written as inline custom properties. A stylesheet block for the light scheme would
// otherwise win over a later attribute whenever both are set, and the terminal can read only a
// computed hex — a var() or a color-mix() comes back as its own default. Inline values beat both
// scheme blocks and stay hex. Nothing here is stored on the host: a font address is fetched by
// this browser alone.

export const THEME_IDS = ["serebro", "zakat", "malibu", "baltika", "claude", "sazha", "grafit", "slanets", "pepel", "oranzhereya", "med", "glub", "laim"] as const;
export type ThemeId = (typeof THEME_IDS)[number];
export const THEME_GROUPS = ["light", "gray", "color"] as const;
export type ThemeGroup = (typeof THEME_GROUPS)[number];
export const FONT_ROLES = ["ui", "prose", "code"] as const;
export type FontRole = (typeof FONT_ROLES)[number];

export type Theme = {
  id: ThemeId;
  group: ThemeGroup;
  /** Light themes paint dark text. The rest paint light text, grey ones included. */
  light: boolean;
  bg: string;
  side: string;
  surface: string;
  surface2: string;
  fg: string;
  fg2: string;
  fg3: string;
  accent: string;
  send: string;
  sendInk: string;
  wash: string;
  termBg: string;
};

const DARK_STATUS = { ok: "#5fd68a", warn: "#e8b44c", bad: "#f0625d", info: "#7db4ff" };
const LIGHT_STATUS = { ok: "#1e9e56", warn: "#b7791f", bad: "#d93b36", info: "#2f6fe0" };

/** The sixteen ANSI colours of the dark scheme, in order. A theme recolors the frame and keeps these. */
const DARK_ANSI = ["#1c1c1f", "#f0625d", "#5fd68a", "#e8b44c", "#7db4ff", "#c792ea", "#2dd4bf", "#c8c9ce", "#5c5d66", "#ff8a85", "#8be9a8", "#f5cf7a", "#a6ccff", "#ddb3f5", "#7ee8d9", "#ededef"];
const LIGHT_ANSI = ["#141416", "#d93b36", "#1e9e56", "#b7791f", "#2f6fe0", "#9b4dca", "#0e9f8e", "#8e9098", "#5b5d66", "#e0524d", "#25b865", "#c98a26", "#4a84f0", "#b066dd", "#13b3a0", "#b4b5bb"];

function theme(partial: Theme): Theme {
  return partial;
}

export const THEMES: Theme[] = [
  theme({ id: "serebro", group: "light", light: true, bg: "#E7EEF2", side: "#D9E3EA", surface: "#F7F4EF", surface2: "#EFE8DF", fg: "#1C242B", fg2: "#3E4C57", fg3: "#5C6B76", accent: "#1F5E73", send: "#1F5E73", sendInk: "#F4FBFD", wash: "linear-gradient(165deg,#E6EEF2 0%,#E8E4DC 58%,#D8CFC4 100%)", termBg: "#F7F4EF" }),
  theme({ id: "zakat", group: "light", light: true, bg: "#F7F1E8", side: "#EFE4D6", surface: "#FFFCF8", surface2: "#F4E7DA", fg: "#2A221C", fg2: "#5C4A3E", fg3: "#6F5C4E", accent: "#9A4E22", send: "#9A4E22", sendInk: "#FFF8F3", wash: "linear-gradient(145deg,#F8E7D6 0%,#F6F1EA 46%,#E3EEF5 100%)", termBg: "#FFFCF8" }),
  theme({ id: "malibu", group: "light", light: true, bg: "#F3F7F2", side: "#E3F0F2", surface: "#FFFEFB", surface2: "#E5F4E4", fg: "#14323A", fg2: "#3E5960", fg3: "#4E6A70", accent: "#0C6E84", send: "#0C6E84", sendInk: "#F3FBFC", wash: "linear-gradient(160deg,#D7F3FA 0%,#F3F7F2 48%,#E7F0D4 100%)", termBg: "#FFFEFB" }),
  theme({ id: "baltika", group: "light", light: true, bg: "#E6EBEF", side: "#D7E0E6", surface: "#F4F7F8", surface2: "#E7EEF1", fg: "#1B2832", fg2: "#3E4E59", fg3: "#52616B", accent: "#8A5314", send: "#8A5314", sendInk: "#FFF8EF", wash: "linear-gradient(180deg,#E7EEF2 0%,#E6EBEF 55%,#E4E2DA 100%)", termBg: "#F4F7F8" }),
  theme({ id: "claude", group: "gray", light: false, bg: "#151515", side: "#111111", surface: "#1c1c1c", surface2: "#303030", fg: "#eceae6", fg2: "#a9a69f", fg3: "#8c8982", accent: "#8aa0c8", send: "#e8e6e0", sendInk: "#1a1917", wash: "#151515", termBg: "#101010" }),
  theme({ id: "sazha", group: "gray", light: false, bg: "#070708", side: "#050506", surface: "#141416", surface2: "#1c1c1f", fg: "#f5f5f6", fg2: "#b4b4b8", fg3: "#8e8e93", accent: "#f5f5f6", send: "#ffffff", sendInk: "#111113", wash: "#070708", termBg: "#050506" }),
  theme({ id: "grafit", group: "gray", light: false, bg: "#1c1c20", side: "#18181c", surface: "#26262b", surface2: "#2e2e34", fg: "#ececee", fg2: "#b7b7be", fg3: "#8e8e98", accent: "#c6c8d0", send: "#e4e4e8", sendInk: "#1a1a1e", wash: "#1c1c20", termBg: "#141418" }),
  theme({ id: "slanets", group: "gray", light: false, bg: "#12171e", side: "#10151b", surface: "#1c2430", surface2: "#243040", fg: "#e7edf4", fg2: "#a9b6c6", fg3: "#8494a6", accent: "#9eb0c4", send: "#c5d4e4", sendInk: "#12171e", wash: "#12171e", termBg: "#0e1318" }),
  theme({ id: "pepel", group: "gray", light: false, bg: "#2a2a2e", side: "#242428", surface: "#34343a", surface2: "#3e3e44", fg: "#f4f4f6", fg2: "#c8c8ce", fg3: "#a0a0a8", accent: "#f4f4f6", send: "#f4f4f6", sendInk: "#242428", wash: "#2a2a2e", termBg: "#1e1e22" }),
  theme({ id: "oranzhereya", group: "color", light: false, bg: "#101916", side: "#0C1411", surface: "#18241F", surface2: "#21302A", fg: "#E7F3EC", fg2: "#A9C4B6", fg3: "#8EAEA0", accent: "#7EE0C3", send: "#E4F26A", sendInk: "#1A1C10", wash: "radial-gradient(900px 520px at 88% -8%, #1A3330 0%, #101916 58%)", termBg: "#0C1411" }),
  theme({ id: "med", group: "color", light: false, bg: "#1A1210", side: "#140E0C", surface: "#261A16", surface2: "#32241E", fg: "#F6EDE6", fg2: "#D4B5A6", fg3: "#B89A8C", accent: "#E2A36B", send: "#E2A36B", sendInk: "#2A160E", wash: "radial-gradient(880px 520px at 0% -10%, #3A241C 0%, #1A1210 62%)", termBg: "#140E0C" }),
  theme({ id: "glub", group: "color", light: false, bg: "#14122A", side: "#100E22", surface: "#1E1B38", surface2: "#282450", fg: "#ECEAF6", fg2: "#B7B3D0", fg3: "#9C98B8", accent: "#7EE0D0", send: "#7EE0D0", sendInk: "#10211E", wash: "linear-gradient(165deg,#1A1440 0%,#14122A 42%,#0E2438 100%)", termBg: "#100E22" }),
  theme({ id: "laim", group: "color", light: false, bg: "#14160F", side: "#10120C", surface: "#1E2218", surface2: "#292E20", fg: "#F4F1E6", fg2: "#C9C6B4", fg3: "#A8A594", accent: "#D2EE6A", send: "linear-gradient(90deg,#D6F25A,#FF8A4A)", sendInk: "#1A1C10", wash: "radial-gradient(840px 480px at 100% 0%, #2A2E16 0%, #14160F 52%)", termBg: "#10120C" }),
];

const BY_ID = new Map(THEMES.map((item) => [item.id, item]));

export function themeById(id: string | null | undefined): Theme {
  return BY_ID.get(id as ThemeId) ?? BY_ID.get("claude")!;
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
export type Column = "narrow" | "normal" | "wide";
export type Radius = "sharp" | "normal" | "round";
/** Answer text in pixels, one per step; `null` is "usual", which follows the interface step. */
export const PROSE_MIN = 12;
export const PROSE_MAX = 22;
export type ProseSize = number | null;

export type ColorKey = "bg" | "surface" | "fg" | "fg2" | "accent" | "send";

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
};

export const DEFAULT_PREFS: Prefs = {
  follow: true,
  theme: "claude",
  dark: "claude",
  light: "serebro",
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

export function inkFor(value: string): string {
  if (!HEX.test(value)) return "#1a1a1a";
  return luminance(value) > 0.55 ? "#1a1a1a" : "#f6f6f4";
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
  for (const key of ["bg", "surface", "fg", "fg2", "accent", "send"] as const) {
    const value = stored.colors?.[key];
    if (value && HEX.test(value)) colors[key] = value;
  }
  return {
    ...DEFAULT_PREFS,
    ...stored,
    follow: typeof stored.follow === "boolean" ? stored.follow : DEFAULT_PREFS.follow,
    wash: typeof stored.wash === "boolean" ? stored.wash : DEFAULT_PREFS.wash,
    theme: isThemeId(stored.theme ?? "") ? stored.theme! : DEFAULT_PREFS.theme,
    dark: isThemeId(stored.dark ?? "") ? stored.dark! : DEFAULT_PREFS.dark,
    light: isThemeId(stored.light ?? "") ? stored.light! : DEFAULT_PREFS.light,
    scale: oneOf(stored.scale, ["sm", "md", "lg", "xl"] as const, DEFAULT_PREFS.scale),
    prose: proseFromStored(stored.prose),
    leading: oneOf(stored.leading, ["tight", "normal", "open"] as const, DEFAULT_PREFS.leading),
    column: oneOf(stored.column, ["narrow", "normal", "wide"] as const, DEFAULT_PREFS.column),
    radius: oneOf(stored.radius, ["sharp", "normal", "round"] as const, DEFAULT_PREFS.radius),
    motion: oneOf(stored.motion, ["full", "reduce"] as const, DEFAULT_PREFS.motion),
    contrast: oneOf(stored.contrast, ["normal", "high"] as const, DEFAULT_PREFS.contrast),
    code: oneOf(stored.code, ["theme", "dark"] as const, DEFAULT_PREFS.code),
    bubble: oneOf(stored.bubble, ["card", "plain"] as const, DEFAULT_PREFS.bubble),
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

const FS: Record<string, number> = { "--fs-11": 11, "--fs-12": 12, "--fs-13": 13, "--fs-14": 14, "--fs-15": 15, "--fs-16": 16, "--fs-18": 18, "--fs-mono": 12.5 };
const SCALE: Record<Scale, number> = { sm: 0.92, md: 1, lg: 1.1, xl: 1.18 };

function ansiFor(base: Theme, forceDark: boolean): string[] {
  const ansi = (base.light && !forceDark ? LIGHT_ANSI : DARK_ANSI).slice();
  if (!base.light || forceDark) {
    ansi[0] = base.surface;
    ansi[6] = HEX.test(base.accent) ? base.accent : ansi[6];
    ansi[7] = base.fg2;
    ansi[15] = base.fg;
  } else if (HEX.test(base.accent)) {
    ansi[6] = base.accent;
  }
  return ansi;
}

/** Every custom property the theme owns. Absent keys are removed, so a step back to "usual" restores the stylesheet. */
export function appearanceTokens(prefs: Prefs, env: AppearanceEnv): Record<string, string> {
  const base = resolvedTheme(prefs, env);
  const status = base.light ? LIGHT_STATUS : DARK_STATUS;
  const bg = hex(prefs.colors.bg, base.side);
  const surface = hex(prefs.colors.surface, base.surface);
  const fg = hex(prefs.colors.fg, base.fg);
  const fg2 = prefs.contrast === "high" ? mixHex(hex(prefs.colors.fg2, base.fg2), fg, 0.55) : hex(prefs.colors.fg2, base.fg2);
  const fg3 = prefs.contrast === "high" ? mixHex(base.fg3, fg, 0.4) : base.fg3;
  const accent = hex(prefs.colors.accent, base.accent);
  const send = prefs.colors.send && HEX.test(prefs.colors.send) ? prefs.colors.send : base.send;
  const sendInk = HEX.test(send) ? inkFor(send) : base.sendInk;
  const forceDark = prefs.code === "dark" && base.light;
  const ansi = ansiFor({ ...base, accent, fg, fg2, surface }, forceDark);
  const line = base.light ? "rgba(28,36,43,.12)" : "rgba(255,255,255,.10)";
  const lineStrong = prefs.contrast === "high" ? (base.light ? "rgba(28,36,43,.28)" : "rgba(255,255,255,.28)") : base.light ? "rgba(28,36,43,.18)" : "rgba(255,255,255,.16)";
  const out: Record<string, string> = {
    "--bg": bg,
    "--wash": prefs.wash ? base.wash : bg,
    "--surface-1": surface,
    "--surface-2": base.surface2,
    "--surface-3": mixHex(base.surface2, fg, base.light ? 0.06 : 0.08),
    "--surface-4": mixHex(base.surface2, fg, base.light ? 0.12 : 0.14),
    "--line": line,
    "--line-strong": lineStrong,
    "--fg": fg,
    "--fg-2": fg2,
    "--fg-3": fg3,
    "--accent": accent,
    "--accent-ink": inkFor(accent),
    "--primary": send,
    "--primary-ink": sendInk,
    "--ok": status.ok,
    "--warn": status.warn,
    "--bad": status.bad,
    "--info": status.info,
    "--code": base.light ? "#7c3f00" : "#f5b56e",
    "--code-block": base.light ? "#3b2a12" : "#e8c48a",
    "--shadow-pop": base.light ? `0 12px 32px rgba(0,0,0,.14), 0 0 0 1px ${line}` : `0 12px 32px rgba(0,0,0,.5), 0 0 0 1px ${lineStrong}`,
    "--term-bg": forceDark ? "#09090b" : base.termBg,
    "--term-fg": forceDark ? "#ededef" : fg,
  };
  ansi.forEach((color, i) => {
    out[`--ansi-${i}`] = color;
  });
  if (prefs.scale !== "md") {
    for (const [name, size] of Object.entries(FS)) out[name] = `${Math.round(size * SCALE[prefs.scale] * 10) / 10}px`;
  }
  if (prefs.prose !== null) out["--fs-prose"] = `${prefs.prose}px`;
  if (prefs.leading === "tight") out["--lh-prose"] = "1.4";
  if (prefs.leading === "open") out["--lh-prose"] = "1.75";
  if (prefs.column === "narrow") out["--chat-w"] = "760px";
  if (prefs.column === "wide") out["--chat-w"] = "1200px";
  if (prefs.radius === "sharp") {
    out["--radius-xs"] = "2px";
    out["--radius-sm"] = "4px";
    out["--radius-md"] = "6px";
    out["--radius-lg"] = "10px";
  }
  if (prefs.radius === "round") {
    out["--radius-xs"] = "10px";
    out["--radius-sm"] = "14px";
    out["--radius-md"] = "18px";
    out["--radius-lg"] = "24px";
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
