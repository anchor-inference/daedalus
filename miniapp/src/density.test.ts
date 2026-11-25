// The stylesheet is read as a table of sizes, the way i18n.test.ts reads the dictionary as a table
// of words. The app once grew to 15 px body text, 44 px round buttons and 64 px session rows one
// rule at a time, each of them reasonable on its own; this is what stops the next one.
//
// The claims for the desktop, and Settings' own column and row. Every size comes from the scale in
// :root. The approved design sets the type: 14 for controls, lists and the body, 15 for reading text
// and a page's title, 13 for secondary text, 12 for meta and labels and never smaller, 18 for a pane's
// title and 28 for a page's hero. So nothing outside the answer, the headings and the prose is drawn
// above 15 px on a desktop, and nothing at all under 12, in whatever unit it is written (14 was the
// cap until the page title moved to 15 and 11 the smallest step until the design set 12 as the floor).
// No box that is not a surface of its own is given a height a row or a control would not have. Rows
// and controls take their height from the row and control tokens, so a phone gets its 44 px from one
// media rule and not from forty.
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
import tokens from "./ui/tokens.css?raw";
import phone from "./ui/phone.css?raw";
import browserPhone from "./browser/phone.css?raw";
import petHost from "./pethost.tsx?raw";
import sidePanel from "./sidepanel/sidepanel.css?raw";
import terminals from "./terminal/workspace.css?raw";
import terminalSide from "./terminal/sidepanel.css?raw";
import terminalCompose from "./terminal/compose.css?raw";
import agentFeed from "./staff/agentfeed.css?raw";
import app from "./App.tsx?raw";
// Every stylesheet of the app, found rather than listed: a hand list was a line to add for each new
// sheet, and a sheet left off it answered to nothing. The tokens come first, since the claims read
// the first :root block as the scale; the Chrome side panel redefines the scale for its own subtree
// and is held to that below, not to the desktop's. The shared stylesheet is a list of imports, which
// are dropped; the guard reads rules one at a time, so the order they are joined in does not matter.
const sheets = import.meta.glob(["./**/*.css", "!./ui/tokens.css", "!./sidepanel/sidepanel.css"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;
// The sheets that were written before they were read here and break its claims (the calendar, the
// canvas, the agent link's cards, the rules editor and a few strips): each comes off this list when
// the screen it draws is redrawn on the new scale, and nothing is ever added to it.
const NOT_YET_HELD = ["./actionrules.css", "./agenttalk.css", "./calendar/blocks.css", "./calendar/calendar.css", "./canvas/canvas.css", "./contacts.css", "./maintenance.css", "./screens/dependencies.css", "./screens/promptchange.css"];
const css = tokens + "\n" + Object.keys(sheets).filter((name) => !NOT_YET_HELD.includes(name)).sort().map((name) => sheets[name].replace(/@import[^;]+;/g, "")).join("\n");

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

const TOKENS: Record<string, number> = { "--fs-11": 12, "--fs-12": 12, "--fs-13": 13, "--fs-14": 14, "--fs-15": 15, "--fs-16": 16, "--fs-17": 17, "--fs-18": 18, "--fs-20": 20, "--fs-22": 22, "--fs-28": 28, "--fs-mono": 13, "--fs-prose": 15 };

/** The largest size a desktop rule may state outside prose and headings, and the smallest anywhere. */
const DESKTOP_CAP = 15;
const FLOOR = 12;

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

/** Where text is allowed to be larger than the UI: running prose, headings, numbers meant to be read from across a room (a
 *  Details card opens on its one headline number, the reason that card exists), and glyphs. */
const LARGE_ALLOWED = [".answer", ".summary-body", ".preview-doc", ".composer-box .composer-field", "h1", "h2", "h3", ".kpi .value", ".dt-big", ".empty b", ".stat b", ".step-head b", ".attachment-glyph", ".btn.big", ".chev", ".voice-", ".orb", ".onboard-head", ".login",
  // The task dialog's title is edited where it stands, so its field is drawn at the heading's size;
  // at 14 px the title shrank under the cursor the moment Edit was pressed.
  ".ptask-title-input",
  // The code a terminal asks to be paired with is the check itself: the operator compares it, letter by
  // letter, with the one their terminal prints, so it is drawn at a heading's size on the sheet.
  ".pair-code",
  // A note's title is edited where it reads as the note's heading, and its words are prose written at
  // the size the answers are read at (the composer's field is allowed the same for the same reason);
  // the tinted copy under the field must match it letter for letter.
  ".ne-title", ".ne-field", ".ne-tint",
  // The open Inbox entry reads as prose at the answer's size, and its title is a pane's heading.
  ".inbox-prose", ".inbox-pane-title"];

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
  // The note editor is a dialog of its own, and its writing and reading panes are pages, not rows:
  // the room under the last line is where the next one is written.
  ".ne-dialog", ".ne-field", ".ne-tint", ".ne-read",
  // A layer over the whole window is a surface like a sheet: its bar is a page header's height.
  ".fullscreen-",
  // The page header's one row is 52 px on a desktop, as tall as the surface it heads.
  ".pagehead-row",
  // The open Inbox entry is a page of its own beside the list: the room above its first line and
  // below its last is the room a page has.
  ".inbox-pane",
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

/** Content with an intentional height: two-line file cards, a message placeholder,
 *  and the document viewport. Keep their limits explicit so larger boxes still fail the guard. */
const CONTENT_BOXES = [
  // A file card and the changed-files card under an answer are the design's 52 px two-line card (48
  // before it): the same frame for a file handed over and for the files written.
  { selector: ".artifact", property: "min-height", max: 52 },
  { selector: ".artifact-main", property: "min-height", max: 50 },
  { selector: ".changed-files-head", property: "height", max: 52 },
  { selector: ".turn-skeleton .sk-user", property: "height", max: 40 },
  { selector: ".html-frame", property: "min-height", max: 400 },
  // A note card's picture and the card-shaped placeholders the Notes tab shows while it loads.
  { selector: ".note-thumb", property: "height", max: 52 },
  { selector: ".notes-loading i", property: "height", max: 64 },
  // A reply waiting in the composer is a tile, not a row: two lines of its words over its "Reply" chip,
  // as Claude draws it; and the field that edits one holds a few lines of them.
  { selector: ".reply-card .reply-card-open", property: "height", max: 72 },
  { selector: ".reply-dialog-text", property: "min-height", max: 72 },
  // Not a box but the shade a sheet raises over its sticky footer while more waits below; shorter
  // than this it fell into the gap above the footer and said nothing.
  { selector: '.sheet-body[data-more="below"] .sheet-foot::before', property: "height", max: 56 },
  // A list row comes in three heights (Row's `height`): a line, a title over a meta line, and the
  // large row the approved designs draw for a list of providers or entries. The 56 is that one.
  { selector: ".list-row.h56", property: "min-height", max: 56 },
  // The model picker's list is a window onto six 32 px rows (six and the five 1 px gaps between them),
  // the same height whatever the tab or the search holds, so the box around it never changes size.
  { selector: ".model-picker-list", property: "height", max: 197 },
  // An Inbox row is a title over a preview over where it came from: three lines, 64 px, as drawn. The
  // line under an open question is the 48 px field the composer's answer line shares.
  { selector: ".inbox-row", property: "height", max: 64 },
  { selector: ".inbox-reply", property: "height", max: 48 },
  // The Terminals page: a list row holds a title, a command and an owner, its memory meter two lines
  // and a bar, and a pane's header a title, a folder, the owner chip and four icon buttons. Each is
  // the size the approved page draws, stated here so a taller one fails.
  { selector: ".tp-row-open", property: "height", max: 56 },
  { selector: ".tp-mem", property: "height", max: 56 },
  { selector: ".tp-pane-head", property: "height", max: 40 },
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
      if (size === null || size <= DESKTOP_CAP) continue;
      for (const selector of selectors(r)) {
        if (covered(selector, LARGE_ALLOWED)) continue;
        large.push(`${selector} { font-size: ${m[1].trim()} }`);
      }
    }
  }
  return large;
}

