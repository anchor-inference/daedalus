// The application shell: a page header with the screen's name and its actions, four tabs and a
// More sheet on a phone, the command palette and the keyboard. On a desktop the sessions are the
// left column (sidebar.tsx) and the destinations are a menu over the content (navmenu.tsx).

import { useEffect, useState, type ReactNode } from "react";
import { Icon, IconName } from "./icons";
import { Sheet } from "./dialogs";
import { Screen, navigate, pathFor } from "./router";
import { SelfDevMode, screenTag, visibleScreens } from "./capabilities";
import { plural, t } from "./i18n";
import { LangPicker } from "./components";
import { insideTerminal } from "./terminal/keys";
import { modeHome } from "./mode";

export type Counts = { inbox?: number; changes?: number; services?: number; agents?: number };

function tagFor(s: Screen, selfdev: SelfDevMode): string {
  return screenTag(s, selfdev, BETA);
}

export const ICONS: Record<Screen, IconName> = { agents: "bots", voice: "mic", inbox: "inbox", board: "board", terminals: "terminal", harnesses: "wrench", changes: "changes", schedules: "clock", services: "globe", memory: "bulb", usage: "chart", health: "check", settings: "settings", orchestration: "compass", browser: "globe" };

/** A destination's name, in the reader's language. The components below re-render with it because
 *  the shell's own `useLang` does; nothing here holds a translated string of its own. */
export function screenTitle(s: Screen): string {
  return t(`nav.${s}`);
}

/** On a phone the first two tabs are the two modes: the switch between them is one tap from every
 *  screen that shows the bar, and orchestration's tab carries the count of what waits there. Then
 *  Terminals and the Board. The Inbox is in More, where its count still shows on the More tab. */
const PRIMARY: Screen[] = ["agents", "orchestration", "terminals", "board"];
/** Screens that carry a beta tag beside their name: new, usable, not yet finished. */
const BETA: Screen[] = ["voice"];
const MORE: Screen[] = ["inbox", "voice", "harnesses", "changes", "schedules", "services", "memory", "usage", "health", "settings"];

export function countFor(s: Screen, counts: Counts): number {
  if (s === "inbox") return counts.inbox ?? 0;
  if (s === "changes") return counts.changes ?? 0;
  if (s === "services") return counts.services ?? 0;
  return 0;
}

export function go(e: React.MouseEvent, path: string) {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
  e.preventDefault();
  navigate(path);
}

/** The screen's name at the top, with the one or two actions that belong to it. */
export function PageHeader({ title, subtitle, actions, back, children }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; back?: string; children?: ReactNode }) {
  return (
    <header className="pagehead">
      <div className="pagehead-row">
        {back && (
          <a className="iconbtn" href={back} onClick={(e) => go(e, back)} aria-label={t("shell.back")} title={t("shell.back")}>
            <Icon name="back" />
          </a>
        )}
        <div className="pagehead-title">
          <h1>{title}</h1>
          {subtitle && <div className="sub">{subtitle}</div>}
        </div>
        {actions && <div className="pagehead-actions">{actions}</div>}
      </div>
      {children}
    </header>
  );
}

/** `waiting`: what waits for the operator in orchestration mode, shown quietly on its tab from
 *  elsewhere. `flow`: the bar stands in the column (under the main chat) rather than over the page. */
export function TabBar({ screen, counts, selfdev, onMore, moreOpen, waiting = 0 }: { screen: Screen; counts: Counts; selfdev: SelfDevMode; onMore: () => void; moreOpen: boolean; waiting?: number }) {
  const more = visibleScreens(MORE, selfdev);
  const inMore = more.includes(screen);
  const moreCount = more.reduce((n, s) => n + countFor(s, counts), 0);
  // The Inbox lives in More on a phone, so its unseen count is what the More tab shows as a number;
  // anything else counted in there (a change waiting) is a dot.
  // While the slot stands for another screen of More (Usage, Schedules…) the number would sit under
  // that screen's name and read as its own count, "Schedules 5" beside one schedule; there it is a dot.
  const unseen = inMore && screen !== "inbox" ? 0 : countFor("inbox", counts);
  return (
    <nav className="tabbar five" aria-label={t("shell.nav.primary")}>
      {PRIMARY.map((s) => {
        const n = countFor(s, counts);
        const quiet = s === "orchestration" && screen !== "orchestration" ? waiting : 0;
        // Orchestration opens at its list on a phone (Main first, then the projects), not in the main chat.
        const href = s === "orchestration" ? modeHome("orchestration", false) : pathFor(s);
        return (
          <a key={s} href={href} data-screen={s} className={screen === s && !moreOpen ? "active" : ""} aria-current={screen === s ? "page" : undefined} onClick={(e) => go(e, href)}>
            <span className="glyph">
              <Icon name={ICONS[s]} size={22} />
              {n > 0 && <span className="tab-badge">{n > 99 ? "99+" : n}</span>}
              {quiet > 0 && <span className="tab-badge mode-count" data-waiting={quiet} aria-label={plural("mode.waiting", quiet)}>{quiet > 99 ? "99+" : quiet}</span>}
            </span>
            <span className="tab-label">{screenTitle(s)}</span>
          </a>
        );
      })}
      <button className={inMore || moreOpen ? "active" : ""} onClick={onMore} aria-haspopup="dialog" aria-expanded={moreOpen}>
        <span className="glyph">
          <Icon name={inMore ? ICONS[screen] : "more"} size={22} />
          {unseen > 0 ? <span className="tab-badge" data-unseen={unseen} aria-label={t("shell.waiting", { n: moreCount })}>{unseen > 99 ? "99+" : unseen}</span>
            : moreCount > 0 && <span className="tab-badge dot" aria-label={t("shell.waiting", { n: moreCount })} />}
        </span>
        <span className="tab-label">{inMore ? screenTitle(screen) : t("nav.more")}</span>
      </button>
    </nav>
  );
}

