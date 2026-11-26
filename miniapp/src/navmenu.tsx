// The More launcher: every screen and page of the app in its group, with a line saying what it is,
// and a pin on each that puts it in the sidebar (or takes it out). A search field narrows the list to
// the destinations that match; what is not a destination (a chat, a setting) is the command palette's,
// one row away. The desktop opens it beside the sidebar's More row (NavMenu); the phone's drawer
// opens the same body as a sheet over the drawer; the pin is the same on both, the sidebar's pinned list.
//
// A dialog, not a menu. ARIA's menu may hold menu items and nothing else, and a screen reader in a
// menu takes the letter keys for itself, so the search field on top could not be typed into; a
// labelled dialog with a field and groups of links reads correctly and keeps the arrows ours.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import "./navlauncher.css";
import { Popover, useContextActions, useLayer, type MenuItem, type VirtualAnchor } from "./ui/primitives";
import { Icon } from "./icons";
import { t } from "./i18n";
import { SEARCH, moveInGrid } from "./navigation";
import { type NavItem, type ResolvedNav, PAGES_GROUP, MENU_GROUPS, launcherLayout } from "./navconfig";
import { frequent, useFrequentState } from "./navfrequent";
import { placeItem } from "./navplace";
import { navigate, pathFor } from "./router";
import { SelfDevMode, screenTag } from "./capabilities";
import { go, screenTitle } from "./shell";
import { KEYS } from "./sidebar";

const CHIPS = 4;

export type LauncherProps = {
  nav: ResolvedNav;
  /** What the sidebar beside the launcher already shows: its pinned rows. */
  placed: NavItem[];
  /** The id of the item the route stands on (navconfig's `currentId`). */
  current: string;
  selfdev: SelfDevMode;
  /** Goes to an address and closes whatever the launcher sits in. */
  onGo: (path: string) => void;
  /** Puts an item on the surface beside the launcher or takes it off; resolves to whether it took. */
  onPlace: (id: string, on: boolean) => Promise<boolean>;
  /** Hands what was typed to the command palette, which searches chats and settings too. */
  onEverywhere?: (query: string) => void;
  onClose: () => void;
  phone?: boolean;
};

/** What a destination is, in a line. A page has no description of its own to offer. */
function describe(it: NavItem): string {
  return t(it.kind === "page" ? "nav.desc.page" : `nav.desc.${it.id}`);
}

const GROUP_KEYS = [...MENU_GROUPS.map((g) => g.key), PAGES_GROUP];

/** The launcher's body: the field, the destinations most used as chips, every destination in its
 *  group with a pin, and the foot. Everything one Tab stop away from the field is reached with the
 *  arrows as one grid, in the order it is drawn (navigation.ts's `moveInGrid`); Tab jumps from one
 *  group to the next. */