/** Every size under the 12 px floor, on any screen. A size relative to its parent (em, %) is left to
 *  check_density.py, which reads the drawn page: here an em is read against the 14 px body, and the
 *  small print of a large number would be read as small when it is not. */
function undersizeFonts(all: Rule[], tokens: Record<string, number> = TOKENS): string[] {
  const small: string[] = [];
  for (const r of all) {
    for (const m of r.body.matchAll(/(?:^|;)\s*font(?:-size)?\s*:\s*([^;]+)/g)) {
      if (/^\s*[\d.]+(em|%)/.test(m[1])) continue;
      const size = pixels(m[1], tokens);
      if (size === null || size >= FLOOR) continue;
      for (const selector of selectors(r)) small.push(`${selector} { font-size: ${m[1].trim()} }`);
    }
  }
  return small;
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
  // A size written as a number is a size the scale cannot move. One is allowed: the phone's 16 px
  // field (a touch screen's rule, skipped below). The 10 px beta tag that once sat below the scale
  // went with the 12 px floor.
  const numeric: string[] = [];
  for (const r of all) {
    if (r.media.includes("hover: none")) continue;
    for (const m of r.body.matchAll(/(?:^|;)\s*font-size\s*:\s*([\d.]+px)/g)) {
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
    for (const name of [...Object.keys(TOKENS), "--space-2", "--radius-sm", "--radius-md", "--radius-lg", "--radius-bubble", "--row-h", "--row-h-2", "--row-h-dense", "--row-h-touch", "--ctl-h", "--ctl-h-sm", "--chip-h-dense", "--ctl-h-lg", "--avatar", "--plate", "--sidebar-w", "--panel-w", "--pip-w", "--reading-w", "--chat-w", "--head-h",
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

  it("draws rows at 32, dense rows at 28, popovers at 14 px corners and code at 13", () => {
    // The approved design's rhythm: 32 px one-line rows on a 34 px pitch (sidebar, menus, settings'
    // sections), 28 for a dense row, 8 / 12 / 14 / 20 px corners for a control, a card, what floats and
    // the composer. They were 36, 32, and 16 for a popover; code was 12.5.
    const root = all.find((r) => r.selector === ":root" && !r.media)!.body;
    for (const decl of ["--row-h: 32px", "--row-h-dense: 28px", "--ctl-h: 32px", "--ctl-h-sm: 28px", "--radius-sm: 8px", "--radius-md: 12px", "--radius-lg: 14px", "--radius-xl: 20px", "--radius-bubble: 18px", "--fs-mono: 13px", "--icon: 16px"]) {
      expect(root, decl).toContain(decl);
    }
    expect(root).toContain("--lh-ui: 1.45");
    expect(root).toContain("--lh-prose: 1.6");
  });
});

describe("a page's header", () => {
  const DESKTOP = "min-width: 1024px";
  const desktopRule = (selector: string) => all.filter((r) => r.media.includes(DESKTOP) && selectors(r).includes(selector)).map((r) => r.body).join(";");

  it("is one 52 px row with its title 24 px in, on every page, whatever the column under it", () => {
    // --head-h is every header's height: a page's, the conversation's, the panel's tabs. It was 48
    // until the design set 52 for all of them.
    expect(all.find((r) => r.selector === ":root" && !r.media)!.body).toContain("--head-h: 52px");
    expect(desktopRule(".pagehead-row")).toContain("min-height: var(--head-h)");
    expect(desktopRule(".pagehead")).toContain("padding: 0 12px 0 24px");
    // The header once followed the page's column (padding in to the 1120 or 1680 px column's edge), so
    // the titles stood at six different x positions. No rule may pad it by the column again.
    expect(all.filter((r) => /\.pagehead:has\(\+ \.screen\.(narrow|wide)\)/.test(r.selector)).map((r) => r.selector)).toEqual([]);
  });

  it("draws the rule under a ruled page without growing it", () => {
    expect(desktopRule(".pagehead.ruled")).toContain("box-shadow: inset 0 -1px 0 var(--line)");
    expect(desktopRule(".pagehead.ruled")).not.toMatch(/border/);
  });

  it("sets a page's column at 880 (list), 760 (reading) or the window (full), all between 24 px margins", () => {
    const column = (name: string) => all.filter((r) => selectors(r).includes(`.screen.page-${name}`)).map((r) => r.body).join(";");
    expect(column("list")).toContain("calc(880px + 48px)");
    expect(column("reading")).toContain("calc(760px + 48px)");
    expect(column("full")).toContain("padding-inline: 24px");
    expect(column("full")).not.toContain("width:");
  });
});

describe("the inbox", () => {
  const rule = (selector: string) => all.filter((r) => !r.media && selectors(r).includes(selector)).map((r) => r.body).join(";");

  it("is a 480 px list of 64 px rows beside a 760 px reading column that narrows with the window", () => {
    expect(rule(".inbox-list")).toContain("width: 480px");
    expect(rule(".inbox-list")).toContain("padding: 4px 8px 16px");
    expect(rule(".inbox-row")).toContain("height: 64px");
    expect(rule(".inbox-group-head")).toContain("height: 36px");
    expect(rule(".inbox-pane")).toContain("width: min(var(--reading-w), calc(100% - 48px))");
    expect(all.find((r) => r.selector === ":root" && !r.media)!.body).toContain("--reading-w: 760px");
  });

  it("marks an unread row by a 6 px dot that is always there, so reading it moves nothing", () => {
    expect(rule(".inbox-row-unread")).toContain("opacity: 0");
    expect(rule(".inbox-row-unread")).toContain("width: var(--dot)");
    expect(rule(".inbox-row.unread .inbox-row-unread")).toContain("opacity: 1");
  });

  it("colours a row by one of the three accents: orange for what waits or failed, cyan for finished work", () => {
    expect(rule(".inbox-row-mark.attention")).toContain("var(--attention)");
    expect(rule(".inbox-row-mark.live")).toContain("var(--live)");
    expect(rule(".inbox-group-head.waiting")).toContain("var(--attention)");
  });
});

describe("every size", () => {
  it("is at or under 15 px on a desktop outside prose, headings, titles and glyphs", () => {
    expect(oversizeFonts(all)).toEqual([]);
  });

  it("is never under 12 px, on a desktop or a phone", () => {
    expect(undersizeFonts(all)).toEqual([]);
    expect(undersizeFonts(all, phoneTokens(all))).toEqual([]);
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
    expect(phoneRule(".list-row")).toContain("min-height: var(--list-row-h)");
    expect(phoneRule(".list-row.one")).toContain("min-height: var(--list-row-h-1)");
    expect(phoneRule(".list-row")).not.toMatch(/border/);
    expect(phoneRule(".ph-chip")).toContain("height: var(--chip-h)");
    expect(phoneRule(".ph-chip::after")).toContain("inset: -8px 0");
    // The sheet's head keeps its height under a long body: shrinking, it slid its subtitle under the
    // model sheet's search field.
    expect(phoneRule(".sheet-head")).toContain("flex: none");
    expect(phoneRule(".menu-row")).toContain("min-height: var(--list-row-h-1)");
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

  it("opens 300 px wide and stays a drag, with a 52 px head and foot and every row one 32 px line", () => {
    // 300 is the approved design's column (it was 288, and 272 before the rows went to two lines).
    // The design drew it fixed; the operator kept the drag, so what is pinned is the default the
    // stylesheet and the drag start from, and that it sits inside the bounds the drag is held to,
    // never the drawn width. Every row — a pinned destination, a project, a chat, a chat inside a
    // project — is the 32 px one-line row of every list: a chat was two lines of 44 px, its model and
    // host on the second, and the design moved those into the tooltip and the state to the row's end.
    const px = (name: string) => Number(new RegExp(`${name}: (\\d+)px`).exec(declared())?.[1]);
    expect(declared()).toContain("--sidebar-w: 300px");
    expect(app).toMatch(/const SIDEBAR_W = 300;/);
    const [min, max] = [Number(/const SIDEBAR_W_MIN = (\d+);/.exec(app)?.[1]), Number(/const SIDEBAR_W_MAX = (\d+);/.exec(app)?.[1])];
    expect(min).toBe(px("--sidebar-w-min"));
    expect(max).toBe(px("--sidebar-w-max"));
    expect(min).toBeGreaterThanOrEqual(220);
    expect(min).toBeLessThan(300);
    expect(max).toBeGreaterThan(300);
    expect(max).toBeLessThanOrEqual(420);
    expect(declared()).toContain("--row-h: 32px");
    expect(wide(".sidebar-head")).toContain("height: var(--head-h)");
    expect(wide(".sidebar-foot")).toContain("height: var(--head-h)");
    expect(wide(".sb-nav")).toContain("height: var(--row-h)");
    expect(wide(".sb-row")).toContain("height: var(--row-h)");
    // Inside a project a chat's text starts where the project's name does: 12 + 16 + 8 of the row.
    expect(wide(".sb-kids > .sb-row")).toContain("padding-left: 36px");
    // A subagent under its unfolded leader is one short line, quieter than a chat: the dense row.
    expect(wide(".sb-sub")).toContain("height: var(--row-h-dense)");
    // No second line and no plate is left to pin a two-line height on.
    expect(declared()).not.toContain("--row-h-kid");
    expect(wide(".sb-plate")).toBe("");
  });

  it("gives the phone's kind tabs a finger's height", () => {
    // The desktop's kind tabs became the Show part of the Chats label's menu; the phone's Chats page
    // keeps them as tabs, each the 44 px a finger needs.
    const rule = (selector: string) => all.filter((r) => !r.media && selectors(r).includes(selector)).map((r) => r.body).join(";");
    expect(rule(".kind-tab")).toContain("height: var(--ctl-h-sm)");
    expect(rule(".ph-kinds .kind-tab")).toContain("height: var(--row-h-touch)");
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

  it("draw a menu row as an icon and a label centred on one line", () => {
    // The row lost its flex layout once, which left the icon inline on the label's baseline: three
    // pixels high, touching the word, and the text of an item with no icon a column further left.
    // check_menu_icons.py measures the same claim on a drawn page.
    expect(decl(".menu button")).toContain("display: flex");
    expect(decl(".menu button")).toContain("align-items: center");
    expect(decl(".menu button")).toContain("gap: var(--space-2)");
    expect(decl(".menu button > .ic")).toContain("flex: none");
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

  it("give the Terminals page a 56 px list row, a 40 px pane header, a 36 px pane foot and a 12 px gap", () => {
    // The page draws its list in rows of three lines (title, command, owner) at 56 px, a pane's header
    // at 40 and its foot at 36, in panes a 12 px gap apart: the splitter is that gap, and it draws a
    // line only while it is touched. The 52 px header over all of it is the page header's own.
    expect(decl(".tp-row-open")).toContain("height: 56px");
    expect(decl(".tp-pane-head")).toContain("height: 40px");
    expect(decl(".tp-pane-foot")).toContain("height: 36px");
    expect(decl(".tp-mem")).toContain("height: 56px");
    expect(decl(".tp-label")).toContain("height: var(--row-h)");
    expect(decl(".tp-filter")).toContain("height: var(--ctl-h-sm)");
    expect(decl(".tp")).toContain("--tp-split: 12px");
    expect(decl(".tp")).toContain("--tp-list-w: 280px");
    expect(decl(".tp-list")).toContain("width: var(--tp-list-w)");
    expect(decl(".tp-grid")).toContain("padding: 12px");
    // One colour per kind of data, and every one of them an alias of a token each theme sets: a
    // hex here would stay put while the theme around it changed.
    const declared = all.find((r) => r.selector === ":root" && !r.media)!.body;
    for (const kind of ["context", "cache", "wait", "output", "path", "branch", "bad"]) expect(declared).toMatch(new RegExp(`--data-${kind}: var\\(--`));
    expect(terminals).not.toMatch(/#[0-9a-f]{3,8}\b/i);
  });

  it("give the Terminals side panel the workspace's rows and the same scale", () => {
    // The panel sits beside four panes and lists files, commands and terminals: its tab bar is a row
    // like the workspace's bar, every list row a dense row, its pills dense chips. It is read on its
    // own here so the rules above hold for it too, and like the workspace it names no colour.
    const side = rules(terminalSide);
    expect(oversizeFonts(side)).toEqual([]);
    expect(numericFonts(side)).toEqual([]);
    expect(oversizeBoxes(side)).toEqual([]);
    const sideDecl = (selector: string) => side.filter((r) => r.selector === selector).map((r) => r.body).join(";");
    expect(sideDecl(".tside-head")).toContain("height: var(--row-h)");
    expect(sideDecl(".tside-tab")).toContain("height: var(--ctl-h-sm)");
    for (const row of [".tside-file", ".tside-act-row", ".tside-pane"]) expect(sideDecl(row)).toContain("height: var(--row-h-dense)");
    expect(sideDecl(".tside-term-open")).toContain("height: var(--row-h)");
    expect(sideDecl(".tside-pill")).toContain("height: var(--chip-h-dense)");
    expect(terminalSide).not.toMatch(/#[0-9a-f]{3,8}\b/i);
  });

  it("give a workspace pane's compose line a dense row, and the messages between agents the same scale", () => {
    // The line sits under a terminal in a pane four of which may share the screen, so it is one dense
    // row like the pane's header, its chips the side panel's dense chips, its hints small controls.
    // The feed entries and the orchestrator's agent rows read at the Feed's and the event card's sizes.
    // Neither sheet names a colour: the message colour is the theme's information token.
    for (const sheet of [terminalCompose, agentFeed]) {
      const own = rules(sheet);
      expect(oversizeFonts(own)).toEqual([]);
      expect(numericFonts(own)).toEqual([]);
      expect(oversizeBoxes(own)).toEqual([]);
      expect(sheet).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    }
    const composeDecl = (selector: string) => rules(terminalCompose).filter((r) => r.selector === selector).map((r) => r.body).join(";");
    expect(composeDecl(".tp-compose")).toContain("height: var(--row-h-dense)");
    expect(composeDecl(".tp-chip")).toContain("height: var(--chip-h-dense)");
    expect(composeDecl(".tp-slash-row")).toContain("height: var(--ctl-h-sm)");
    expect(rules(agentFeed).find((r) => r.selector === ".agent-line-head")!.body).toContain("min-height: var(--row-h-dense)");
    // The message flash is a state colour like the others, an alias of a token every theme sets.
    const declared = all.find((r) => r.selector === ":root" && !r.media)!.body;
    expect(declared).toMatch(/--state-message: var\(--/);
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

  it("gives the sidebar the first column alone, with no rail beside it", () => {
    // The rail of icons is gone: the sidebar's labelled rows hold the destinations, and folding sets
    // its share to 0, with a button in the window's corner to bring it back.
    const wide = all.filter((r) => r.media.includes("min-width: 1024px"));
    const body = (sel: string) => wide.filter((r) => selectors(r).includes(sel)).map((r) => r.body).join(";");
    expect(body(".app")).toContain("grid-template-columns: var(--sidebar-w) minmax(0, 1fr)");
    expect(body(".desktop-column")).toContain("width: var(--sidebar-w)");
    expect(all.filter((r) => /\.rail\b|\.rail-/.test(r.selector))).toEqual([]);
    expect(all.find((r) => r.selector === ":root" && !r.media)!.body).not.toContain("--rail-w");
    // A drag writes the width once a frame; a transition on it is what made the edge lag and glide.
    expect(body(".sidebar")).not.toMatch(/transition/);
    expect(body(".desktop-column")).not.toMatch(/transition/);
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

  it("holds the stripe at 760 px at every window width", () => {
    // One measure, 760 px at 15/1.6 (about 85 characters), on a
    // laptop and on a 2560 screen alike. The stripe once widened to 1120 and 1320 on wide windows, and
    // every line of an answer became a long return trip for the eye. One declaration, no media rule
    // that moves it; the Appearance setting's narrow and wide columns are the operator's own choice.
    const stripes = all.filter((r) => /--chat-w:/.test(r.body) && /(^|[\s,]):root($|[\s,])/.test(r.selector));
    expect(stripes.map((r) => r.media)).toEqual([""]);
    expect(stripes[0].body).toMatch(/--chat-w: 760px/);
    // The operator's bubble reads at the answer's size and stops at three quarters of the stripe.
    expect(decl(".msg.user")).toContain("max-width: min(560px, 100%)");
    expect(decl(".msg.user")).toContain("font-size: var(--fs-prose)");
    expect(decl(".msg.user")).toContain("border-radius: var(--radius-bubble)");
  });
});

describe("settings", () => {
  const decl = (selector: string) => all.filter((r) => r.selector === selector && !r.media).map((r) => r.body).join(" ");
  const declared = () => all.find((r) => r.selector === ":root" && !r.media)!.body;
  const wide = (selector: string) => all.filter((r) => r.media.includes("min-width: 1024px") && selectors(r).includes(selector)).map((r) => r.body).join(";");

  it("is a page: the list of sections in the sidebar's 300 px, the section in a centred 880 px column", () => {
    // Settings was a dialog over the screen it was opened from, its list 248 px and its section the
    // dialog's whole width. It is a page of its own now: the list stands where the sidebar was and is
    // as wide, a 52 px header carries the section's title, and the section reads in the same centred
    // 880 px column (less 24 px a side on a narrower window) at every width.
    expect(wide(".settings-nav")).toContain("width: 300px");
    expect(wide(".settings-head")).toContain("height: var(--head-h)");
    expect(wide(".settings-page .settings-col")).toContain("width: min(880px, 100% - 48px)");
    expect(wide(".settings-page .settings-col")).toContain("margin: 0 auto");
    expect(wide(".settings-nav .settings-link")).toContain("height: var(--row-h)");
  });

  it("draws a settings card's rows 44 px tall and a provider's 56", () => {
    // The cards a section is drawn in: a row of a name and its value at 44, a provider leading its
    // models at 56, both from tokens so the phone can take them to its own touch sizes.
    expect(declared()).toContain("--card-row-h: 44px");
    expect(declared()).toContain("--card-row-h-lg: 56px");
    expect(decl(".set-row")).toContain("min-height: var(--card-row-h)");
    expect(decl(".prov-row")).toContain("height: var(--card-row-h-lg)");
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

// The Chrome side panel is a desktop column beside a page, narrow enough that the phone rules apply.
// The operator measured its text as larger than the page it sits beside, so the panel redefines the
// scale for its own subtree one step under the desktop (13 px body, 12 px meta) and brings the
// desktop's control heights back. Its own sheet is held to that, and to naming steps, here.
describe("the side panel", () => {
  const panel = rules(sidePanel);
  const scale = panel.find((r) => r.selector === ".sidepanel" && !r.media);
  const value = (name: string) => Number(new RegExp(`${name}: (\\d+(?:\\.\\d+)?)px`).exec(scale?.body ?? "")?.[1] ?? Number.NaN);

  it("redefines every step at or under the desktop's, with a 13 px body and 12 px meta", () => {
    for (const [name, px] of Object.entries(TOKENS)) {
      expect(value(name), name).toBeLessThanOrEqual(px);
    }
    expect(value("--fs-prose")).toBe(13);
    expect(value("--fs-14")).toBe(13);
    expect(value("--fs-12")).toBe(12);
    expect(scale!.body).toContain("font-size: var(--fs-14)");
  });

  it("keeps the desktop's control heights, since it is driven with a mouse", () => {
    expect(value("--ctl-h")).toBe(32);
    expect(value("--ctl-h-sm")).toBe(28);
  });

  it("names a step of the scale in every rule of its own", () => {
    expect(numericFonts(panel)).toEqual([]);
    expect(oversizeFonts(panel)).toEqual([]);
  });
});

describe("the guard itself", () => {
  it("reads every stylesheet but the few still waiting to be held, and those exist", () => {
    for (const name of NOT_YET_HELD) expect(Object.keys(sheets), name).toContain(name);
    expect(NOT_YET_HELD.length).toBeLessThanOrEqual(9);
    expect(Object.keys(sheets).length).toBeGreaterThan(40);
  });


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

  it("refuses a size under the floor, written as a step, a number or a rem", () => {
    const small = rules(".probe-11 { font-size: 11px; } .probe-step { font-size: var(--fs-12); } .probe-rem { font-size: .6rem; } .probe-em { font-size: .8em; }");
    expect(undersizeFonts(small)).toEqual([".probe-11 { font-size: 11px }", ".probe-rem { font-size: .6rem }"]);
    expect(undersizeFonts(small, { ...TOKENS, "--fs-12": 11 })).toContain(".probe-step { font-size: var(--fs-12) }");
  });

  it("reads a size it does not understand as a finding rather than as nothing", () => {
    expect(oversizeFonts(rules(".probe-odd { font-size: 3vmax; }"))).toEqual([".probe-odd { font-size: 3vmax }"]);
  });
});
