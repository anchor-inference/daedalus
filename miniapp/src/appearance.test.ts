import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_PREFS, FONT_CATALOG, THEMES, appearanceTokens, contrast, fontUrlAllowed, googleStylesheet, isThemeId, readPrefs, resolvedTheme, themeById, usualProse } from "./appearance";

const dark = { scheme: "dark" as const, prefersDark: true };
const light = { scheme: "light" as const, prefersDark: false };

describe("themes", () => {
  it("keeps text readable on every theme", () => {
    for (const theme of THEMES) {
      expect(contrast(theme.fg, theme.side), theme.id).toBeGreaterThanOrEqual(4.5);
      expect(contrast(theme.fg, theme.surface), theme.id).toBeGreaterThanOrEqual(4.5);
      expect(contrast(theme.fg2, theme.side), theme.id).toBeGreaterThanOrEqual(4.5);
      if (theme.send.startsWith("#")) expect(contrast(theme.sendInk, theme.send), theme.id).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("resolves follow-system to the dark and light pair, and a chosen theme when follow is off", () => {
    const prefs = { ...DEFAULT_PREFS, colors: {} };
    expect(resolvedTheme(prefs, dark).id).toBe("claude");
    expect(resolvedTheme(prefs, light).id).toBe("serebro");
    expect(resolvedTheme({ ...prefs, follow: false, theme: "baltika" }, dark).id).toBe("baltika");
    expect(themeById("nope").id).toBe("claude");
    expect(isThemeId("malibu")).toBe(true);
  });

  it("lets a colour override replace the theme, and keeps the terminal dark when asked", () => {
    const prefs = { ...DEFAULT_PREFS, follow: false, theme: "serebro" as const, colors: { bg: "#112233" } };
    const tokens = appearanceTokens(prefs, light);
    expect(tokens["--bg"]).toBe("#112233");
    expect(tokens["--ansi-0"]).toMatch(/^#[0-9a-f]{6}$/i);
    const forced = appearanceTokens({ ...prefs, colors: {}, code: "dark" }, light);
    expect(forced["--term-bg"]).toBe("#09090b");
    expect(forced["--term-fg"]).toBe("#ededef");
    expect(Object.keys(forced).filter((name) => name.startsWith("--ansi-"))).toHaveLength(16);
  });

  it("leaves the type scale alone at the usual step and moves it otherwise", () => {
    expect(appearanceTokens(DEFAULT_PREFS, dark)["--fs-14"]).toBeUndefined();
    expect(appearanceTokens({ ...DEFAULT_PREFS, scale: "lg" }, dark)["--fs-14"]).toBe("15.4px");
    expect(appearanceTokens({ ...DEFAULT_PREFS, column: "narrow" }, dark)["--chat-w"]).toBe("760px");
    expect(appearanceTokens(DEFAULT_PREFS, dark)["--chat-w"]).toBeUndefined();
  });

  it("accepts a Google stylesheet or an https woff2 and refuses anything else", () => {
    expect(fontUrlAllowed("")).toBe(true);
    expect(fontUrlAllowed("https://fonts.googleapis.com/css2?family=Literata")).toBe(true);
    expect(fontUrlAllowed("https://example.com/face.woff2")).toBe(true);
    expect(fontUrlAllowed("http://example.com/face.woff2")).toBe(false);
    expect(fontUrlAllowed("https://example.com/face.css")).toBe(false);
    expect(googleStylesheet("Literata")).toContain("family=Literata");
    expect(FONT_CATALOG.some((face) => face.local)).toBe(true);
  });

  it("drops a stored theme it does not know", () => {
    const kept = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => kept.get(key) ?? null, setItem: (key: string, value: string) => void kept.set(key, value), removeItem: (key: string) => void kept.delete(key) });
    localStorage.setItem("daedalus.appearance", JSON.stringify({ theme: "cheek", follow: false, colors: { bg: "red" } }));
    const prefs = readPrefs();
    expect(prefs.theme).toBe("claude");
    expect(prefs.follow).toBe(false);
    expect(prefs.colors.bg).toBeUndefined();
  });

  it("sets answer text to any whole size from 12 to 22 px, independent of the interface step", () => {
    for (const size of [12, 17, 22]) {
      expect(appearanceTokens({ ...DEFAULT_PREFS, prose: size }, dark)["--fs-prose"]).toBe(`${size}px`);
      expect(appearanceTokens({ ...DEFAULT_PREFS, prose: size, scale: "xl" }, dark)["--fs-prose"]).toBe(`${size}px`);
    }
    expect(appearanceTokens(DEFAULT_PREFS, dark)["--fs-prose"]).toBeUndefined();
    expect(usualProse("md", false)).toBe(15);
    expect(usualProse("md", true)).toBe(16);
    expect(usualProse("xl", false)).toBe(17.7);
  });

  it("carries the old answer-text choices over to pixel sizes", () => {
    const kept = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => kept.get(key) ?? null, setItem: (key: string, value: string) => void kept.set(key, value), removeItem: (key: string) => void kept.delete(key) });
    const read = (prose: unknown) => {
      localStorage.setItem("daedalus.appearance", JSON.stringify({ prose }));
      return readPrefs().prose;
    };
    expect(read("17")).toBe(17);
    expect(read("19")).toBe(19);
    expect(read("auto")).toBeNull();
    expect(read(13)).toBe(13);
    expect(read(40)).toBeNull();
    expect(read(undefined)).toBeNull();
  });

  afterEach(() => vi.unstubAllGlobals());
});
