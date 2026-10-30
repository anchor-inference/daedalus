// Mermaid diagrams in an answer. md.ts writes a closed ```mermaid fence as an ordinary code card
// marked `data-mermaid`; this module, called as the markup lands in the page, draws each one and puts
// the drawing above the code, which stays in the card (hidden) for the copy button and the toggle.
//
// The page rewrites a message's markup on every streamed token, so a drawing is kept by its source
// and theme and put back synchronously, before the browser paints: without that a diagram above a
// still-streaming paragraph would vanish and come back on every token. The renderer is loaded the
// first time a diagram is met and never before (see mermaidrender.ts).

import { t } from "./i18n";
import { iconSvg } from "./icons";
import type { MermaidTheme } from "./mermaidrender";

type Drawn = { svg: string; id: string } | { error: string };

const settled = new Map<string, Drawn>();
const inFlight = new Map<string, Promise<Drawn>>();
const SETTLED_MAX = 120;
// Past this a source is not a diagram anybody reads, and mermaid would refuse it anyway.
const SOURCE_MAX = 40_000;
let uses = 0;

let probe: HTMLSpanElement | null = null;

/** A token as a colour mermaid can compute with: `rgb()`, never `var()` or `color-mix()`. */
function tokenColor(name: string, fallback: string, alpha = 1): string {
  if (!probe) {
    probe = document.createElement("span");
    probe.style.display = "none";
    document.body.appendChild(probe);
  }
  probe.style.color = `var(${name}, ${fallback})`;
  const value = getComputedStyle(probe).color;
  const rgb = /^rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)$/.exec(value);
  if (!rgb) return fallback;
  const a = (rgb[4] === undefined ? 1 : Number(rgb[4])) * alpha;
  return a >= 1 ? `rgb(${rgb[1]}, ${rgb[2]}, ${rgb[3]})` : `rgba(${rgb[1]}, ${rgb[2]}, ${rgb[3]}, ${a.toFixed(3)})`;
}

/** The app's current colours as a mermaid theme: nodes on the second surface, lines in the muted ink. */
export function currentTheme(): MermaidTheme {
  const dark = document.documentElement.dataset.scheme !== "light";
  const fg = tokenColor("--fg", dark ? "#ededef" : "#111111");
  const muted = tokenColor("--fg-2", dark ? "#a8a9af" : "#555555");
  const faint = tokenColor("--fg-3", dark ? "#92939b" : "#777777");
  const bg = tokenColor("--bg", dark ? "#0b0b0d" : "#ffffff");
  const surface = tokenColor("--surface-2", dark ? "#1c1c1f" : "#f2f2f3");
  const raised = tokenColor("--surface-3", dark ? "#26262a" : "#e8e8ea");
  const sunken = tokenColor("--surface-1", dark ? "#141416" : "#f7f7f8");
  const border = tokenColor("--fg-3", faint, 0.55);
  const accent = tokenColor("--accent", "#2dd4bf");
  // The drawing sits in a code card, not on the page: a label box in the page colour was a patch.
  const card = tokenColor("--card", sunken);
  const vars: Record<string, string> = {
    background: card, textColor: fg, lineColor: faint,
    primaryColor: surface, primaryTextColor: fg, primaryBorderColor: border,
    secondaryColor: raised, secondaryTextColor: fg, secondaryBorderColor: border,
    tertiaryColor: sunken, tertiaryTextColor: fg, tertiaryBorderColor: border,
    mainBkg: surface, nodeBorder: border, nodeTextColor: fg,
    clusterBkg: sunken, clusterBorder: border, titleColor: fg, edgeLabelBackground: card,
    noteBkgColor: raised, noteTextColor: fg, noteBorderColor: border,
    actorBkg: surface, actorBorder: border, actorTextColor: fg, actorLineColor: faint,
    signalColor: muted, signalTextColor: fg, labelBoxBkgColor: surface, labelBoxBorderColor: border, labelTextColor: fg,
    loopTextColor: fg, activationBkgColor: raised, activationBorderColor: border, sequenceNumberColor: bg,
    pie1: accent, pieStrokeColor: bg, pieTitleTextColor: fg, pieSectionTextColor: bg, pieLegendTextColor: fg,
    git0: accent, gitBranchLabel0: bg,
  };
  const font = getComputedStyle(document.body).fontFamily || "sans-serif";
  return { key: Object.values(vars).join("|") + font, dark, font, vars };
}

function draw(source: string, theme: MermaidTheme): Promise<Drawn> {
  const key = `${theme.key}\n${source}`;
  const known = inFlight.get(key);
  if (known) return known;
  const asked: Promise<Drawn> = (source.length > SOURCE_MAX ? Promise.reject(new Error("too long")) : import("./mermaidrender").then(({ renderMermaid }) => renderMermaid(source, theme)))
    .then((drawn): Drawn => drawn, (error: unknown): Drawn => ({ error: String((error as Error)?.message ?? error).split("\n")[0].slice(0, 160) }))
    .then((drawn) => {
      inFlight.delete(key);
      settled.set(key, drawn);
      while (settled.size > SETTLED_MAX) {
        const oldest = settled.keys().next();
        if (oldest.done) break;
        settled.delete(oldest.value);
      }
      return drawn;
    });
  inFlight.set(key, asked);
  return asked;
}

