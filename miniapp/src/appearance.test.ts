import { afterEach, describe, expect, it, vi } from "vitest";
import { COLUMN_WIDTH, DARK_ACCENTS, DEFAULT_PREFS, FONT_CATALOG, LIGHT_ACCENTS, THEMES, appearanceTokens, contrast, fontUrlAllowed, googleStylesheet, isThemeId, readPrefs, resolvedTheme, themeById, usualProse, type Column } from "./appearance";

const dark = { scheme: "dark" as const, prefersDark: true };
const light = { scheme: "light" as const, prefersDark: false };

describe("themes", () => {
  it("keeps every text step readable on every surface of every theme", () => {
    // 4.5:1 for the text, the secondary and the meta on the frame, the page, a card, what floats and
    // the solid fill's first step; the translucent hover and selection only lighten or darken these.
    for (const theme of THEMES) {
      for (const ink of [theme.fg, theme.fg2, theme.fg3]) {
        for (const ground of [theme.side, theme.wash, theme.card, theme.surface, theme.surface2, theme.surface3, theme.inset]) {
          expect(contrast(ink, ground), `${theme.id}: ${ink} on ${ground}`).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
  });

  it("keeps the accents readable on every surface and their labels readable on them", () => {
    for (const theme of THEMES) {
      const accents = theme.light ? LIGHT_ACCENTS : DARK_ACCENTS;
      for (const colour of [accents.accent, accents.live, accents.attention]) {
        for (const ground of [theme.side, theme.wash, theme.card, theme.surface]) {
          expect(contrast(colour, ground), `${theme.id}: ${colour} on ${ground}`).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
    for (const accents of [DARK_ACCENTS, LIGHT_ACCENTS]) {
      expect(contrast(accents.primaryInk, accents.primary)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(accents.primaryInk, accents.primaryHover)).toBeGreaterThanOrEqual(4.5);
      expect(contrast(accents.accentInk, accents.accent)).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("is six neutral frames that never write an accent of their own", () => {
    // A theme that recoloured violet, or painted a gradient, broke the one meaning each accent has.
    expect(THEMES.map((theme) => theme.id)).toEqual(["ink", "paper", "sazha", "grafit", "slanets", "pepel"]);
    for (const theme of THEMES) {
      for (const [key, value] of Object.entries(theme)) if (key !== "id" && key !== "light") expect(value, `${theme.id}.${key}`).toMatch(/^#[0-9a-f]{6}$/);
      const tokens = appearanceTokens({ ...DEFAULT_PREFS, follow: false, theme: theme.id, colors: {} }, dark);
      const accents = theme.light ? LIGHT_ACCENTS : DARK_ACCENTS;
      expect(tokens["--accent"]).toBe(accents.accent);
      expect(tokens["--primary"]).toBe(accents.primary);
      expect(tokens["--ok"]).toBe(accents.live);
      expect(tokens["--bad"]).toBe(accents.attention);
    }
  });

  it("resolves follow-system to the dark and light pair, and a chosen theme when follow is off", () => {
    const prefs = { ...DEFAULT_PREFS, colors: {} };
    expect(resolvedTheme(prefs, dark).id).toBe("ink");
    expect(resolvedTheme(prefs, light).id).toBe("paper");
    expect(resolvedTheme({ ...prefs, follow: false, theme: "slanets" }, dark).id).toBe("slanets");
    expect(themeById("nope").id).toBe("ink");
    expect(isThemeId("grafit")).toBe(true);
    expect(isThemeId("malibu")).toBe(false);
  });

  it("lets a colour override replace the theme's frame, and keeps the terminal dark when asked", () => {
    const prefs = { ...DEFAULT_PREFS, follow: false, theme: "paper" as const, colors: { bg: "#112233" } };
    const tokens = appearanceTokens(prefs, light);
    expect(tokens["--bg"]).toBe("#112233");
    expect(tokens["--ansi-0"]).toMatch(/^#[0-9a-f]{6}$/i);
    const forced = appearanceTokens({ ...prefs, colors: {}, code: "dark" }, light);
    expect(forced["--term-bg"]).toBe("#101010");
    expect(forced["--term-fg"]).toBe("#ededed");
    expect(Object.keys(forced).filter((name) => name.startsWith("--ansi-"))).toHaveLength(16);
  });

  it("leaves the type scale alone at the usual step and moves it otherwise", () => {
    expect(appearanceTokens(DEFAULT_PREFS, dark)["--fs-14"]).toBeUndefined();
    expect(appearanceTokens({ ...DEFAULT_PREFS, scale: "lg" }, dark)["--fs-14"]).toBe("15.4px");
    expect(appearanceTokens({ ...DEFAULT_PREFS, scale: "sm" }, dark)["--fs-12"]).toBe("11px");
  });

  it("draws the conversation at 720, 840, 960 or the whole room, and writes nothing for the usual 840", () => {
    const stripe = (column: Column) => appearanceTokens({ ...DEFAULT_PREFS, column }, dark);
    expect(stripe("normal")["--chat-w"]).toBeUndefined();
    expect(stripe("narrow")["--chat-w"]).toBe("720px");
    expect(stripe("wide")["--chat-w"]).toBe("960px");
    expect(stripe("full")["--chat-w"]).toBe("1400px");
    // Only the whole-room choice moves the side padding, to 32 px.
    expect(stripe("full")["--chat-pad"]).toBe("32px");
    for (const column of ["narrow", "normal", "wide"] as const) expect(stripe(column)["--chat-pad"]).toBeUndefined();
    expect(COLUMN_WIDTH.normal).toBe(840);
  });

  it("reads a stored narrow, usual or wide choice as the same name at its new width, and anything else as the usual", () => {
    const kept = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => kept.get(key) ?? null, setItem: (key: string, value: string) => void kept.set(key, value), removeItem: (key: string) => void kept.delete(key) });
    const read = (column: unknown) => {
      localStorage.setItem("daedalus.appearance", JSON.stringify({ column }));
      return readPrefs().column;
    };
    expect(["narrow", "normal", "wide", "full"].map(read)).toEqual(["narrow", "normal", "wide", "full"]);
    expect(read("huge")).toBe("normal");
    expect(read(undefined)).toBe("normal");
    vi.unstubAllGlobals();
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

  it("drops a stored theme it does not know, a deleted one included, and an accent override", () => {
    const kept = new Map<string, string>();
    vi.stubGlobal("localStorage", { getItem: (key: string) => kept.get(key) ?? null, setItem: (key: string, value: string) => void kept.set(key, value), removeItem: (key: string) => void kept.delete(key) });
    localStorage.setItem("daedalus.appearance", JSON.stringify({ theme: "cheek", follow: false, colors: { bg: "red", accent: "#ff0000", send: "#00ff00", fg: "#eeeeee" } }));
    const prefs = readPrefs();
    expect(prefs.theme).toBe("ink");
    expect(prefs.follow).toBe(false);
    expect(prefs.colors).toEqual({ fg: "#eeeeee" });
    localStorage.setItem("daedalus.appearance", JSON.stringify({ theme: "malibu", dark: "glub", light: "zakat", scale: "lg" }));
    expect(readPrefs()).toMatchObject({ theme: "ink", dark: "ink", light: "paper", scale: "lg" });
    // A theme stored on the wrong side of the pair would paint the dark scheme light.
    localStorage.setItem("daedalus.appearance", JSON.stringify({ dark: "paper", light: "grafit" }));
    expect(readPrefs()).toMatchObject({ dark: "ink", light: "paper" });
    localStorage.setItem("daedalus.appearance", JSON.stringify({ dark: "pepel" }));
    expect(readPrefs().dark).toBe("pepel");
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
