// The token stylesheet is a published contract as well as the app's palette: pages read it through
// /pages/_kit/kit.css and the bridge, and the page guide names the tokens a page may rely on. These
// claims keep the names alive whatever their values become, and keep the three accents meaning one
// thing each in both schemes.

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import tokens from "./tokens.css?raw";
import reference from "../../../skills/daedalus-pages/reference.md?raw";
import { DARK_ACCENTS, LIGHT_ACCENTS, THEMES } from "../appearance";

const text = tokens.replace(/\/\*[\s\S]*?\*\//g, "");

/** The declarations of the first block whose selector is exactly `head`, as name → value. */
function block(head: string): Record<string, string> {
  const at = text.indexOf(`${head} {`);
  expect(at, head).toBeGreaterThanOrEqual(0);
  const body = text.slice(text.indexOf("{", at) + 1, text.indexOf("\n}", at));
  return Object.fromEntries([...body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

const dark = block(":root");
const light = block(':root[data-scheme="light"]');

/** Every token the page guide lists under "Tokens, usable in any style of the page", ranges expanded. */
function documented(): string[] {
  const section = reference.slice(reference.indexOf("Tokens, usable"), reference.indexOf("## The bridge"));
  const names = new Set<string>();
  for (const m of section.matchAll(/`(--[a-z0-9-]+)`(?:\s*…\s*`(--[a-z]+-)(\d+)`)?/g)) {
    names.add(m[1]);
    if (m[2]) {
      const from = Number(/(\d+)$/.exec(m[1])?.[1]);
      for (let n = from; n <= Number(m[3]); n += 1) if (`${m[2]}${n}` in dark) names.add(`${m[2]}${n}`);
    }
  }
  return [...names];
}

describe("the published tokens", () => {
  it("declares every token the page guide promises", () => {
    // A page written against the guide breaks silently when one of these goes: its rule falls back
    // to nothing. Values may change (the redesign changed most of them); the names may not.
    const names = documented();
    expect(names.length).toBeGreaterThan(30);
    for (const name of ["--bg", "--fg", "--accent", "--primary", "--ok", "--bad", "--font", "--mono", "--fs-11", "--fs-22", "--space-8", "--radius-pill", "--row-h", "--head-h"]) expect(names).toContain(name);
    for (const name of names) expect(dark, name).toHaveProperty(name);
  });

  it("lets the text colour fall through to Telegram's, in both schemes", () => {
    expect(dark["--fg"]).toMatch(/^var\(--tg-theme-text-color, #[0-9a-f]{6}\)$/);
    expect(light["--fg"]).toMatch(/^var\(--tg-theme-text-color, #[0-9a-f]{6}\)$/);
  });

  it("keeps --fs-11 at the 12 px floor", () => {
    expect(dark["--fs-11"]).toBe("12px");
    expect(dark["--fs-12"]).toBe("12px");
  });
});

describe("the three accents", () => {
  // Violet is what is chosen and the primary action, cyan is running and added, orange needs the
  // operator and was removed. The status names are aliases of the last two, so a published page's
  // "ok" and "bad" speak the same language as the app.
  for (const [scheme, sheet, accents] of [["dark", dark, DARK_ACCENTS], ["light", light, LIGHT_ACCENTS]] as const) {
    it(`mean one thing each on the ${scheme} scheme, and the stylesheet and the theme writer agree`, () => {
      expect(sheet["--accent"]).toBe(accents.accent);
      expect(sheet["--primary"]).toBe(accents.primary);
      expect(sheet["--primary-hover"]).toBe(accents.primaryHover);
      expect(sheet["--live"]).toBe(accents.live);
      expect(sheet["--attention"]).toBe(accents.attention);
      for (const name of ["--ok", "--info"]) expect(sheet[name], name).toBe(accents.live);
      for (const name of ["--warn", "--bad"]) expect(sheet[name], name).toBe(accents.attention);
    });
  }

  it("are written as hex beside the names that alias them, for the terminal", () => {
    for (const sheet of [dark, light]) for (const name of ["--live", "--attention", "--accent", "--primary"]) expect(sheet[name], name).toMatch(/^#[0-9a-f]{6}$/);
  });

  it("draw states from the accents, and no dot glows", () => {
    expect(dark["--state-working"]).toBe("var(--live)");
    expect(dark["--state-waiting"]).toBe("var(--attention)");
    expect(dark["--state-failed"]).toBe("var(--attention)");
    expect(dark["--ring"]).toBe("var(--accent)");
    expect(text).not.toContain("--state-glow");
  });
});

describe("the shadows", () => {
  it("are one floating shadow, and never `none`, which would void a list they are written into", () => {
    // `box-shadow: inset 0 2px 0 var(--warn), var(--drop-xs)` is dropped whole when the drop is `none`:
    // a host terminal's card lost its amber edge that way.
    for (const sheet of [dark, light]) for (const [name, value] of Object.entries(sheet)) if (/^--(shadow|drop)-/.test(name)) expect(value, name).not.toBe("none");
    expect(dark["--shadow-lg"]).toBe("var(--shadow-md)");
    expect(dark["--shadow-xl"]).toBe("var(--shadow-md)");
    expect(dark["--shadow-pop"]).toBe("var(--shadow-md)");
  });
});

describe("the default theme and the stylesheet", () => {
  it("paint the same frame, so the first paint before the theme is written does not flash", () => {
    const ink = THEMES.find((theme) => theme.id === "ink")!;
    const paper = THEMES.find((theme) => theme.id === "paper")!;
    for (const [theme, sheet] of [[ink, dark], [paper, light]] as const) {
      expect(sheet["--bg"]).toBe(theme.side);
      expect(sheet["--wash"] ?? dark["--wash"]).toBe(theme.wash);
      expect(sheet["--card"]).toBe(theme.card);
      expect(sheet["--surface-1"]).toBe(theme.surface);
      expect(sheet["--inset"]).toBe(theme.inset);
      expect(sheet["--fg-2"]).toBe(theme.fg2);
      expect(sheet["--fg-3"]).toBe(theme.fg3);
    }
  });
});
