/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import css from "../ui/tokens.css?raw";
import { attachTheme, FALLBACK, terminalTheme, withAlpha } from "./theme";

/** The custom properties a CSS block declares, by name. */
function block(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  expect(start).toBeGreaterThanOrEqual(0);
  const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("\n}", start));
  const out: Record<string, string> = {};
  for (const m of body.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)) out[m[1]] = m[2].trim();
  return out;
}

const TOKENS = ["--term-bg", ...Array.from({ length: 16 }, (_, i) => `--ansi-${i}`)];

describe("the terminal theme", () => {
  it("has every terminal token in the dark and the light scheme, as a colour xterm.js can read", () => {
    for (const selector of [":root", ':root[data-scheme="light"]']) {
      const tokens = block(selector);
      for (const name of TOKENS) expect(tokens[name], `${selector} ${name}`).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });

  it("builds the xterm.js theme from the stylesheet's dark tokens", () => {
    const dark = block(":root");
    const theme = terminalTheme((name) => (name === "--fg" ? "#ededef" : dark[name] ?? ""));
    expect(theme.background).toBe(dark["--term-bg"]);
    expect(theme.foreground).toBe("#ededef");
    expect(theme.cursor).toBe(dark["--accent"]);
    expect(theme.red).toBe(dark["--ansi-1"]);
    expect(theme.brightWhite).toBe(dark["--ansi-15"]);
    expect(theme.selectionBackground).toBe(withAlpha(dark["--accent"], 0.3));
    expect(theme.cursorAccent).toBe(theme.background);
  });

  it("keeps the fallback palette in step with the stylesheet", () => {
    const dark = block(":root");
    expect(FALLBACK.background).toBe(dark["--term-bg"]);
    expect(FALLBACK.ansi).toEqual(Array.from({ length: 16 }, (_, i) => dark[`--ansi-${i}`]));
  });

  it("falls back rather than hand xterm.js something it cannot parse", () => {
    const theme = terminalTheme((name) => (name === "--fg" ? "var(--tg-theme-text-color)" : name === "--ansi-3" ? "color-mix(in srgb, red, blue)" : ""));
    expect(theme.foreground).toBe(FALLBACK.foreground);
    expect(theme.yellow).toBe(FALLBACK.ansi[3]);
    expect(theme.background).toBe(FALLBACK.background);
  });

  it("takes rgb() values and Telegram's hex colours as they are", () => {
    const theme = terminalTheme((name) => (name === "--fg" ? "rgb(10, 20, 30)" : name === "--term-bg" ? "#fff" : ""));
    expect(theme.foreground).toBe("rgb(10, 20, 30)");
    expect(theme.background).toBe("#fff");
    expect(withAlpha("#fff", 0.5)).toBe("rgba(255, 255, 255, 0.5)");
  });

  it("gives the daemon the colours to answer OSC 10/11/12 with", () => {
    expect(attachTheme({ foreground: "#111111", background: "#222222", cursor: "#333333" })).toEqual({ fg: "#111111", bg: "#222222", cursor: "#333333" });
  });
});