export function MoreSheet({ screen, counts, selfdev, onClose }: { screen: Screen; counts: Counts; selfdev: SelfDevMode; onClose: () => void }) {
  return (
    <Sheet onClose={onClose} size="narrow" className="more-sheet" title={t("nav.more")}>
      <div className="more-grid">
        {visibleScreens(MORE, selfdev).map((s) => {
          const n = countFor(s, counts);
          const tag = tagFor(s, selfdev);
          return (
            <a key={s} href={pathFor(s)} className={`more-item ${screen === s ? "active" : ""}`} onClick={(e) => { go(e, pathFor(s)); onClose(); }}>
              <Icon name={ICONS[s]} size={22} />
              <span>{screenTitle(s)}</span>
              {tag && <span className="beta-tag">{t(tag)}</span>}
              {n > 0 && <span className="tab-badge">{n}</span>}
            </a>
          );
        })}
      </div>
      {/* The language is changed from here as well as from Settings: on a phone this sheet is the
          menu, and a reader who cannot read the rail cannot find a settings section either. */}
      <div className="more-lang">
        <span>{t("lang.menu")}</span>
        <LangPicker />
      </div>
    </Sheet>
  );
}

// ── command palette ──────────────────────────────────────────────────────────────────────

export type PaletteItem = { id: string; label: string; hint?: string; icon: IconName; run: () => void };

/** Ctrl/⌘ K: go somewhere by name — a screen, a session, an action. */
export function Palette({ items, onClose }: { items: PaletteItem[]; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState(0);
  const needle = q.trim().toLowerCase();
  const shown = (needle ? items.filter((it) => `${it.label} ${it.hint ?? ""}`.toLowerCase().includes(needle)) : items).slice(0, 12);
  useEffect(() => setCursor(0), [needle]);
  const run = (it: PaletteItem) => {
    onClose();
    it.run();
  };
  return (
    <Sheet ariaLabel={t("shell.search.label")} onClose={onClose} size="narrow" className="palette-sheet">
      <input
        className="field"
        autoFocus
        placeholder={t("shell.search.placeholder")}
        value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") {
            setCursor((c) => Math.min(shown.length - 1, c + 1));
            e.preventDefault();
          } else if (e.key === "ArrowUp") {
            setCursor((c) => Math.max(0, c - 1));
            e.preventDefault();
          } else if (e.key === "Enter" && shown[cursor]) run(shown[cursor]);
        }}
        aria-label={t("shell.search.label")}
      />
      <div className="palette-list" role="listbox">
        {shown.map((it, i) => (
          <button key={it.id} role="option" aria-selected={i === cursor} className={`palette-row ${i === cursor ? "on" : ""}`} onMouseEnter={() => setCursor(i)} onClick={() => run(it)}>
            <Icon name={it.icon} size={16} />
            <span className="truncate">{it.label}</span>
            {it.hint && <PaletteHint hint={it.hint} />}
          </button>
        ))}
        {shown.length === 0 && <div className="sub" style={{ padding: "10px 12px" }}>{t("shell.search.nomatch")}</div>}
      </div>
    </Sheet>
  );
}

/** A folder is cut from its start, not its end: every project lives under the same home folder, so
 *  an end-cut left six rows of an identical "/home/o…" and lost the one part that told them apart.
 *  The marks on both sides keep the leading slash where it belongs inside the right-to-left box the
 *  start-cut relies on. The full value stays in the tooltip. */
function PaletteHint({ hint }: { hint: string }) {
  const path = hint.startsWith("/") || hint.startsWith("~");
  return <span className={`sub truncate ${path ? "tail" : ""}`} title={hint}>{path ? `\u200e${hint}\u200e` : hint}</span>;
}

const GO_KEYS: Record<string, Screen> = { a: "agents", o: "orchestration", v: "voice", i: "inbox", b: "board", t: "terminals", c: "changes", m: "memory", u: "usage", s: "settings" };

/** Keyboard on a desktop: Ctrl/⌘ K opens the palette, `g` then a letter goes to a screen. Never inside a text field. */
export function useShortcuts(onPalette: () => void, selfdev: SelfDevMode) {
  useEffect(() => {
    let pendingG = 0;
    const onKey = (e: KeyboardEvent) => {
      // A focused terminal keeps Ctrl+K and every letter: they are the shell's.
      if (insideTerminal(e.target)) return;
      const t = e.target as HTMLElement | null;
      const typing = !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onPalette();
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "g") {
        pendingG = Date.now();
        return;
      }
      if (pendingG && Date.now() - pendingG < 1200 && GO_KEYS[e.key] && visibleScreens([GO_KEYS[e.key]], selfdev).length) {
        e.preventDefault();
        navigate(pathFor(GO_KEYS[e.key]));
      }
      pendingG = 0;
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onPalette, selfdev]);
}

/** Whether a media query matches, kept current as the window changes. */
export function useMedia(query: string): boolean {
  const [on, setOn] = useState(() => window.matchMedia?.(query).matches ?? false);
  useEffect(() => {
    const mq = window.matchMedia?.(query);
    if (!mq) return;
    const update = () => setOn(mq.matches);
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, [query]);
  return on;
}
