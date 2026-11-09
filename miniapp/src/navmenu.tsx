// The More launcher: one search field over every destination, the ones used most as tiles, and the
// whole catalogue below them, every screen and page whether or not the rail already shows it, so the
// operator never has to remember which of the two places a screen was put in. The desktop opens it
// beside the rail's More button (NavMenu); the phone's drawer opens the same body as a sheet.
//
// A dialog, not a menu. ARIA's menu may hold menu items and nothing else, and a screen reader in a
// menu takes the letter keys for itself, so the search field on top could not be typed into; a
// labelled dialog with a field and groups of links reads correctly and keeps the arrows ours.

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import "./navlauncher.css";
import { Overlay, trapDialogTab, useLayer, type MenuItem } from "./ui/dialogs";
import { useContextActions } from "./ui/context-menu";
import { Icon } from "./icons";
import { lang, t } from "./i18n";
import { SEARCH, moveInGrid } from "./navigation";
import { type NavItem, type ResolvedNav, launcherLayout } from "./navconfig";
import { frequent, isPinned, pin, useFrequentState } from "./navfrequent";
import { Screen, navigate, pathFor } from "./router";
import { SelfDevMode, screenTag } from "./capabilities";
import { type PaletteItem, go, matchPalette, screenTitle } from "./shell";
import { KEYS } from "./rail";

const BETA: Screen[] = [];
const TILES = 4;
/** The settings shortcut, written the way the search's is: the same modifier, a comma for the K. */
const SETTINGS_KEY = KEYS.search.replace(/K$/, ",");

type SettingsIndex = typeof import("./screens/settingsindex");

export type LauncherProps = {
  nav: ResolvedNav;
  /** What the surface beside the launcher already shows: the rail, or the phone drawer's list. */
  placed: NavItem[];
  /** The id of the item the route stands on (navconfig's `currentId`). */
  current: string;
  selfdev: SelfDevMode;
  /** The Ctrl K palette's items, read once when the launcher opens. */
  search?: () => PaletteItem[];
  /** Goes to an address and closes whatever the launcher sits in. */
  onGo: (path: string) => void;
  /** Closes the launcher and then runs a palette item's own action. */
  onRun: (run: () => void) => void;
  onClose: () => void;
  phone?: boolean;
};

/** The launcher's body: the field, the tiles and the catalogue (or the search's results), and the
 *  foot with Settings, Customise and About. Every item one Tab stop away from the field is reached
 *  with the arrows as one grid, in the order it is drawn (navigation.ts's `moveInGrid`). */