/** The drawing's markup with its ids made its own: the same diagram may be on the page twice (two
 *  answers, the card and the large view), and an arrowhead found by id in the other copy disappears
 *  with it when that copy is hidden. */
function ownIds(drawn: { svg: string; id: string }): string {
  return drawn.svg.split(drawn.id).join(`${drawn.id}-${++uses}`);
}

function show(card: HTMLElement, drawn: Drawn): void {
  card.querySelector(":scope > .mermaid-view")?.remove();
  card.querySelector(":scope > .mermaid-note")?.remove();
  const head = card.querySelector(":scope > .codehead");
  if ("error" in drawn) {
    card.dataset.mermaid = "error";
    const note = document.createElement("div");
    note.className = "mermaid-note";
    note.textContent = t("diagram.error");
    if (drawn.error) note.title = drawn.error;
    card.appendChild(note);
    return;
  }
  const view = document.createElement("div");
  view.className = "mermaid-view";
  view.setAttribute("role", "img");
  view.setAttribute("aria-label", t("diagram.label"));
  view.innerHTML = ownIds(drawn);
  head?.after(view);
  card.dataset.mermaid = "done";
}

/** Draw the diagrams of markup just written into the page. Cheap when there are none. */
export function hydrateDiagrams(root: HTMLElement): void {
  const cards = root.querySelectorAll<HTMLElement>(".codecard[data-mermaid]");
  if (!cards.length) return;
  const theme = currentTheme();
  cards.forEach((card) => paint(card, theme));
}

function paint(card: HTMLElement, theme: MermaidTheme): void {
  const source = card.querySelector(":scope > pre > code")?.textContent ?? "";
  const key = `${theme.key}\n${source}`;
  if (card.dataset.drawn === key) return;
  card.dataset.drawn = key;
  const known = settled.get(key);
  if (known) return show(card, known);
  if (card.dataset.mermaid === "pending") card.dataset.mermaid = "loading";
  void draw(source, theme).then((drawn) => {
    if (card.isConnected && card.dataset.drawn === key) show(card, drawn);
  });
}

// ── the large view ──────────────────────────────────────────────────────────────────────────

let zoom: HTMLDivElement | null = null;
let returnFocus: HTMLElement | null = null;

function closeZoom(): void {
  zoom?.remove();
  zoom = null;
  returnFocus?.focus();
  returnFocus = null;
}

function openZoom(card: HTMLElement, from: HTMLElement): void {
  const svg = card.querySelector(":scope > .mermaid-view > svg");
  if (!svg) return;
  closeZoom();
  returnFocus = from;
  zoom = document.createElement("div");
  zoom.className = "diagram-zoom";
  zoom.setAttribute("role", "dialog");
  zoom.setAttribute("aria-modal", "true");
  zoom.setAttribute("aria-label", t("diagram.label"));
  zoom.tabIndex = -1;
  const close = escapeText(t("common.close"));
  zoom.innerHTML = `<div class="diagram-zoom-top"><button type="button" data-zoom-close="1" aria-label="${close}" title="${close}">${iconSvg("close", 18)}</button></div><div class="diagram-zoom-body"></div>`;
  // Re-scoped like any other copy, and freed of the width cap mermaid writes for the inline size.
  const key = card.dataset.drawn ?? "";
  const drawn = settled.get(key);
  const body = zoom.querySelector(".diagram-zoom-body") as HTMLElement;
  body.innerHTML = drawn && !("error" in drawn) ? ownIds(drawn) : svg.outerHTML;
  const big = body.querySelector("svg");
  if (big) {
    big.style.maxWidth = "";
    big.setAttribute("width", "100%");
    big.setAttribute("height", "100%");
  }
  document.body.appendChild(zoom);
  zoom.focus();
}

function escapeText(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

if (typeof document !== "undefined") {
  document.addEventListener("click", (e) => {
    const target = e.target as HTMLElement | null;
    if (zoom && (target?.closest?.("[data-zoom-close]") || target === zoom || target?.classList?.contains("diagram-zoom-body"))) {
      closeZoom();
      return;
    }
    const button = target?.closest?.("button[data-mermaid-toggle], button[data-mermaid-open]") as HTMLButtonElement | null;
    const card = button?.closest(".codecard") as HTMLElement | null;
    if (!button || !card) return;
    if (button.dataset.mermaidOpen) return openZoom(card, button);
    // The reader's choice of code or drawing lives on the card; a streamed rewrite starts over at the drawing.
    const code = card.dataset.view !== "code";
    card.dataset.view = code ? "code" : "diagram";
    const label = t(code ? "diagram.drawing" : "diagram.source");
    button.setAttribute("aria-label", label);
    button.title = label;
    button.innerHTML = iconSvg(code ? "chart" : "braces", 14);
  });
  document.addEventListener("keydown", (e) => {
    if (zoom && e.key === "Escape") closeZoom();
  });
  // A theme change repaints the drawings in the new colours; the old ones stay until the new arrive.
  let timer = 0;
  new MutationObserver(() => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      if (document.querySelector(".codecard[data-mermaid]")) hydrateDiagrams(document.body);
    }, 120);
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-scheme", "data-theme", "style"] });
}
