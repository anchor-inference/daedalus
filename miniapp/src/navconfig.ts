// Where each destination sits: the desktop rail, its More menu, the phone drawer and the phone's More
// sheet, from three inputs — the layout the app ships with, the operator's `[navigation]` section of
// the configuration, and the pages the agent has published. Every place that draws navigation asks
// `resolveNav` and nothing else, so the rail and the drawer cannot disagree about what exists.
//
// No runtime import of the router or the store: this module is a pure function a test can load
// without a browser (router.ts reads window.history the moment it loads).

import { type SelfDevMode, visibleScreens } from "./capabilities";
import { lang, t } from "./i18n";
import { type IconName, isIconName } from "./icons";
import type { Screen } from "./router";

export type Counts = { inbox?: number; changes?: number; services?: number; agents?: number };

export const ICONS: Record<Screen, IconName> = { agents: "bots", voice: "mic", inbox: "inbox", board: "board", terminals: "terminal", harnesses: "wrench", changes: "changes", schedules: "clock", calendar: "calendar", diagrams: "pen", services: "globe", memory: "bulb", usage: "chart", health: "check", settings: "settings", orchestration: "compass", browser: "globe", page: "file" };

/** A destination's name, in the reader's language. */
export function screenTitle(s: Screen): string {
  return t(`nav.${s}`);
}

export function countFor(s: Screen, counts: Counts): number {
  if (s === "inbox") return counts.inbox ?? 0;
  if (s === "changes") return counts.changes ?? 0;
  if (s === "services") return counts.services ?? 0;
  return 0;
}

/** The desktop rail as it ships, under the two modes. */
export const DESKTOP_RAIL: Screen[] = ["inbox", "board", "calendar", "diagrams", "changes", "terminals", "voice", "schedules"];

/** Every destination the operator may place, grouped as the More menu heads them (`nav.group.*`).
 *  The two modes and Settings are not here: they are the frame of the rail, not items on it. */
export const MENU_GROUPS: { key: string; items: Screen[] }[] = [
  { key: "work", items: ["voice", "inbox", "board", "calendar", "diagrams", "terminals", "harnesses"] },
  { key: "autonomy", items: ["changes", "schedules", "services"] },
  { key: "knowledge", items: ["memory"] },
  { key: "observe", items: ["usage", "health"] },
];
/** The group a page is listed under in the More menu. */
export const PAGES_GROUP = "pages";

export const NAV_SCREENS: Screen[] = MENU_GROUPS.flatMap((g) => g.items);

/** The phone drawer's places as they ship, per mode; the phone's More sheet holds `PHONE_MORE`. */
export const PHONE_AGENTS: Screen[] = ["inbox", "terminals", "board", "voice", "usage"];
export const PHONE_ORCHESTRATION: Screen[] = ["inbox", "terminals", "usage"];
export const PHONE_MORE: Screen[] = ["calendar", "diagrams", "changes", "schedules", "services", "memory", "harnesses", "health"];

/** The server's `[navigation]` section. Every field may be missing: an older host has none of it. */
export type NavConfig = { rail?: string[]; more?: string[]; drawer?: string[]; hidden?: string[]; icons?: Record<string, string> };

/** One entry of `GET /api/pages`. The title may be one string or one per language. */
export type PageInfo = { slug: string; title?: string | Record<string, string> | null; icon?: string | null; icon_path?: string | null; path?: string | null; version?: number | string | null; updated_at?: string | null };

export type NavItem = {
  id: string;
  kind: "screen" | "page";
  /** The built-in screen, or null for a page. */
  screen: Screen | null;
  title: string;
  icon: IconName;
  /** A page's own glyph, drawn in place of `icon`. */
  iconPath: string | null;
  path: string;
  badge: number;
  /** The More menu's heading for it. */
  group: string;
};

export type ResolvedNav = {
  rail: NavItem[];
  more: { key: string; items: NavItem[] }[];
  /** The phone drawer's places, per mode. */
  drawer: { agents: NavItem[]; orchestration: NavItem[] };
  phoneMore: NavItem[];
  /** Every item that is shown somewhere, in the order the More menu lists them. */
  shown: NavItem[];
  /** Every item this installation has, hidden ones included, for Settings → Navigation. */
  catalogue: NavItem[];
  hidden: Set<string>;
};

