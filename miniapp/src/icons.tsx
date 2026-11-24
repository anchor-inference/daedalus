// One icon set for the whole app. The names below are a contract with things outside the bundle (the
// navigation config's icon map, a page manifest's `icon`, the calendar's types, the flow blocks of an
// answer's Markdown, the settings index), so they stay; each is drawn by one glyph of the `lucide`
// package, whose icons are plain node lists. The same nodes feed the React component and the string
// renderer, so an answer's rendered Markdown and a screen never draw a name two ways.

import { createElement } from "react";
import {
  Archive, ArrowDown, ArrowUp, Bell, Blocks, Book, Bot, Braces, CalendarDays, Camera, ChartNoAxesColumn, Check,
  ChevronDown, ChevronLeft, ChevronRight, CircleCheck, CircleQuestionMark, ClipboardPaste, Clock, Cloud, Code,
  Columns2, Compass, Copy, Crop, Database, Diamond, Dot, Download, Dumbbell, Ellipsis, EllipsisVertical,
  ExternalLink, Eye, EyeOff, FastForward, File, FileText, Flag, Folder, FolderPlus, GitBranch, GitBranchPlus,
  GitFork, Globe, Grid2x2Plus, Hand, Headset, Heart, House, Image, Import, Inbox, Italic, Key, Languages,
  Laptop, LayoutGrid, LetterB, LetterH, Lightbulb, Link, List, ListOrdered, ListTodo, Lock, LogIn, Mail,
  MapPin, Maximize2, Menu, MessageCircle, Mic, Minus, Monitor, Network, NotebookText, PanelLeft, PanelRight,
  Paperclip, Pause, Pencil, Pin, Plane, Play, Plug, Plus, QuestionMark, RefreshCw, Reply, RotateCw, ScanText,
  Scissors, Search, Send, SeparatorHorizontal, Settings, Settings2, Share, Shield, ShoppingCart, Smartphone,
  Square, SquareKanban, SquarePen, SquareSplitHorizontal, SquareTerminal, Strikethrough, Table, TextAlignStart,
  TextQuote, Trash, TriangleExclamationPoint, Undo2, Unlink, User, Utensils, Volume1, VolumeX, WandSparkles,
  WifiOff, Wrench, X, Zap,
} from "lucide";

export type IconName =
  | "back" | "more" | "plus" | "minus" | "up" | "stop" | "model" | "terminal" | "file" | "pen" | "search" | "globe" | "attach" | "image"
  | "question" | "skill" | "spawn" | "bulb" | "wrench" | "clock" | "calendar" | "plug" | "dot" | "compact"
  | "folder" | "folderplus" | "settings" | "bots" | "inbox" | "board" | "changes" | "chart" | "loop" | "pause" | "play" | "trash" | "check" | "close" | "send"
  | "download" | "share" | "split" | "key" | "link" | "unlink" | "down" | "copy" | "paste" | "columns" | "eye" | "mic" | "fork" | "undo" | "phone" | "expand" | "external" | "reload" | "forward" | "panel" | "chevron" | "bolt" | "volume" | "mute" | "lock" | "bell" | "shield" | "conductor" | "journal" | "alert" | "compass" | "user" | "ask" | "archive" | "braces" | "sidebar" | "reply" | "flag" | "menu" | "pin" | "pushpin"
  | "compose" | "grid" | "vdots" | "camera" | "region" | "offline" | "logo" | "sliders" | "apps" | "import" | "eyeoff"
  | "hand" | "ffwd" | "translate"
  | "monitor" | "laptop"
  | "utensils" | "cart" | "dumbbell" | "plane" | "home" | "list" | "heart" | "book" | "wand"
  | "chat" | "headset" | "database" | "mail" | "cloud" | "checkcircle"
  | "note" | "bold" | "italic" | "strike" | "code" | "heading" | "quote" | "olist" | "tasks" | "table" | "hr"
  | "open" | "cut" | "selectall";

/** One glyph as Lucide ships it: the SVG elements of a 24-unit drawing, each a tag and its attributes. */
type IconNode = readonly (readonly [tag: string, attrs: Readonly<Record<string, string | number | undefined>>])[];

/** The product's own mark: a frame inside a frame around a point. Lucide has no brand marks, and this
 *  one is not an icon but the logo, so it keeps its drawing. */
const LOGO: IconNode = [["path", { d: "M10 3.5H3.5v17h17v-17H14M10 16.5H7.5v-9h9v9H14M12 11.2a.8.8 0 1 0 0 1.6.8.8 0 0 0 0-1.6z" }]];

/** `spawn` has no true equivalent in Lucide: a branch that gains a new line is the nearest to "start
 *  another agent". `stop` is the one glyph drawn solid, below, because an empty square reads as a
 *  checkbox rather than as Stop. The keys keep the order the icon picker of Settings shows them in. */