export function Launcher({ nav, placed, current, selfdev, search, onGo, onRun, onClose, phone = false }: LauncherProps) {
  const root = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const state = useFrequentState();
  const layout = useMemo(() => launcherLayout(nav, placed), [nav, placed]);
  const tiles = useMemo(() => {
    const byId = new Map(layout.catalogue.map((it) => [it.id, it]));
    return frequent(state, layout.catalogue.map((it) => it.id), Date.now(), TILES, layout.fallback).map((id) => byId.get(id)!);
  }, [state, layout]);
  const listed = useMemo(() => {
    const onTiles = new Set(tiles.map((it) => it.id));
    return layout.catalogue.filter((it) => !onTiles.has(it.id));
  }, [tiles, layout]);
  // The palette's items as the shell knows them at the moment of opening: the sessions, projects and
  // terminals it already holds. Nothing is fetched for the launcher.
  const items = useMemo(() => search?.() ?? [], []); // eslint-disable-line react-hooks/exhaustive-deps -- read once per opening
  // The settings index is a large table only Settings needs; it is fetched the first time a word is
  // typed here, not with the shell.
  const [index, setIndex] = useState<SettingsIndex | null>(null);
  const searching = query.trim().length > 0;
  useEffect(() => {
    if (!searching || index) return;
    let live = true;
    void import("./screens/settingsindex").then((module) => { if (live) setIndex(module); }).catch(() => {});
    return () => { live = false; };
  }, [searching, index]);
  const results = useMemo<PaletteItem[]>(() => {
    if (!searching) return [];
    const found = matchPalette(items, query, 10);
    if (!index) return found;
    const words = query.trim().toLowerCase();
    const here = lang();
    const sections = index.INDEX_SECTIONS.filter((section) => [section.title.en, section.title.ru, ...section.synonyms].join("\n").toLowerCase().includes(words)).slice(0, 3);
    const titleOf = (id: string) => index.INDEX_SECTIONS.find((section) => section.id === id)?.title[here] ?? id;
    const fields = index.matchFields(query).slice(0, 6 - sections.length);
    return [
      ...found,
      ...sections.map((section) => ({ id: `set-${section.id}`, label: section.title[here], hint: screenTitle("settings"), icon: section.icon, run: () => onGo(pathFor("settings", section.id)) })),
      ...fields.map((f) => ({ id: `set-${f.section}-${f.anchor}`, label: f.title[here], hint: titleOf(f.section), icon: "settings" as const, run: () => onGo(index.fieldPath(f)) })),
    ];
  }, [searching, items, query, index, onGo]);

  useEffect(() => {
    // The field takes focus on a desktop; on a phone that would raise the keyboard over half the
    // sheet before the operator has decided to type.
    if (!phone) field.current?.focus();
  }, [phone]);

  // Esc clears a typed search before it closes anything. The layer is armed one render after the
  // body mounts so it lands above the phone sheet's own: effects run child first, and a layer pushed
  // in the first pass would sit under the sheet's and never see the key.
  const [armed, setArmed] = useState(false);
  useEffect(() => setArmed(true), []);
  const escape = () => {
    if (query) {
      setQuery("");
      field.current?.focus();
    } else onClose();
  };

  const cells = () => Array.from(root.current?.querySelectorAll<HTMLElement>("[data-launch]") ?? []).filter((el) => el.getClientRects().length > 0);
  const onKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Tab" && !phone) {
      trapDialogTab(e);
      return;
    }
    const inField = e.target === field.current;
    if (inField && e.key === "Enter" && results[0]) {
      e.preventDefault();
      onRun(results[0].run);
      return;
    }
    // In the field the side arrows, Home and End move the caret; only Down leaves it.
    if (inField && e.key !== "ArrowDown") return;
    const list = cells();
    const owner = (e.target as HTMLElement).closest<HTMLElement>(".launch-cell")?.querySelector<HTMLElement>("[data-launch]") ?? (e.target as HTMLElement);
    const from = inField ? SEARCH : list.indexOf(owner);
    if (!inField && from < 0) return;
    const boxes = list.map((el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, w: r.width, h: r.height };
    });
    const next = moveInGrid(boxes, from, e.key);
    if (next === null) return;
    e.preventDefault();
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
  return (
    <div ref={root} className={`launch ${phone ? "phone" : ""}`} onKeyDown={onKey}>
      {armed && <EscapeLayer onEscape={escape} />}
      <label className="launch-q">
        <Icon name="search" size={phone ? 20 : 18} />
        <input
          ref={field}
          type="search"
          className="launch-field"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("nav.launch.search")}
          aria-label={t("nav.launch.search")}
          aria-controls="launch-results"
          enterKeyHint="go"
          autoComplete="off"
          spellCheck={false}
        />
        {!phone && <kbd>{KEYS.search}</kbd>}
      </label>
      <div className="launch-body">
        {searching ? (
          <div id="launch-results" className="launch-results" role="group" aria-label={t("nav.launch.results")}>
            {results.map((it, i) => (
              <button key={it.id} type="button" data-launch="" data-result={it.id} className={`launch-row ${i === 0 ? "first" : ""}`} onClick={() => onRun(it.run)}>
                <Icon name={it.icon} size={16} path={it.iconPath} />
                <span className="truncate">{it.label}</span>
                {it.hint && <span className="launch-hint truncate">{it.hint}</span>}
              </button>
            ))}
            {results.length === 0 && <div className="launch-empty">{t("shell.search.nomatch")}</div>}
          </div>
        ) : (
          <>
            {tiles.length > 0 && (
              <section className="launch-sec" aria-labelledby="launch-frequent">
                <div className="launch-label"><span id="launch-frequent">{t("nav.launch.frequent")}</span><span className="launch-label-hint">{t("nav.launch.byuse")}</span></div>
                <div className="launch-tiles">
                  {tiles.map((it) => <Cell key={it.id} it={it} tile phone={phone} on={current === it.id} placed={false} pinned={isPinned(state, it.id)} selfdev={selfdev} open={open} onGo={onGo} />)}
                </div>
              </section>
            )}
            {listed.length > 0 && (
              <section className="launch-sec" aria-labelledby="launch-all">
                <div className="launch-label"><span id="launch-all">{t("nav.launch.all")}</span><span className="launch-label-hint"><span className="launch-dot" aria-hidden />{t("nav.launch.placed")}</span></div>
                <div className="launch-catalogue">
                  {listed.map((it) => <Cell key={it.id} it={it} phone={phone} on={current === it.id} placed={layout.placed.has(it.id)} pinned={isPinned(state, it.id)} selfdev={selfdev} open={open} onGo={onGo} />)}
                </div>
              </section>
            )}
          </>
        )}
      </div>
      <div className="launch-foot">
        <a href={settings} data-launch="" data-nav="settings" className="launch-row launch-settings" onClick={(e) => open(e, settings)}>
          <Icon name="settings" size={phone ? 20 : 16} />
          <span className="truncate">{screenTitle("settings")}</span>
          {phone ? <span className="launch-chev"><Icon name="forward" size={16} /></span> : <kbd>{SETTINGS_KEY}</kbd>}
        </a>
        <a href={customise} data-launch="" data-nav="customise" className="launch-row" onClick={(e) => open(e, customise)}>
          <Icon name="sliders" size={phone ? 20 : 16} />
          <span className="truncate">{t("nav.customise")}</span>
        </a>
        <a href={about} data-launch="" data-nav="about" className="launch-row" onClick={(e) => open(e, about)}>
          <Icon name="question" size={phone ? 20 : 16} />
          <span className="truncate">{t("settings.sec.about")}</span>
        </a>
        {!phone && (
          <span className="launch-keys" role="note" aria-label={t("nav.launch.keys")}>
            <kbd aria-hidden>↑↓←→</kbd><kbd aria-hidden>Enter</kbd><kbd aria-hidden>Esc</kbd>
          </span>
        )}
      </div>
    </div>
  );
}