export const ICON_PATH_MAX = 1000;
const PATH_CHARS = /^[Mm][MmLlHhVvCcSsQqTtAaZz0-9.,\s-]*$/;
const NUMBER = /-?(?:\d+\.?\d*|\.\d+)/g;

/** A page's own glyph, or null when it is not one we will draw.
 *
 *  One path's `d` and nothing else: only path commands, digits and separators, so nothing that could
 *  close the attribute or open an element survives, and every coordinate on the 24-unit grid the set
 *  is drawn on — a glyph drawn on a 1000-unit grid would be a speck in the corner of its tile. */
export function iconPath(d: unknown): string | null {
  if (typeof d !== "string") return null;
  const value = d.trim().replace(/\s+/g, " ");
  if (!value || value.length > ICON_PATH_MAX || !PATH_CHARS.test(value)) return null;
  const numbers = value.match(NUMBER) ?? [];
  if (numbers.length === 0 || numbers.some((n) => Math.abs(Number(n)) > 24)) return null;
  return value;
}

/** Where a page opens: outside every screen's own address, so a slug can never shadow one. */
export function pagePath(slug: string): string {
  return `/app/p/${encodeURIComponent(slug)}`;
}

export function pageId(slug: string): string {
  return `page:${slug}`;
}

function pageTitle(page: PageInfo): string {
  const title = page.title;
  if (typeof title === "string" && title.trim()) return title;
  if (title && typeof title === "object") {
    const own = title[lang()] ?? title.en ?? Object.values(title)[0];
    if (typeof own === "string" && own.trim()) return own;
  }
  return page.slug;
}

