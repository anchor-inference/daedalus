// The stylesheet is read as a table of sizes, the way i18n.test.ts reads the dictionary as a table
// of words. The app once grew to 15 px body text, 44 px round buttons and 64 px session rows one
// rule at a time, each of them reasonable on its own; this is what stops the next one.
//
// Four claims for the desktop, and Settings' own column and row. Every size comes from the scale in
// :root. Nothing outside the answer, the headings and the prose is drawn above 14 px on a desktop, in
// whatever unit it is written. No box that is not a surface of its own is given a height a row or a
// control would not have. Rows and controls take their height from the row and control tokens, so a
// phone gets its 44 px from one media rule and not from forty.
//
// The phone has its own scale since the redesign that took the chat apps on the operator's phone as
// its reference: six steps, 12 / 13 / 14 / 15 / 16 / 17, and nothing else below 1024 px. A 14 px cap
// there kept list titles and fields smaller than every app on the same phone, which is what the
// operator measured against; the first cut of the redesign (12 / 13 / 15 / 16 / 17 / 22, 60 px rows)
// then went the other way, and the operator measured the phone as too large next to Claude and
// ChatGPT on the same device, so it came down a step. A rule inside the phone media query is held to
// the six steps instead of to the cap, the phone's :root block must move every desktop step onto one
// of them, and the phone's rows, top bar, chips and composer must take their sizes from the phone
// tokens (54 px rows, 44 with one line, a 52 px bar, a 44 px idle composer, 28 px chips, 44 px
// targets). check_density.py measures the same claims on a drawn page.
//
// The three functions below are the guard; the claims run them over the real stylesheet, and the
// last describe runs them over a sheet of planted regressions, because a guard that is never shown
// a failure is a guard nobody has checked.

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import legacy from "./ui/styles.css?raw";
import tokens from "./ui/tokens.css?raw";
import desktop from "./ui/desktop.css?raw";
import phone from "./ui/phone.css?raw";
import browserPhone from "./browser/phone.css?raw";
import petHost from "./pethost.tsx?raw";
// The browser's phone sheet keeps its looks beside its component; it answers to the same scale.
const css = tokens + "\n" + legacy.replace(/@import[^;]+;/g, "") + "\n" + desktop + "\n" + phone + "\n" + browserPhone;

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

const TOKENS: Record<string, number> = { "--fs-11": 11, "--fs-12": 12, "--fs-13": 13, "--fs-14": 14, "--fs-15": 15, "--fs-16": 16, "--fs-17": 17, "--fs-18": 18, "--fs-20": 20, "--fs-22": 22, "--fs-mono": 12.5, "--fs-prose": 15 };

/** The phone's six steps. */
const PHONE_STEPS = [12, 13, 14, 15, 16, 17];
const PHONE = "max-width: 1023px";

/** The document never re-sizes its root, so a rem is the browser's own step and an em is the 14 px body. */
const ROOT_PX = 16;
const BODY_PX = 14;

/** The pixels a font-size stands for: null only where the rule states no size of its own, and
 *  Infinity — a finding, quoted back with its value — where the unit cannot be read at all. A size
 *  the guard does not understand is the one that gets through it. */