function EscapeLayer({ onEscape }: { onEscape: () => void }) {
  useLayer(onEscape);
  return null;
}

/** One destination: a tile under Frequent or a row of the catalogue, with the pin beside it. The pin
 *  is a sibling of the link, not inside it, because a button inside a link is two controls a screen
 *  reader announces as one. */
function Cell({ it, tile = false, phone, on, placed, pinned, selfdev, open, onGo }: { it: NavItem; tile?: boolean; phone: boolean; on: boolean; placed: boolean; pinned: boolean; selfdev: SelfDevMode; open: (e: React.MouseEvent, path: string) => void; onGo: (path: string) => void }) {
  const link = useRef<HTMLAnchorElement>(null);
  const tag = it.screen ? screenTag(it.screen, selfdev, BETA) : "";
  const n = it.badge;
  const tone = it.screen === "services" ? "ok" : it.screen === "changes" ? "attn" : "";
  const items = useMemo<MenuItem[]>(() => [
    { label: t("common.open"), onSelect: () => onGo(it.path) },
    { label: t("rail.ctx.newtab"), icon: "external", onSelect: () => void window.open(new URL(it.path, location.href).href, "_blank", "noopener") },
    "-",
    { label: t(pinned ? "nav.launch.unpin" : "nav.launch.pin"), icon: "pushpin", onSelect: () => pin(it.id) },
    { label: t("rail.ctx.customise"), icon: "sliders", onSelect: () => onGo(pathFor("settings", "navigation")) },
  ], [it.id, it.path, pinned, onGo]);
  useContextActions(link, items);
  const label = [it.title, n > 0 ? String(n) : "", pinned ? t("nav.launch.pinned") : "", placed ? t("nav.launch.placed") : ""].filter(Boolean).join(" · ");
  const count: ReactNode = n > 0 && <span className={`launch-count ${tone}`} aria-hidden>{n > 99 ? "99+" : n}</span>;
  return (
    <div className={`launch-cell ${tile ? "tile" : ""} ${pinned ? "pinned" : ""}`}>
      <a
        ref={link}
        href={it.path}
        data-launch=""
        data-nav-item={it.id}
        data-screen={it.id}
        data-placed={placed ? "" : undefined}
        className={tile ? `launch-tile ${on ? "active" : ""}` : `launch-row ${on ? "active" : ""}`}
        aria-current={on ? "page" : undefined}
        aria-label={label}
        title={it.title}
        onClick={(e) => open(e, it.path)}
      >
        <Icon name={it.icon} size={tile ? 22 : phone ? 20 : 16} path={it.iconPath} />
        <span className="launch-name truncate">{it.title}</span>
        {tag && <span className="beta-tag">{t(tag)}</span>}
        {count}
        {!tile && placed && <span className="launch-dot" aria-hidden />}
      </a>
      <button type="button" className="launch-pin" aria-pressed={pinned} aria-label={`${t(pinned ? "nav.launch.unpin" : "nav.launch.pin")}: ${it.title}`} title={t(pinned ? "nav.launch.unpin" : "nav.launch.pin")} onClick={() => pin(it.id)}>
        <Icon name="pushpin" size={12} />
      </button>
    </div>
  );
}

