// @vitest-environment jsdom
// The icon set is a façade: 132 names that things outside the bundle also use (the navigation
// config, a page's manifest, the calendar's types, an answer's flow blocks), each drawn by one glyph
// of the `lucide` package. These claims keep it honest: every name has its glyph, the two renderers
// draw the same thing, and a size a screen asks for lands on the scale.
import { renderToStaticMarkup } from "react-dom/server";
import * as lucide from "lucide";
import { describe, expect, it } from "vitest";
import { ICON_NAMES, ICON_SIZES, ICON_STROKE, Icon, type IconName, iconSvg, isIconName, snapIconSize } from "./icons";

/** The name the app has used, and the canonical Lucide export that draws it. `logo` is the product's
 *  mark, not an icon, and keeps its own drawing. */
const GLYPHS: Record<Exclude<IconName, "logo">, keyof typeof lucide> = {
  open: "LogIn", info: "Info", gauge: "Gauge", coin: "Coins", brain: "Brain", activity: "Activity", cut: "Scissors", selectall: "ScanText", hand: "Hand", star: "Star", gift: "Gift", ffwd: "FastForward", utensils: "Utensils", cart: "ShoppingCart",
  dumbbell: "Dumbbell", plane: "Plane", home: "House", list: "List", heart: "Heart", book: "Book", chat: "MessageCircle",
  headset: "Headset", database: "Database", mail: "Mail", cloud: "Cloud", checkcircle: "CircleCheck", note: "FileText",
  bold: "LetterB", italic: "Italic", strike: "Strikethrough", code: "Code", heading: "LetterH", quote: "TextQuote",
  olist: "ListOrdered", tasks: "ListTodo", table: "Table", hr: "SeparatorHorizontal", back: "ChevronLeft", more: "Ellipsis",
  plus: "Plus", minus: "Minus", up: "ArrowUp", stop: "Square", model: "Diamond", terminal: "SquareTerminal", file: "File",
  pen: "Pencil", search: "Search", globe: "Globe", translate: "Languages", attach: "Paperclip", image: "Image",
  question: "QuestionMark", ask: "CircleQuestionMark", skill: "Blocks", spawn: "GitBranchPlus", bulb: "Lightbulb",
  wand: "WandSparkles", wrench: "Wrench", clock: "Clock", calendar: "CalendarDays", plug: "Plug", dot: "Dot",
  compact: "TextAlignStart", folder: "Folder", folderplus: "FolderPlus", archive: "Archive", braces: "Braces",
  settings: "Settings", bots: "Bot", inbox: "Inbox", board: "SquareKanban", changes: "GitBranch",
  chart: "ChartNoAxesColumn", loop: "RefreshCw", pause: "Pause", play: "Play", trash: "Trash", check: "Check", close: "X",
  send: "Send", download: "Download", share: "Share", split: "SquareSplitHorizontal", key: "Key", lock: "Lock",
  shield: "Shield", link: "Link", unlink: "Unlink", down: "ArrowDown", copy: "Copy", paste: "ClipboardPaste",
  fork: "GitFork", undo: "Undo2", reply: "Reply", columns: "Columns2", eyeoff: "EyeOff", eye: "Eye", mic: "Mic",
  monitor: "Monitor", laptop: "Laptop", phone: "Smartphone", expand: "Maximize2", external: "ExternalLink",
  reload: "RotateCw", forward: "ChevronRight", panel: "PanelRight", sidebar: "PanelLeft", chevron: "ChevronDown",
  bolt: "Zap", volume: "Volume1", mute: "VolumeX", bell: "Bell", alert: "TriangleExclamationPoint", conductor: "Network",
  journal: "NotebookText", compass: "Compass", user: "User", flag: "Flag", menu: "Menu", pin: "MapPin", pushpin: "Pin",
  compose: "SquarePen", grid: "LayoutGrid", apps: "Grid2x2Plus", import: "Import", vdots: "EllipsisVertical",
  region: "Crop", camera: "Camera", offline: "WifiOff", sliders: "Settings2",
};

type Node = [string, Record<string, string>];
type Shape = [string, [string, string][]];
const byName = ([a]: [string, string], [b]: [string, string]) => a.localeCompare(b);
/** The elements an <svg> draws, as a tag and its sorted attributes, whichever way the markup spelt them. */
const shapes = (svg: string): Shape[] => Array.from(new DOMParser().parseFromString(svg, "text/html").querySelector("svg")!.children)
  .map((el) => [el.tagName.toLowerCase(), Array.from(el.attributes).map((a): [string, string] => [a.name, a.value]).sort(byName)]);
