// The stylesheet is read as a table of sizes, the way i18n.test.ts reads the dictionary as a table
// of words. The app once grew to 15 px body text, 44 px round buttons and 64 px session rows one
// rule at a time, each of them reasonable on its own; this is what stops the next one.
//
// Four claims, and Settings' own column and row. Every size comes from the scale in :root. Nothing outside the answer, the headings
// and the prose is drawn above 14 px, in whatever unit it is written. No box that is not a surface
// of its own is given a height a row or a control would not have. Rows and controls take their
// height from the row and control tokens, so a phone gets its 44 px from one media rule and not
// from forty.
//
// The three functions below are the guard; the claims run them over the real stylesheet, and the
// last describe runs them over a sheet of planted regressions, because a guard that is never shown
// a failure is a guard nobody has checked.

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import legacy from "./ui/styles.css?raw";
import tokens from "./ui/tokens.css?raw";
import desktop from "./ui/desktop.css?raw";
import petHost from "./pethost.tsx?raw";
const css = tokens + "\n" + legacy.replace(/@import[^;]+;/g, "") + "\n" + desktop;

type Rule = { selector: string; media: string; body: string };

/** Every rule with the media query it sits in; comments stripped first, and one space after every colon
 *  in a declaration, so the two spacing styles the file is written in read as one. */
function rules(source: string): Rule[] {
  const text = source.replace(/\/\*[\s\S]*?\*\//g, "");
  const out: Rule[] = [];
  const media: string[] = [];
  let i = 0;
  let start = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "{") {
      const head = text.slice(start, i).trim();
      if (head.startsWith("@")) {
        media.push(head);
        i += 1;
        start = i;
        continue;
      }
      const end = text.indexOf("}", i);
      out.push({ selector: head, media: media.join(" "), body: text.slice(i + 1, end).replace(/\s*:\s*/g, ": ") });
      i = end + 1;
      start = i;
      continue;
    }
    if (ch === "}") {
      media.pop();
      i += 1;
      start = i;
      continue;
    }
    i += 1;
  }
  return out;
}

const TOKENS: Record<string, number> = { "--fs-11": 11, "--fs-12": 12, "--fs-13": 13, "--fs-14": 14, "--fs-15": 15, "--fs-16": 16, "--fs-18": 18, "--fs-20": 20, "--fs-22": 22, "--fs-mono": 12.5, "--fs-prose": 15 };

/** The document never re-sizes its root, so a rem is the browser's own step and an em is the 14 px body. */
const ROOT_PX = 16;
const BODY_PX = 14;

/** The pixels a font-size stands for: null only where the rule states no size of its own, and
 *  Infinity — a finding, quoted back with its value — where the unit cannot be read at all. A size
 *  the guard does not understand is the one that gets through it. */