/** The desktop's launcher, beside the rail's More button. */
export function NavMenu({ current, nav, selfdev, onClose, opener, search }: { current: string; nav: ResolvedNav; selfdev: SelfDevMode; onClose: () => void; opener?: HTMLElement | null; search?: () => PaletteItem[] }) {
  const panel = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left:8, top:8 });
  // Beside the button and level with it, moved up and in as far as it takes to stay whole in the
  // window: the box itself shrinks to the window in the stylesheet, so this only has to slide it.
  useLayoutEffect(() => {
    const place = () => {
      if (!panel.current || !opener) return;
      const anchor = opener.getBoundingClientRect();
      const menu = panel.current.getBoundingClientRect();
      setPosition({ left:Math.max(8, Math.min(anchor.right + 6, window.innerWidth - menu.width - 8)), top:Math.max(8, Math.min(anchor.top, window.innerHeight - menu.height - 8)) });
    };
    place();
    const observer = new ResizeObserver(place);
    if (panel.current) observer.observe(panel.current);
    window.addEventListener("resize", place);
    return () => { observer.disconnect(); window.removeEventListener("resize", place); };
  }, [opener]);
  // When the launcher closes focus goes back to the button that opened it, or to wherever it was for
  // a keyboard shortcut. The callback's identity changes on every render of the shell and must not
  // re-run this.
  useEffect(() => {
    const from = opener ?? (document.activeElement as HTMLElement | null);
    return () => {
      from?.focus?.();
    };
  }, []);
  const placed = nav.rail;
  return (
    <Overlay>
      <div className="navmenu-backdrop" onClick={onClose} />
      <div ref={panel} className="navmenu" style={{ ...position, bottom:"auto" }} role="dialog" aria-modal="true" aria-label={t("nav.more")}>
        <Launcher
          nav={nav}
          placed={placed}
          current={current}
          selfdev={selfdev}
          search={search}
          onGo={(path) => { onClose(); navigate(path); }}
          onRun={(run) => { onClose(); run(); }}
          onClose={onClose}
        />
      </div>
    </Overlay>
  );
}