const NODES: Record<IconName, IconNode> = {
  open: LogIn,
  cut: Scissors,
  selectall: ScanText,
  hand: Hand,
  ffwd: FastForward,
  utensils: Utensils,
  cart: ShoppingCart,
  dumbbell: Dumbbell,
  plane: Plane,
  home: House,
  list: List,
  heart: Heart,
  book: Book,
  chat: MessageCircle,
  headset: Headset,
  database: Database,
  mail: Mail,
  cloud: Cloud,
  checkcircle: CircleCheck,
  note: FileText,
  bold: LetterB,
  italic: Italic,
  strike: Strikethrough,
  code: Code,
  heading: LetterH,
  quote: TextQuote,
  olist: ListOrdered,
  tasks: ListTodo,
  table: Table,
  hr: SeparatorHorizontal,
  back: ChevronLeft,
  more: Ellipsis,
  plus: Plus,
  minus: Minus,
  up: ArrowUp,
  stop: Square,
  model: Diamond,
  terminal: SquareTerminal,
  file: File,
  pen: Pencil,
  search: Search,
  globe: Globe,
  translate: Languages,
  attach: Paperclip,
  image: Image,
  question: QuestionMark,
  // The same question in a ring: what waits for the operator, where a bare "?" would read as help.
  ask: CircleQuestionMark,
  skill: Blocks,
  spawn: GitBranchPlus,
  bulb: Lightbulb,
  wand: WandSparkles,
  wrench: Wrench,
  clock: Clock,
  calendar: CalendarDays,
  plug: Plug,
  dot: Dot,
  compact: TextAlignStart,
  folder: Folder,
  folderplus: FolderPlus,
  archive: Archive,
  braces: Braces,
  settings: Settings,
  bots: Bot,
  inbox: Inbox,
  board: SquareKanban,
  changes: GitBranch,
  chart: ChartNoAxesColumn,
  loop: RefreshCw,
  pause: Pause,
  play: Play,
  trash: Trash,
  check: Check,
  close: X,
  send: Send,
  download: Download,
  share: Share,
  split: SquareSplitHorizontal,
  key: Key,
  lock: Lock,
  shield: Shield,
  link: Link,
  unlink: Unlink,
  down: ArrowDown,
  copy: Copy,
  paste: ClipboardPaste,
  fork: GitFork,
  undo: Undo2,
  reply: Reply,
  columns: Columns2,
  eyeoff: EyeOff,
  eye: Eye,
  mic: Mic,
  monitor: Monitor,
  laptop: Laptop,
  phone: Smartphone,
  expand: Maximize2,
  external: ExternalLink,
  reload: RotateCw,
  forward: ChevronRight,
  panel: PanelRight,
  sidebar: PanelLeft,
  chevron: ChevronDown,
  bolt: Zap,
  volume: Volume1,
  mute: VolumeX,
  bell: Bell,
  alert: TriangleExclamationPoint,
  conductor: Network,
  journal: NotebookText,
  compass: Compass,
  user: User,
  flag: Flag,
  menu: Menu,
  // An event's place; `pushpin` below is the drawing pin of a chat or project held at the top of the sidebar.
  pin: MapPin,
  pushpin: Pin,
  compose: SquarePen,
  grid: LayoutGrid,
  // Three tiles and a fourth still to come: "everything else".
  apps: Grid2x2Plus,
  import: Import,
  vdots: EllipsisVertical,
  region: Crop,
  camera: Camera,
  offline: WifiOff,
  logo: LOGO,
  sliders: Settings2,
};

const FILLED: ReadonlySet<IconName> = new Set<IconName>(["stop"]);

/** Every name of the set, in the order the table lists them: the icon picker of Settings → Navigation. */
export const ICON_NAMES = Object.keys(NODES) as IconName[];

export function isIconName(value: unknown): value is IconName {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(NODES, value);
}

/** The sizes the interface draws icons at. Every size a caller asks for lands on one of these, so a
 *  screen that asks for 13 or 17 draws the same icon as its neighbour and none sits half a pixel off. */
export const ICON_SIZES = [12, 14, 16, 20, 24] as const;
export type IconSize = (typeof ICON_SIZES)[number];

export const ICON_STROKE = 1.75;

/** Up to 12 draws at 12, 13 and 14 at 14, 15 to 18 at 16, 19 to 22 at 20, anything larger at 24. */
export function snapIconSize(size: number): IconSize {
  if (size <= 12) return 12;
  if (size <= 14) return 14;
  if (size <= 18) return 16;
  if (size <= 22) return 20;
  return 24;
}

function nodeOf(name: IconName, path?: string | null): IconNode {
  return path ? [["path", { d: path }]] : NODES[name];
}

function attributes(attrs: Readonly<Record<string, string | number | undefined>>): string {
  return Object.entries(attrs)
    .filter(([, value]) => value !== undefined)
    .map(([key, value]) => ` ${key}="${String(value).replace(/&/g, "&amp;").replace(/"/g, "&quot;")}"`)
    .join("");
}

/** The same drawing as markup, for HTML the app writes as a string (an answer's rendered Markdown). */
export function iconSvg(name: IconName, size = 16): string {
  const px = snapIconSize(size);
  const fill = FILLED.has(name) ? "currentColor" : "none";
  const shapes = NODES[name].map(([tag, attrs]) => `<${tag}${attributes(attrs)}/>`).join("");
  return `<svg class="ic ic-${name}" viewBox="0 0 24 24" width="${px}" height="${px}" fill="${fill}" stroke="currentColor" stroke-width="${ICON_STROKE}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${shapes}</svg>`;
}

/** `path` is a page's own glyph, a `d` string already checked by `iconPath` in navconfig.ts: drawn
 *  in the same stroke as the set, so a page's icon on the rail does not look pasted in. */
export function Icon({ name, size = 16, path }: { name: IconName; size?: number; path?: string | null }) {
  const px = snapIconSize(size);
  return (
    <svg className={`ic ${path ? "ic-custom" : `ic-${name}`}`} viewBox="0 0 24 24" width={px} height={px} fill={!path && FILLED.has(name) ? "currentColor" : "none"} stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {nodeOf(name, path).map(([tag, attrs], index) => createElement(tag, { key: index, ...attrs }))}
    </svg>
  );
}