function pixels(value: string, tokens: Record<string, number> = TOKENS): number | null {
  const v = value.trim();
  if (v === "inherit" || v === "unset" || v === "initial" || v === "revert") return null;
  // The companion slider only shrinks; a scaled font cannot exceed its named step.
  const petScale = /^calc\(var\((--fs-[a-z0-9]+)\)\s*\*\s*var\(--pet-scale\)\)$/.exec(v);
  if (petScale) return tokens[petScale[1]] ?? Number.POSITIVE_INFINITY;
  const token = /^var\((--fs-[a-z0-9]+)\)/.exec(v);
  if (token) return tokens[token[1]] ?? Number.POSITIVE_INFINITY;
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

/** On a phone the 44 px target is the floor and a list row is at most 60 px (two lines are 54; a few
 *  taller rows of their own, such as an Inbox entry with answers, say so), so a phone rule may state a
 *  box up to that; above it only the phone's own surfaces: the empty state, the attachment tiles of the
 *  + sheet and the home's greeting, each a block that holds content rather than being a row. */
const PHONE_BOX_LIMIT = 61;
// The browser list's rows carry a live thumbnail, and the give-back note is a field for a sentence or
// two: both are taller than a text row on purpose.
const PHONE_SURFACES = [".ph-empty", ".ph-tile", ".ph-hero", ".bp-list-open", ".bp-give-note"];

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
    // The phone's rules answer to the phone's scale (offScale), not to the desktop's cap.
    if (r.media.includes(PHONE)) continue;
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

/** The token table a phone resolves: the desktop's, with the phone's :root block laid over it. */
function phoneTokens(all: Rule[]): Record<string, number> {
  const out = { ...TOKENS };
  for (const r of all) {
    if (r.selector !== ":root" || !r.media.includes(PHONE)) continue;
    for (const m of r.body.matchAll(/(--fs-[a-z0-9]+)\s*:\s*([^;]+)/g)) {
      const value = m[2].trim();
      const px = /^(\d*\.?\d+)px$/.exec(value);
      const ref = /^var\((--fs-[a-z0-9]+)\)$/.exec(value);
      out[m[1]] = px ? Number(px[1]) : ref ? out[ref[1]] ?? Number.POSITIVE_INFINITY : Number.POSITIVE_INFINITY;
    }
  }
  return out;
}

/** Every size a phone rule states that is not one of the six steps, read with the phone's tokens. A
 *  size the guard cannot read is a finding here too. */
function offScale(all: Rule[]): string[] {
  const tokens = phoneTokens(all);
  const off: string[] = [];
  for (const r of all) {
    if (!r.media.includes(PHONE) || r.selector === ":root" || r.selector.startsWith(":root[")) continue;
    for (const m of r.body.matchAll(/(?:^|;)\s*font(?:-size)?\s*:\s*([^;]+)/g)) {
      const size = pixels(m[1], tokens);
      if (size === null || PHONE_STEPS.includes(size)) continue;
      for (const selector of selectors(r)) off.push(`${selector} { font-size: ${m[1].trim()} }`);
    }
  }
  return off;
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
      const phone = r.media.includes(PHONE);
      if (box < (phone ? PHONE_BOX_LIMIT : BOX_LIMIT)) continue;
      for (const selector of selectors(r)) {
        if (covered(selector, BOX_ALLOWED)) continue;
        if (phone && covered(selector, PHONE_SURFACES)) continue;
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
    for (const name of [...Object.keys(TOKENS), "--space-2", "--radius-sm", "--radius-md", "--radius-lg", "--row-h", "--row-h-2", "--row-h-kid", "--row-h-dense", "--row-h-touch", "--ctl-h", "--ctl-h-sm", "--chip-h-dense", "--ctl-h-lg", "--avatar", "--plate", "--sidebar-w", "--rail-w", "--panel-w", "--pip-w", "--reading-w", "--chat-w", "--head-h",
      "--top-h", "--list-row-h", "--list-row-h-1", "--nav-row-h", "--set-row-h", "--tabbar-h", "--ctl-round", "--chip-h", "--drawer-w", "--gutter", "--radius-xl", "--radius-composer"]) {
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
  it("is at or under 14 px on a desktop outside prose, headings and glyphs", () => {
    expect(oversizeFonts(all)).toEqual([]);
  });

  it("is one of the six phone steps in every phone rule", () => {
    expect(offScale(all)).toEqual([]);
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

describe("the phone", () => {
  const phoneRule = (selector: string) => all.filter((r) => r.media.includes(PHONE) && selectors(r).includes(selector)).map((r) => r.body).join(";");
  const rootPhone = all.filter((r) => r.selector === ":root" && r.media.includes(PHONE)).map((r) => r.body).join(";");

  it("moves every desktop step onto the six, once, in its :root block", () => {
    // The desktop's 11, 18, 20 and 22 have no place on the phone's scale; one block moves them to 12
    // and 17 so a rule written for the desktop lands on the scale without a phone copy of it. The 14
    // is on the scale since the phone came down a step, and stays.
    const tokens = phoneTokens(all);
    for (const [name, px] of Object.entries(tokens)) expect(PHONE_STEPS, `${name} is ${px}px on a phone`).toContain(px);
  });

  it("takes its bar, rows, targets and composer from the phone tokens", () => {
    // The geometry of the redesign, a step smaller since the operator measured it as too large next
    // to Claude and ChatGPT on the same phone (their single-line rows are 40 to 48 px): a 52 px top
    // bar with no rule under it, 54 px rows (44 with one line), 44 px targets for every glyph, 28 px
    // chips that reach 44 px to the finger, a 52 px project tab bar and an idle composer of one 44 px
    // row with 34 px circles.
    const declared = all.find((r) => r.selector === ":root" && !r.media)!.body;
    expect(declared).toContain("--top-h: 52px");
    expect(declared).toContain("--list-row-h: 54px");
    expect(declared).toContain("--list-row-h-1: 44px");
    expect(declared).toContain("--nav-row-h: 44px");
    expect(declared).toContain("--set-row-h: 48px");
    expect(declared).toContain("--chip-h: 28px");
    expect(declared).toContain("--ctl-round: 34px");
    expect(declared).toContain("--tabbar-h: 52px");
    expect(declared).toContain("--row-h-touch: 44px");
    expect(rootPhone).toContain("--composer-h: 44px");
    expect(rootPhone).toContain("--head-h: var(--top-h)");
    expect(phoneRule(".ph-top")).toContain("min-height: var(--top-h)");
    expect(phoneRule(".ph-top")).not.toMatch(/border/);
    expect(phoneRule(".pagehead-row")).toContain("min-height: var(--top-h)");
    expect(phoneRule(".pagehead")).toContain("backdrop-filter: none");
    expect(phoneRule(".ph-ib")).toContain("width: var(--tap)");
    expect(phoneRule(".ph-ib")).toContain("height: var(--tap)");
    expect(phoneRule(".ph-row")).toContain("min-height: var(--list-row-h)");
    expect(phoneRule(".ph-row.one")).toContain("min-height: var(--list-row-h-1)");
    expect(phoneRule(".ph-row")).not.toMatch(/border/);
    expect(phoneRule(".ph-chip")).toContain("height: var(--chip-h)");
    expect(phoneRule(".ph-chip::after")).toContain("inset: -8px 0");
    // The sheet's head keeps its height under a long body: shrinking, it slid its subtitle under the
    // model sheet's search field.
    expect(phoneRule(".sheet-head")).toContain("flex: none");
    expect(phoneRule(".ph-mrow")).toContain("min-height: var(--list-row-h-1)");
    expect(phoneRule(".ph-srow")).toContain("min-height: var(--set-row-h)");
    expect(phoneRule('.composer[data-shape="idle"] .composer-box')).toContain("min-height: var(--composer-h)");
    expect(phoneRule(".composer[data-shape] .composer-box")).toContain("border-radius: var(--radius-composer)");
    expect(phoneRule(".composer[data-shape] .roundbtn")).toContain("width: var(--ctl-round)");
    expect(phoneRule(".tabbar.project-tabs > a")).toContain("height: var(--tabbar-h)");
    expect(phoneRule(".ph-drawer")).toContain("width: var(--drawer-w)");
  });

  it("keeps every phone rule inside the phone media query, so a desktop never reads one", () => {
    const outside = rules(phone + "\n" + browserPhone).filter((r) => !r.media.includes(PHONE)).map((r) => r.selector);
    expect(outside).toEqual([]);
  });
});

describe("the sidebar", () => {
  const wide = (selector: string) => all.filter((r) => r.media.includes("min-width: 1024px") && selectors(r).includes(selector)).map((r) => r.body).join(";");
  const declared = () => all.find((r) => r.selector === ":root" && !r.media)!.body;

  it("is 288 px wide, with a chat's two lines at 44 px, a project's one at 36 and a nested chat at 40", () => {
    // 288 rather than 272 since the rows went to two lines: a Russian title was cut at eighteen
    // characters in the narrower column. The conversation still keeps its stripe at 1440 (see
    // check_density.py), and the column stays a drag between the same two bounds.
    expect(declared()).toContain("--sidebar-w: 288px");
    expect(declared()).toContain("--row-h-2: 44px");
    expect(declared()).toContain("--row-h-kid: 40px");
    expect(declared()).toContain("--chip-h-dense: 24px");
    expect(declared()).toContain("--plate: 24px");
    expect(wide(".sb-row")).toContain("min-height: var(--row-h-2)");
    expect(wide(".sb-prow")).toContain("min-height: var(--row-h)");
    expect(wide(".sb-kids > .sb-row")).toContain("min-height: var(--row-h-kid)");
    expect(wide(".sb-plate")).toContain("width: var(--plate)");
    expect(wide(".sb-plate")).toContain("border-radius: 50%");
    expect(wide(".sb-tile")).toContain("border-radius: var(--radius-xs)");
  });

  it("gives the two project buttons a small control's height and its chips the dense one", () => {
    expect(wide(".sb-pbtn")).toContain("height: var(--ctl-h-sm)");
    expect(wide(".sb-new")).toContain("height: var(--ctl-h)");
    expect(wide(".sb-chip")).toContain("height: var(--chip-h-dense)");
  });

  it("shows a project's + and a row's menu on hover or focus, not always", () => {
    // The + used to stand on every project row; with a project row of 36 px it is a hover action,
    // and it is also the first item of the row's menu and of its right click.
    expect(wide(".sidebar .sb-acts")).toContain("display: none");
    expect(wide(".sidebar .sb-row:hover .sb-acts")).toContain("display: flex");
    expect(wide(".sidebar .sb-row:focus-within .sb-acts")).toContain("display: flex");
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

  it("refuses a phone size off the six steps, and a desktop step the phone block forgot to move", () => {
    const planted = rules(`
      @media (max-width: 1023px) { .probe-phone-22 { font-size: 22px; } .probe-phone-ok { font-size: var(--fs-14); } .probe-phone-18 { font-size: var(--fs-18); } }
    `);
    expect(offScale(planted)).toEqual([".probe-phone-22 { font-size: 22px }", ".probe-phone-18 { font-size: var(--fs-18) }"]);
    expect(oversizeFonts(planted)).toEqual([]);
    const tall = rules("@media (max-width: 1023px) { .probe-phone-row { min-height: 72px; } .ph-tile { height: 72px; } }");
    expect(oversizeBoxes(tall)).toEqual([".probe-phone-row { min-height: 72px }"]);
  });

  it("reads a size it does not understand as a finding rather than as nothing", () => {
    expect(oversizeFonts(rules(".probe-odd { font-size: 3vmax; }"))).toEqual([".probe-odd { font-size: 3vmax }"]);
  });
});