function list(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** The items this installation has, in the More menu's order: screens by group, then the pages. */
export function catalogue(pages: PageInfo[] | null | undefined, selfdev: SelfDevMode, config: NavConfig | null | undefined = null, counts: Counts = {}): NavItem[] {
  const icons = config?.icons && typeof config.icons === "object" ? config.icons : {};
  const override = (id: string): IconName | null => (isIconName(icons[id]) ? (icons[id] as IconName) : null);
  const items: NavItem[] = [];
  for (const group of MENU_GROUPS) {
    for (const s of visibleScreens(group.items, selfdev)) {
      items.push({ id: s, kind: "screen", screen: s, title: screenTitle(s), icon: override(s) ?? ICONS[s], iconPath: null, path: `/app/${s}`, badge: countFor(s, counts), group: group.key });
    }
  }
  const seen = new Set<string>();
  for (const page of Array.isArray(pages) ? pages : []) {
    if (!page || typeof page.slug !== "string" || !page.slug || seen.has(page.slug)) continue;
    seen.add(page.slug);
    const id = pageId(page.slug);
    const named = override(id);
    const own = named ? null : iconPath(page.icon_path);
    items.push({
      id,
      kind: "page",
      screen: null,
      title: pageTitle(page),
      icon: named ?? (isIconName(page.icon) ? page.icon : "file"),
      iconPath: own,
      path: typeof page.path === "string" && page.path.startsWith("/app/p/") ? page.path : pagePath(page.slug),
      badge: 0,
      group: PAGES_GROUP,
    });
  }
  return items;
}

/** `ordered` first, in its order, then the rest of `items` in theirs. */
function orderBy(items: NavItem[], ordered: string[]): NavItem[] {
  const at = new Map(ordered.map((id, i) => [id, i]));
  const listed = items.filter((it) => at.has(it.id)).sort((a, b) => at.get(a.id)! - at.get(b.id)!);
  return [...listed, ...items.filter((it) => !at.has(it.id))];
}

export function resolveNav(input: { config?: NavConfig | null; pages?: PageInfo[] | null; selfdev: SelfDevMode; counts?: Counts }): ResolvedNav {
  const config = input.config ?? {};
  const all = catalogue(input.pages, input.selfdev, config, input.counts ?? {});
  const hidden = new Set(list(config.hidden));
  const rail = list(config.rail);
  const more = list(config.more);
  const drawer = list(config.drawer);
  const shown = all.filter((it) => !hidden.has(it.id));
  const byId = new Map(shown.map((it) => [it.id, it]));

  // Desktop. Hidden wins, then the rail, then More; an item mentioned nowhere keeps the place it
  // ships with, which for a page is More — a new page should not push the operator's rail around.
  const onRail = (it: NavItem) => rail.includes(it.id) || (!more.includes(it.id) && it.screen !== null && DESKTOP_RAIL.includes(it.screen));
  const railItems = [
    ...rail.map((id) => byId.get(id)).filter((it): it is NavItem => !!it),
    ...DESKTOP_RAIL.map((s) => byId.get(s)).filter((it): it is NavItem => !!it && !rail.includes(it.id) && onRail(it)),
  ];
  const inMore = shown.filter((it) => !onRail(it));
  const sections = [...MENU_GROUPS.map((g) => g.key), PAGES_GROUP]
    .map((key) => ({ key, items: orderBy(inMore.filter((it) => it.group === key), more) }))
    .filter((g) => g.items.length > 0);

  // Phone. A drawer list that was set is the whole drawer, for both modes; until then each mode
  // keeps the drawer it ships with, and orchestration's leaves the board and the voice page out.
  let agents: NavItem[];
  let orchestration: NavItem[];
  if (drawer.length) {
    agents = drawer.map((id) => byId.get(id)).filter((it): it is NavItem => !!it);
    orchestration = agents;
  } else {
    agents = PHONE_AGENTS.map((s) => byId.get(s)).filter((it): it is NavItem => !!it);
    orchestration = PHONE_ORCHESTRATION.map((s) => byId.get(s)).filter((it): it is NavItem => !!it);
  }
  const inDrawer = new Set(agents.map((it) => it.id));
  const phoneMore = drawer.length
    ? shown.filter((it) => !inDrawer.has(it.id))
    : [
        ...PHONE_MORE.map((s) => byId.get(s)).filter((it): it is NavItem => !!it),
        // Placed nowhere by the shipped layout: a screen of a later build, and every page.
        ...shown.filter((it) => !inDrawer.has(it.id) && !(it.screen && PHONE_MORE.includes(it.screen))),
      ];

  return { rail: railItems, more: sections, drawer: { agents, orchestration }, phoneMore, shown, catalogue: all, hidden };
}

/** The id of the item the route stands on: a screen's name, or `page:<slug>`. */
export function currentId(screen: Screen, detail: string | null): string {
  return screen === "page" && detail ? pageId(detail) : screen;
}

/** The section Settings → Navigation writes, from the order and placement it shows.
 *
 *  Ids the screen did not show — a page that is gone for now, a screen of another build — are kept
 *  where they were: a removed page that comes back finds its place, and a page list that failed to
 *  load for one save does not wipe the layout. */
export function desktopLayout(previous: NavConfig, order: string[], placement: Record<string, "rail" | "more" | "hidden">, known: Set<string>): Pick<Required<NavConfig>, "rail" | "more" | "hidden"> {
  const keep = (field: "rail" | "more" | "hidden") => list(previous[field]).filter((id) => !known.has(id));
  return {
    rail: [...order.filter((id) => placement[id] === "rail"), ...keep("rail")],
    more: [...order.filter((id) => placement[id] === "more"), ...keep("more")],
    hidden: [...order.filter((id) => placement[id] === "hidden"), ...keep("hidden")],
  };
}

export function drawerLayout(previous: NavConfig, on: string[], known: Set<string>): string[] {
  return [...on, ...list(previous.drawer).filter((id) => !known.has(id))];
}

/** Moves the item at `from` to `to`, the rest closing up behind it. */
export function moveItem<T>(items: T[], from: number, to: number): T[] {
  if (from === to || from < 0 || from >= items.length) return items;
  const next = [...items];
  const [it] = next.splice(from, 1);
  next.splice(Math.max(0, Math.min(to, next.length)), 0, it);
  return next;
}