export function Launcher({ nav, placed, current, selfdev, onGo, onPlace, onEverywhere, onClose, phone = false }: LauncherProps) {
  const root = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const state = useFrequentState();
  const layout = useMemo(() => launcherLayout(nav, placed), [nav, placed]);
  // A pin shows what was asked at once; the host's answer takes over when it arrives (the sidebar
  // redraws from the same answer) or, refused, the pin goes back to what was.
  const [asked, setAsked] = useState<Record<string, boolean>>({});
  const isPlaced = (id: string) => asked[id] ?? layout.placed.has(id);
  const place = (id: string) => {
    const on = !isPlaced(id);
    setAsked((was) => ({ ...was, [id]: on }));
    void onPlace(id, on).finally(() => setAsked((was) => {
      const { [id]: _done, ...rest } = was;
      return rest;
    }));
  };

  const needle = query.trim().toLowerCase();
  const searching = needle.length > 0;
  const chips = useMemo(() => {
    const byId = new Map(nav.shown.map((it) => [it.id, it]));
    return frequent(state, nav.shown.map((it) => it.id), Date.now(), CHIPS, layout.fallback).map((id) => byId.get(id)!);
  }, [state, nav, layout]);
  const groups = useMemo(
    () => GROUP_KEYS
      .map((key) => ({ key, items: nav.shown.filter((it) => it.group === key && (!searching || `${it.title} ${describe(it)}`.toLowerCase().includes(needle))) }))
      .filter((g) => g.items.length > 0),
    [nav, searching, needle],
  );

  useEffect(() => {
    // The field takes focus on a desktop; on a phone that would raise the keyboard over half the
    // sheet before the operator has decided to type.
    if (!phone) field.current?.focus();
  }, [phone]);

  // Esc clears a typed search before it closes anything. The layer is armed one render after the
  // body mounts so it lands above the box's own: effects run child first, and a layer pushed in the
  // first pass would sit under the box's and never see the key.
  const [armed, setArmed] = useState(false);
  useEffect(() => setArmed(true), []);
  const escape = () => {
    if (query) {
      setQuery("");
      field.current?.focus();
    } else onClose();
  };

  const cells = () => Array.from(root.current?.querySelectorAll<HTMLElement>("[data-launch]") ?? []).filter((el) => el.getClientRects().length > 0);
  const sections = () => Array.from(root.current?.querySelectorAll<HTMLElement>("[data-launch-group]") ?? []).filter((el) => el.getClientRects().length > 0);
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const inField = e.target === field.current;
    // Tab walks the groups, not every pin: the first thing in the next group, round again at the end.
    if (e.key === "Tab" && !phone) {
      const parts = sections();
      if (!parts.length) return;
      const now = parts.findIndex((el) => el.contains(e.target as Node));
      const step = e.shiftKey ? -1 : 1;
      const next = now < 0 ? (step > 0 ? 0 : parts.length - 1) : (now + step + parts.length) % parts.length;
      e.preventDefault();
      parts[next].querySelector<HTMLElement>("[data-launch]")?.focus();
      return;
    }
    if (inField && e.key === "Enter") {
      e.preventDefault();
      cells()[0]?.click();
      return;
    }
    // In the field the side arrows, Home and End move the caret; only Down leaves it.
    if (inField && e.key !== "ArrowDown") return;
    if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    const list = cells();
    const from = inField ? SEARCH : list.indexOf(e.target as HTMLElement);
    if (!inField && from < 0) return;
    // Taken whether or not it moves: the box's own arrows would otherwise step through its buttons.
    e.preventDefault();
    const boxes = list.map((el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height };
    });
    const next = moveInGrid(boxes, from, e.key);
    if (next === null) return;
    if (next === SEARCH) field.current?.focus();
    else list[next]?.focus();
  };

  const open = (e: React.MouseEvent, path: string) => {
    if (phone) {
      e.preventDefault();
      onGo(path);
      return;
    }
    go(e, path);
    if (e.defaultPrevented) onClose();
  };

  const customise = pathFor("settings", "navigation");
  const about = pathFor("settings", "about");
  const settings = pathFor("settings");
  const row = (it: NavItem) => (
    <Destination key={it.id} it={it} phone={phone} on={current === it.id} placed={isPlaced(it.id)} selfdev={selfdev} open={open} onGo={onGo} onPlace={() => place(it.id)} />
  );
  return (
    <div ref={root} className={`launch ${phone ? "phone" : ""}`} onKeyDown={onKey}>
      {armed && <EscapeLayer onEscape={escape} />}
      <label className="launch-q">
        <Icon name="search" size={phone ? 20 : 16} />
        <input
          ref={field}
          type="search"
          className="launch-field"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("nav.launch.search")}
          aria-label={t("nav.launch.search")}
          enterKeyHint="go"
          autoComplete="off"
          spellCheck={false}
        />
        {!phone && <kbd aria-hidden>{KEYS.escape}</kbd>}
      </label>
      <div className="launch-body">
        {!searching && chips.length > 0 && (
          <section className="launch-sec launch-frequent" data-launch-group aria-labelledby="launch-frequent">
            <div className="launch-label" id="launch-frequent">{t("nav.launch.frequent")}</div>
            <div className="launch-chips">
              {chips.map((it) => (
                <a key={it.id} href={it.path} className="launch-chip" data-launch="" data-chip={it.id} onClick={(e) => open(e, it.path)}>
                  <Icon name={it.icon} size={16} path={it.iconPath} />
                  {it.title}
                </a>
              ))}
            </div>
          </section>
        )}
        {groups.map((g) => (
          <section key={g.key} className="launch-sec" data-launch-group data-group={g.key} aria-labelledby={`launch-group-${g.key}`}>
            <div className="launch-label" id={`launch-group-${g.key}`}>{t(`nav.group.${g.key}`)}</div>
            <div className="launch-grid">{g.items.map(row)}</div>
          </section>
        ))}
        {searching && groups.length === 0 && <div className="launch-empty">{t("shell.search.nomatch")}</div>}
        {searching && onEverywhere && (
          <section className="launch-sec" data-launch-group>
            <button type="button" className="launch-everywhere" data-launch="" onClick={() => onEverywhere(query.trim())}>
              <Icon name="search" size={16} />
              <span className="truncate">{t("nav.launch.everywhere", { q: query.trim() })}</span>
            </button>
          </section>
        )}
      </div>
      <div className="launch-foot" data-launch-group>
        {phone && (
          <a href={settings} data-launch="" data-nav="settings" className="launch-foot-row" onClick={(e) => open(e, settings)}>
            <Icon name="settings" size={20} />
            <span className="truncate">{screenTitle("settings")}</span>
            <span className="launch-chev"><Icon name="forward" size={16} /></span>
          </a>
        )}
        <a href={customise} data-launch="" data-nav="customise" className="launch-foot-row" onClick={(e) => open(e, customise)}>
          <Icon name="sliders" size={phone ? 20 : 16} />
          <span className="truncate">{t("nav.launch.customise")}</span>
        </a>
        {phone && (
          <a href={about} data-launch="" data-nav="about" className="launch-foot-row" onClick={(e) => open(e, about)}>
            <Icon name="question" size={20} />
            <span className="truncate">{t("settings.sec.about")}</span>
          </a>
        )}
      </div>
    </div>
  );
}