function pixels(value: string): number | null {
  const v = value.trim();
  if (v === "inherit" || v === "unset" || v === "initial" || v === "revert") return null;
  // The companion slider only shrinks; a scaled font cannot exceed its named step.
  const petScale = /^calc\(var\((--fs-[a-z0-9]+)\)\s*\*\s*var\(--pet-scale\)\)$/.exec(v);
  if (petScale) return TOKENS[petScale[1]] ?? Number.POSITIVE_INFINITY;
  const token = /^var\((--fs-[a-z0-9]+)\)/.exec(v);
  if (token) return TOKENS[token[1]] ?? Number.POSITIVE_INFINITY;
  const clamp = /^clamp\(\s*([\d.]+)px/.exec(v);
  if (clamp) return Number(clamp[1]);
  const px = /^(\d*\.?\d+)px/.exec(v);
  if (px) return Number(px[1]);
  const rem = /^(\d*\.?\d+)rem/.exec(v);
  if (rem) return Number(rem[1]) * ROOT_PX;
  const em = /^(\d*\.?\d+)em/.exec(v);
  if (em) return Number(em[1]) * BODY_PX;
  const percent = /^(\d*\.?\d+)%/.exec(v);
  if (percent) return (Number(percent[1]) / 100) * BODY_PX;
  return Number.POSITIVE_INFINITY;
}

/** The selectors a rule really applies to. A rule heads a list, and an allowed selector in that list
 *  says nothing about the ones beside it. */
function selectors(rule: Rule): string[] {
  return rule.selector.split(",").map((part) => part.replace(/\s+/g, " ").trim()).filter(Boolean);
}

/** Whether one selector is the allowed one, a descendant of it, or something under it — anchored at a
 *  boundary either way, so `.probe-grouped-huge` is not covered by `.answer` and `.h1nt` is not by `h1`. */
function covered(selector: string, allowed: string[]): boolean {
  return allowed.some((sel) => {
    if (sel.endsWith("-")) return selector.split(/[\s>+~]+/).some((compound) => compound.startsWith(sel));
    if (selector === sel) return true;
    if (selector.endsWith(` ${sel}`)) return true;
    return selector.startsWith(sel) && /[\s>+~.:[]/.test(selector.slice(sel.length, sel.length + 1));
  });
}

/** Where text is allowed to be larger than the UI: running prose, headings, numbers meant to be read from across a room, and glyphs. */
const LARGE_ALLOWED = [".answer", ".summary-body", ".preview-doc", ".composer-box textarea", "h1", "h2", "h3", ".kpi .value", ".empty b", ".stat b", ".step-head b", ".dropzone", ".attachment-glyph", ".btn.big", ".chev", ".voice-", ".orb", ".onboard-head", ".login"];

/** Surfaces that are meant to be large: a screen, a dialog, a stage, an empty state, a bar that holds
 *  rows rather than being one. Everything else is a row, a control or an avatar, and is held to the tokens. */
const BOX_ALLOWED = [
  ".screen", ".sheet", ".dialog", ".gate", ".login", ".login-widget", ".empty", ".empty.calm", ".sidebar .empty", ".toast",
  ".chat-scroll", ".chat-head", ".composer", ".composer-box", ".pagehead", ".panel-body", ".panel-tabs", ".panel-toolbar",
  ".tabbar", ".thought", ".attachment-open", ".attachment.image .attachment-open", ".img-loading", ".preview-body",
  ".kanban-col", ".kanban-empty", ".voice-", ".addmodel", ".addmodel-foot", ".more-item",
  // The companion's canvas is a scene rather than a row or control; its measured height is the
  // viewport for a full 3D pose, and the phone version deliberately uses a smaller viewport.
  ".pet-host", ".pet-figure",
];

/** The height a box is given outright, in px, or 0 where it is a token, a calc or a proportion. */
const BOX_LIMIT = 40;

/** Content with an intentional height: two-line file cards and search hits, a message placeholder,
 *  and the document viewport. Keep their limits explicit so larger boxes still fail the guard. */
const CONTENT_BOXES = [
  { selector: ".artifact", property: "min-height", max: 48 },
  { selector: ".artifact-main", property: "min-height", max: 46 },
  { selector: ".turn-skeleton .sk-user", property: "height", max: 40 },
  { selector: ".files .tree-row.grep-row", property: "height", max: 48 },
  { selector: ".html-frame", property: "min-height", max: 400 },
  // Not a box but the shade a sheet raises over its sticky footer while more waits below; shorter
  // than this it fell into the gap above the footer and said nothing.
  { selector: '.sheet-body[data-more="below"] .sheet-foot::before', property: "height", max: 56 },
];

/** Top and bottom padding of a shorthand, in px; a component that is not a plain length counts as its first literal. */
function verticalPadding(value: string): number {
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const ch of value.trim()) {
    if (ch === "(") depth += 1;
    if (ch === ")") depth -= 1;
    if (/\s/.test(ch) && depth === 0) {
      if (current) parts.push(current);
      current = "";
      continue;
    }
    current += ch;
  }
  if (current) parts.push(current);
  const px = (part: string) => Number(/(\d*\.?\d+)px/.exec(part)?.[1] ?? 0);
  if (parts.length === 0) return 0;
  if (parts.length < 3) return px(parts[0]) * 2;
  return px(parts[0]) + px(parts[2]);
}

function oversizeFonts(all: Rule[]): string[] {
  const large: string[] = [];
  for (const r of all) {
    // A text field on a touch screen is 16 px on purpose: the alternative is Safari zooming the page.
    if (r.media.includes("hover: none")) continue;
    for (const m of r.body.matchAll(/(?:^|;)\s*font(?:-size)?\s*:\s*([^;]+)/g)) {
      const size = pixels(m[1]);
      if (size === null || size <= 14) continue;
      for (const selector of selectors(r)) {
        if (covered(selector, LARGE_ALLOWED)) continue;
        large.push(`${selector} { font-size: ${m[1].trim()} }`);
      }
    }
  }
  return large;
}

function numericFonts(all: Rule[]): string[] {
  // A size written as a number is a size the scale cannot move. Two are allowed: the phone's
  // 16 px field, and the 10 px beta tag, which sits below the scale on purpose.
  const numeric: string[] = [];
  for (const r of all) {
    if (r.media.includes("hover: none")) continue;
    for (const m of r.body.matchAll(/(?:^|;)\s*font-size\s*:\s*([\d.]+px)/g)) {
      if (m[1] === "10px") continue;
      for (const selector of selectors(r)) {
        if (covered(selector, LARGE_ALLOWED)) continue;
        numeric.push(`${selector} { font-size: ${m[1]} }`);
      }
    }
  }
  return numeric;
}

function oversizeBoxes(all: Rule[]): string[] {
  const fat: string[] = [];
  for (const r of all) {
    for (const m of r.body.matchAll(/(?:^|;)\s*(height|min-height|padding)\s*:\s*([^;]+)/g)) {
      const value = m[2].trim();
      const box = m[1] === "padding" ? verticalPadding(value) : Number(/^(\d*\.?\d+)px\s*$/.exec(value)?.[1] ?? 0);
      if (box < BOX_LIMIT) continue;
      for (const selector of selectors(r)) {
        if (covered(selector, BOX_ALLOWED)) continue;
        if (CONTENT_BOXES.some((entry) => selector === entry.selector && m[1] === entry.property && box <= entry.max)) continue;
        fat.push(`${selector} { ${m[1]}: ${value} }`);
      }
    }
  }
  return fat;
}

const all = rules(css);

describe("the scale", () => {
  it("keeps the companion slider at or below the normal size", () => {
    expect(petHost).toContain('min="60" max="100"');
    expect(petHost).toContain('value >= 0.6 && value <= 1');
  });
  it("is declared once, in :root", () => {
    const root = all.find((r) => r.selector === ":root" && !r.media);
    expect(root).toBeDefined();
    for (const name of [...Object.keys(TOKENS), "--space-2", "--radius-sm", "--radius-md", "--radius-lg", "--row-h", "--row-h-dense", "--row-h-touch", "--ctl-h", "--ctl-h-sm", "--ctl-h-lg", "--avatar", "--sidebar-w", "--rail-w", "--panel-w", "--pip-w", "--reading-w", "--chat-w", "--head-h"]) {
      expect(root!.body, name).toContain(`${name}:`);
    }
  });

  it("gives a phone its touch sizes from one media rule", () => {
    const touch = all.find((r) => r.selector === ":root" && r.media.includes("max-width: 1023px"));
    expect(touch?.body).toContain("--row-h: var(--row-h-touch)");
    expect(touch?.body).toContain("--fs-prose: var(--fs-16)");
  });

  it("sets the body at 14", () => {
    const body = all.find((r) => r.selector === "body" && !r.media);
    expect(body?.body).toContain("font-size: var(--fs-14)");
  });
});

describe("every size", () => {
  it("is at or under 14 px outside prose, headings and glyphs", () => {
    expect(oversizeFonts(all)).toEqual([]);
  });

  it("names a step of the scale rather than a number", () => {
    expect(numericFonts(all)).toEqual([]);
  });
});

describe("every box", () => {
  it("is a row's height, a control's height, or a surface that says it is one", () => {
    expect(oversizeBoxes(all)).toEqual([]);
  });
});

describe("project hierarchy", () => {
  it("gives multi-agent projects a full header and an always available creation action", () => {
    expect(css).toContain(".folder-head { display: flex;");
    expect(css).toContain("min-height: calc(var(--row-h-touch) + var(--space-3))");
    expect(css).toContain(".sidebar .folder-add");
  });
});

describe("rows and controls", () => {
  const decl = (selector: string) => all.filter((r) => r.selector === selector && !r.media).map((r) => r.body).join(" ");

  it("take their height from the tokens", () => {
    expect(decl(".erow")).toContain("min-height: var(--row-h)");
    expect(decl(".iconbtn")).toContain("width: var(--ctl-h)");
    expect(decl(".iconbtn")).toContain("height: var(--ctl-h)");
    expect(decl(".iconbtn.small")).toContain("var(--ctl-h-sm)");
    expect(decl(".btn")).toContain("min-height: var(--ctl-h)");
    expect(decl(".menu button")).toContain("min-height: var(--row-h)");
    expect(decl(".erow .avatar")).toContain("var(--avatar)");
  });

  it("give the terminal dock a dense tab row and the conversation's whole column", () => {
    // The tab row is a row like any other; the dock's own height is the operator's (dragged, then a
    // token). It is deliberately not held to the reading stripe: a terminal wants every column, and
    // the stripe is the rule for the conversation and its composer, which keep it above the dock.
    expect(decl(".term-bar")).toContain("height: var(--row-h-dense)");
    expect(decl(".term-dock")).toContain("height: var(--dock-h)");
    const wide = all.filter((r) => r.media.includes("min-width: 1024px")).map((r) => `${r.selector}{${r.body}}`).join("\n");
    expect(wide).not.toMatch(/\.term-dock[^{]*\{[^}]*--chat-w/);
    expect(decl(".term-dock")).not.toContain("--chat-w");
  });

  it("give a phone's terminal and project the thumb's sizes from the tokens, and the visible height", () => {
    // The keys row is a row of controls, and a banner's answers and a project's rows are touch rows:
    // on a phone the tokens make them 40 and 44 px, so nothing here states a size of its own. The
    // terminal is as tall as what the phone shows, which is what keeps its keys above the keyboard.
    expect(decl(".term-key")).toContain("height: var(--ctl-h)");
    expect(decl(".term-phone")).toContain("height: var(--vh, 100dvh)");
    expect(decl(".ask-answers-row .btn")).toContain("min-height: var(--row-h-touch)");
    expect(decl(".phone-staff-row")).toContain("min-height: calc(var(--row-h-touch)");
    expect(decl(".tabbar.project-tabs")).toContain("position: static");
  });

  it("gives the rail and the sidebar the first column together, and the rail's icons a row's size", () => {
    // The rail replaced the sidebar's folded strip, so the column math changed: the first column is
    // the rail plus the sidebar, and folding sets the sidebar's share to 0 rather than to a strip's
    // 48 px. The conversation's stripe is untouched; the rail takes 4 px more than the strip did.
    const wide = all.filter((r) => r.media.includes("min-width: 1024px"));
    const body = (sel: string) => wide.filter((r) => selectors(r).includes(sel)).map((r) => r.body).join(";");
    expect(body(".app")).toContain("grid-template-columns: calc(var(--rail-w) + var(--sidebar-w)) minmax(0, 1fr)");
    expect(body(".sidebar")).toContain("left: var(--rail-w)");
    expect(body(".rail")).toContain("width: var(--rail-w)");
    expect(body(".rail-item")).toContain("height: var(--row-h)");
    // A drag writes the width once a frame; a transition on it is what made the edge lag and glide.
    expect(body(".sidebar")).not.toMatch(/transition/);
  });

  it("put the browser's corner preview inside the conversation's column, at its token width", () => {
    // Absolute inside `.chat-main`, never fixed to the page: that is what keeps it left of the panel
    // and inside its own pane of a dual view. check_density.py measures it at 1440 and 1280.
    expect(decl(".bp-pip")).toContain("position: absolute");
    expect(decl(".bp-pip")).toContain("width: var(--pip-w)");
    expect(decl(".bp-pip")).not.toMatch(/position: fixed/);
    expect(decl(".bp-pip-foot")).toContain("height: 24px");
    // The phone's header thumbnail is a control, not a picture: 28 × 18 inside an icon button.
    expect(decl(".browser-headbtn-thumb")).toContain("width: 28px");
    expect(decl(".browser-headbtn-thumb")).toContain("height: 18px");
  });

  it("fold a step of the run into 28 px", () => {
    expect(decl(".act")).toContain("min-height: 28px");
  });

  it("keeps the conversation in one column with the field that answers it", () => {
    // The defect this guards against is an offset: prose on one axis, the composer on another.
    // Both take the stripe, and nothing in a turn is capped below it.
    const wide = all.filter((r) => r.media.includes("min-width: 1024px")).map((r) => `${r.selector}{${r.body}}`).join("\n");
    expect(wide).toMatch(/\.timeline, \.composer-box\{[^}]*var\(--chat-w\)/);
    expect(wide).toMatch(/\.composer-box,[^{]*\{[^}]*var\(--chat-w\)/);
    expect(wide).toMatch(/\.answer[^{]*\{[^}]*max-width: 100%/);
    expect(wide).not.toMatch(/\.answer[^{]*\{[^}]*max-width: var\(--reading-w\)/);
  });

  it("widens the stripe on a wide window rather than leaving margin", () => {
    // A 2560 screen should read wider than a 1440 one; the owner's standing complaint is narrowness.
    const stripes = all.filter((r) => r.selector === ":root" && /--chat-w/.test(r.body));
    const widths = stripes.map((r) => Number(/--chat-w:\s*(\d+)px/.exec(r.body)?.[1] ?? 0));
    expect(widths.length).toBeGreaterThan(1);
    expect(Math.max(...widths)).toBeGreaterThan(Math.min(...widths));
  });
});

describe("settings", () => {
  const decl = (selector: string) => all.filter((r) => r.selector === selector && !r.media).map((r) => r.body).join(" ");
  const wide = (selector: string) => all.filter((r) => r.media.includes("min-width: 1024px") && selectors(r).includes(selector)).map((r) => r.body).join(";");

  it("gives the section a 960 px column that grows with the window until then", () => {
    // Widened from 760 px on the owner's word that the column was too narrow: at 960 a row's words
    // and its control share one line with a lane to spare. The rail stays 280 px; the gain is content.
    expect(wide(".settings-col")).toContain("width: min(960px, calc(100% - 96px))");
    expect(wide(".settings-stage")).toContain("grid-template-columns: 280px minmax(0, 1fr)");
  });

  it("draws every setting as a touch-height row with its control in one lane", () => {
    // One row shape for every scalar setting (settingsrow.tsx), its height the touch token and not a
    // number of its own, and the compact picker a small control rather than a full-width field.
    expect(decl(".settings-row")).toContain("min-height: var(--row-h-touch)");
    expect(decl(".settings-row")).toContain("grid-template-columns: minmax(0, 1fr) auto");
    expect(decl(".dropdown-btn")).toContain("min-height: var(--ctl-h-sm)");
    expect(decl(".dropdown-item")).toContain("min-height: var(--row-h)");
  });
});

describe("the guard itself", () => {
  it("allows content at its own height but still catches growth and oversized neighbours", () => {
    for (const { selector, property, max } of CONTENT_BOXES) {
      expect(oversizeBoxes(rules(`${selector} { ${property}: ${max}px; }`))).toEqual([]);
      expect(oversizeBoxes(rules(`${selector} { ${property}: ${max + 1}px; }`))).toEqual([`${selector} { ${property}: ${max + 1}px }`]);
      expect(oversizeBoxes(rules(`${selector}, .probe-content-neighbour { ${property}: ${max}px; }`))).toEqual([`.probe-content-neighbour { ${property}: ${max}px }`]);
    }
  });

  // Five regressions of exactly the kind this file exists to stop. Each of them once passed.
  const planted = rules(`
    .probe-rem { font-size: 1.75rem; }
    .probe-em { font-size: 2em; }
    .answer, .probe-grouped-huge { font-size: 28px; }
    .probe-fat-row { min-height: 64px; padding: 20px; }
    .probe-fat-btn { height: 48px; width: 48px; }
  `);

  it("reads a size written in rem or em", () => {
    expect(oversizeFonts(planted)).toContain(".probe-rem { font-size: 1.75rem }");
    expect(oversizeFonts(planted)).toContain(".probe-em { font-size: 2em }");
  });

  it("does not let a selector ride into a group behind an allowed one", () => {
    expect(oversizeFonts(planted)).toContain(".probe-grouped-huge { font-size: 28px }");
    expect(oversizeFonts(planted)).not.toContain(".answer { font-size: 28px }");
    expect(numericFonts(planted)).toContain(".probe-grouped-huge { font-size: 28px }");
  });

  it("sees a row and a control that grew a box of their own", () => {
    expect(oversizeBoxes(planted)).toContain(".probe-fat-row { min-height: 64px }");
    expect(oversizeBoxes(planted)).toContain(".probe-fat-row { padding: 20px }");
    expect(oversizeBoxes(planted)).toContain(".probe-fat-btn { height: 48px }");
  });

  it("reads a size it does not understand as a finding rather than as nothing", () => {
    expect(oversizeFonts(rules(".probe-odd { font-size: 3vmax; }"))).toEqual([".probe-odd { font-size: 3vmax }"]);
  });
});
