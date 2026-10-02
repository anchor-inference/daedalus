// The terminal's colours, read from the same stylesheet tokens as the rest of the app, so a terminal
// follows the light and dark schemes and the Telegram theme like every other surface.
//
// xterm.js takes colours as strings it parses itself (hex and rgb()/rgba()), so the tokens it reads
// are plain values; a token that is missing or unreadable falls back to the dark palette rather than
// leaving a colour undefined, which xterm.js would draw as its own default.

import type { ITheme } from "@xterm/xterm";

/** Reads a CSS custom property's value, e.g. from `getComputedStyle(document.documentElement)`. */
export type TokenReader = (name: string) => string;

const ANSI_NAMES = [
  "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
  "brightBlack", "brightRed", "brightGreen", "brightYellow", "brightBlue", "brightMagenta", "brightCyan", "brightWhite",
] as const;

/** The dark palette from `ui/tokens.css`, used for any token that cannot be read. */
export const FALLBACK = {
  background: "#09090b",
  foreground: "#ededef",
  cursor: "#2dd4bf",
  ansi: [
    "#1c1c1f", "#f0625d", "#5fd68a", "#e8b44c", "#7db4ff", "#c792ea", "#2dd4bf", "#c8c9ce",
    "#5c5d66", "#ff8a85", "#8be9a8", "#f5cf7a", "#a6ccff", "#ddb3f5", "#7ee8d9", "#ededef",
  ],
};

const COLOUR = /^(#[0-9a-f]{3,8}|rgba?\([^()]*\))$/i;

function colour(read: TokenReader, name: string, fallback: string): string {
  const value = (read(name) ?? "").trim();
  return COLOUR.test(value) ? value : fallback;
}

/** `#rgb`/`#rrggbb` as an rgba() with the given alpha; anything else comes back unchanged. */
export function withAlpha(value: string, alpha: number): string {
  let hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value)?.[1];
  if (!hex) return value;
  if (hex.length === 3) hex = [...hex].map((c) => c + c).join("");
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex!.slice(i, i + 2), 16));
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** The xterm.js theme from the stylesheet tokens (`--term-bg`, `--fg`, `--accent`, `--ansi-0…15`). */
export function terminalTheme(read: TokenReader): ITheme {
  const background = colour(read, "--term-bg", FALLBACK.background);
  // --term-fg lets a light page keep a dark terminal. xterm cannot read var(), so the token is a hex
  // of its own; when it is absent the page colour is the terminal colour, as it always was.
  const foreground = colour(read, "--term-fg", colour(read, "--fg", FALLBACK.foreground));
  const cursor = colour(read, "--accent", FALLBACK.cursor);
  const theme: ITheme = {
    background,
    foreground,
    cursor,
    cursorAccent: background,
    selectionBackground: withAlpha(cursor, 0.3),
    selectionInactiveBackground: withAlpha(foreground, 0.15),
  };
  ANSI_NAMES.forEach((name, i) => {
    theme[name] = colour(read, `--ansi-${i}`, FALLBACK.ansi[i]);
  });
  return theme;
}

/** The colours the daemon answers OSC 10/11/12 queries with, taken from the same theme. */
export function attachTheme(theme: ITheme): { fg: string; bg: string; cursor: string } {
  return { fg: theme.foreground ?? FALLBACK.foreground, bg: theme.background ?? FALLBACK.background, cursor: theme.cursor ?? FALLBACK.cursor };
}

/** The document's tokens, as the running page has them now (the scheme can change at any time). */
export function documentTokens(): TokenReader {
  const style = getComputedStyle(document.documentElement);
  return (name) => style.getPropertyValue(name);
}