function EscapeLayer({ onEscape }: { onEscape: () => void }) {
  useLayer(onEscape);
  return null;
}

/** One destination: its glyph, its name over a line saying what it is, a count where it has one, and
 *  the pin beside it. The pin is a sibling of the link, not inside it, because a button inside a link
 *  is two controls a screen reader announces as one. */
function Destination({ it, phone, on, placed, selfdev, open, onGo, onPlace }: { it: NavItem; phone: boolean; on: boolean; placed: boolean; selfdev: SelfDevMode; open: (e: React.MouseEvent, path: string) => void; onGo: (path: string) => void; onPlace: () => void }) {
  const link = useRef<HTMLAnchorElement>(null);
  const tag = it.screen ? screenTag(it.screen, selfdev, []) : "";
  const n = it.badge;
  const pinLabel = t(`nav.launch.${placed ? "unpin" : "pin"}.named`, { name: it.title });
  const items = useMemo<MenuItem[]>(() => [
    { label: t("common.open"), icon: "open", onSelect: () => onGo(it.path) },
    { label: t("nav.ctx.newtab"), icon: "external", onSelect: () => void window.open(new URL(it.path, location.href).href, "_blank", "noopener") },
    "-",
    { label: t(placed ? "nav.launch.unpin" : "nav.launch.pin"), icon: "pushpin", onSelect: onPlace },
    { label: t("nav.ctx.customise"), icon: "sliders", onSelect: () => onGo(pathFor("settings", "navigation")) },
  ], [it.path, placed, onGo, onPlace]);
  useContextActions(link, items);
  const count: ReactNode = n > 0 && <span className={`launch-count ${it.screen === "inbox" || it.screen === "changes" ? "attn" : ""}`} aria-hidden>{n > 99 ? "99+" : n}</span>;
  return (
    <div className={`launch-cell ${on ? "active" : ""} ${placed ? "pinned" : ""}`}>
      <a
        ref={link}
        href={it.path}
        data-launch=""
        data-nav-item={it.id}
        data-screen={it.id}
        data-placed={placed ? "" : undefined}
        className="launch-row"
        aria-current={on ? "page" : undefined}
        aria-label={[it.title, n > 0 ? String(n) : "", describe(it)].filter(Boolean).join(" · ")}
        onClick={(e) => open(e, it.path)}
      >
        <Icon name={it.icon} size={16} path={it.iconPath} />
        <span className="launch-text">
          <span className="launch-name">{it.title}{tag && <span className="beta-tag">{t(tag)}</span>}</span>
          <span className="launch-desc">{describe(it)}</span>
        </span>
        {count}
      </a>
      <button type="button" className="launch-pin" data-launch="" aria-pressed={placed} aria-label={pinLabel} title={pinLabel} onClick={onPlace}>
        <Icon name="pushpin" size={14} />
      </button>
    </div>
  );
}

/** Where the popover stands: its left edge a little way out from the sidebar's right edge, its top
 *  level with the More row, so it grows beside the row that opened it however wide the sidebar was
 *  dragged. With no row (the column is folded and a shortcut opened it) it hangs from the window's
 *  left edge. */
function anchorFor(opener: HTMLElement | null | undefined): VirtualAnchor {
  return {
    contextElement: opener ?? undefined,
    getBoundingClientRect: () => {
      const row = opener?.getBoundingClientRect();
      const edge = opener?.closest("nav")?.getBoundingClientRect().right ?? 0;
      const top = row?.top ?? 56;
      const height = row?.height ?? 0;
      return { x: edge, y: top, width: 0, height, top, bottom: top + height, left: edge, right: edge };
    },
  };
}

/** The desktop's launcher, beside the sidebar's More row. */
export function NavMenu({ current, nav, selfdev, onClose, opener, onSearchAll }: { current: string; nav: ResolvedNav; selfdev: SelfDevMode; onClose: () => void; opener?: HTMLElement | null; onSearchAll: (query: string) => void }) {
  const anchor = useMemo(() => anchorFor(opener), [opener]);
  return (
    <Popover anchor={anchor} onClose={onClose} label={t("nav.more")} role="dialog" side="right" align="start" width={560} offset={8} className="navmenu">
      <Launcher
        nav={nav}
        placed={nav.pinned}
        current={current}
        selfdev={selfdev}
        onGo={(path) => { onClose(); navigate(path); }}
        onPlace={(id, on) => placeItem(nav, id, on ? "pinned" : "more")}
        onEverywhere={(query) => { onClose(); onSearchAll(query); }}
        onClose={onClose}
      />
    </Popover>
  );
}