const expected = (glyph: Node[]): Shape[] => glyph.map(([tag, attrs]) => [tag, Object.entries(attrs).sort(byName)]);

describe("the set", () => {
  // Each of the 132 names is drawn and parsed back by jsdom's DOMParser, and the three loops below do
  // that again and again: about a tenth of a second on an idle machine, over the default five seconds
  // on a busy one.
  it("keeps all 132 names, each drawn by the glyph the mapping names", { timeout: 30_000 }, () => {
    expect(ICON_NAMES.length).toBe(132);
    expect(new Set(ICON_NAMES)).toEqual(new Set([...Object.keys(GLYPHS), "logo"]));
    for (const [name, exportName] of Object.entries(GLYPHS)) {
      const glyph = lucide[exportName] as unknown as Node[];
      expect(Array.isArray(glyph) && glyph.length > 0, `${name} -> ${String(exportName)} is not a Lucide node`).toBe(true);
      expect(shapes(iconSvg(name as IconName)), name).toEqual(expected(glyph));
    }
  });

  it("recognises its names and nothing else", () => {
    expect(isIconName("inbox")).toBe(true);
    expect(isIconName("logo")).toBe(true);
    expect(isIconName("no-such-icon")).toBe(false);
    expect(isIconName("toString")).toBe(false);
    expect(isIconName(null)).toBe(false);
  });

  it("draws every name, in the one stroke, and the logo with its own path", { timeout: 30_000 }, () => {
    for (const name of ICON_NAMES) {
      const svg = iconSvg(name);
      expect(shapes(svg).length, name).toBeGreaterThan(0);
      expect(svg, name).toContain(`stroke-width="${ICON_STROKE}"`);
    }
    expect(shapes(iconSvg("logo"))).toEqual([["path", [["d", "M10 3.5H3.5v17h17v-17H14M10 16.5H7.5v-9h9v9H14M12 11.2a.8.8 0 1 0 0 1.6.8.8 0 0 0 0-1.6z"]]]]);
  });

  it("draws Stop solid and nothing else", () => {
    expect(iconSvg("stop")).toContain('fill="currentColor"');
    expect(ICON_NAMES.filter((name) => iconSvg(name).includes('fill="currentColor"'))).toEqual(["stop"]);
  });
});

describe("sizes", () => {
  it("lands every size on the scale", () => {
    const cases: [number, number][] = [[1, 12], [11, 12], [12, 12], [13, 14], [14, 14], [15, 16], [16, 16], [17, 16], [18, 16], [19, 20], [20, 20], [22, 20], [23, 24], [24, 24], [32, 24], [64, 24]];
    for (const [asked, drawn] of cases) expect(snapIconSize(asked), String(asked)).toBe(drawn);
    for (let size = 1; size <= 80; size += 0.5) expect(ICON_SIZES as readonly number[]).toContain(snapIconSize(size));
  });

  it("draws 16 when no size is asked and snaps the ones that are", () => {
    expect(iconSvg("check")).toContain('width="16" height="16"');
    expect(iconSvg("check", 18)).toContain('width="16" height="16"');
    expect(iconSvg("check", 13)).toContain('width="14" height="14"');
    const html = renderToStaticMarkup(<Icon name="check" />);
    expect(html).toContain('width="16"');
    expect(renderToStaticMarkup(<Icon name="check" size={21} />)).toContain('width="20"');
  });
});

describe("the component and the string renderer", () => {
  it("draw the same shapes for every name", { timeout: 30_000 }, () => {
    for (const name of ICON_NAMES) {
      const component = renderToStaticMarkup(<Icon name={name} size={20} />);
      expect(shapes(component), name).toEqual(shapes(iconSvg(name, 20)));
    }
  });

  it("draws a page's own path in the set's stroke instead of the name's glyph", () => {
    const html = renderToStaticMarkup(<Icon name="grid" path="M4 4h16v16H4z" />);
    expect(html).toContain('class="ic ic-custom"');
    expect(html).toContain('<path d="M4 4h16v16H4z"></path>');
    expect(html).toContain(`stroke-width="${ICON_STROKE}"`);
    expect(html).not.toContain("<rect");
  });
});
