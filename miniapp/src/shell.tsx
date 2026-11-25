// The application shell: a page header with the screen's name and its actions, what the command
// palette (palette.tsx) is made of, and the keyboard. On a desktop the sessions are the left column (sidebar.tsx) and the destinations
// are a menu over the content (navmenu.tsx); on a phone both are in the drawer (drawer.tsx), which
// the header's hamburger opens.

import { useEffect, useState, type ReactNode } from "react";
import type { IconName } from "./icons";
import type { SessionSummary } from "./api";
import { IconButton } from "./ui/primitives";
import { Screen, navigate, parse, pathFor, sessionPath } from "./router";
import { SelfDevMode, visibleScreens } from "./capabilities";
import { t } from "./i18n";
import { relTime } from "./format";
import { insideTerminal } from "./terminal/keys";
import { latinKey } from "./navigation";
import { MenuButton } from "./ui/primitives";

// The names, icons and counts of the destinations live with the resolver that places them; these
// are the same objects, re-exported for the screens that have always imported them from here.
export { ICONS, countFor, screenTitle } from "./navconfig";
export type { Counts } from "./navconfig";

export function go(e: React.MouseEvent, path: string) {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
  e.preventDefault();
  navigate(path);
}

/** The width of a page's column: `list` is the centred 880 px column of lists and cards, `reading`
 *  a 760 px one for prose, `full` the whole window between 24 px margins (boards, calendars, tables). */
export type PageWidth = "list" | "reading" | "full";

/** A page's body, in one of the three column widths. It stays a `.screen` so the phone's padding, the
 *  empty-state centring and the project pages' rules keep reading it. */
export function Page({ width = "list", className = "", children }: { width?: PageWidth; className?: string; children?: ReactNode }) {
  return <div className={`screen page-${width} ${className}`.trim()}>{children}</div>;
}

/** The page's header: its name, then its filters, then its actions at the right edge.
 *
 *  One 52 px row on a desktop, with the title 24 px from the main area's left edge on every page,
 *  whatever the column below it is; `ruled` draws the line under it for the pages that scroll a
 *  list beneath. On a phone it is the top bar, the same 52 px row: a page without `back` is a
 *  top-level page and gets the hamburger that opens the drawer, since the drawer is the only way to
 *  the other destinations there; the title stands beside it, and the filters sit in a row under it,
 *  because a phone has no room beside a title for them. A screen drawn for the phone alone heads
 *  itself with TopBar (below), which draws this same markup.
 *
 *  `lead` stands between the back or menu button and the title, so a face beside a two-line title
 *  keeps both lines beside it instead of the subtitle wrapping under the face; with a `lead` the
 *  subtitle is under the name on a desktop too. Without one, `subtitle` is a count or a status,
 *  beside the title on a desktop and under it on a phone. */
export function PageHeader({ title, subtitle, filters, actions, ruled, back, lead }: { title: ReactNode; subtitle?: ReactNode; filters?: ReactNode; actions?: ReactNode; ruled?: boolean; back?: string; lead?: ReactNode }) {
  const phone = useMedia("(max-width: 1023px)");
  return (
    <header className={ruled ? "pagehead ruled" : "pagehead"}>
      <div className="pagehead-row">
        {back ? (
          <IconButton icon="back" label={t("shell.back")} href={back} />
        ) : phone && <MenuButton />}
        {lead}
        <div className={lead ? "pagehead-title stacked" : "pagehead-title"}>
          <h1>{title}</h1>
          {subtitle && <div className="sub">{subtitle}</div>}
        </div>
        {!phone && filters && <div className="pagehead-filters">{filters}</div>}
        {actions && <div className="pagehead-actions">{actions}</div>}
      </div>
      {phone && filters}
    </header>
  );
}

// ── the phone's own bar ───────────────────────────────────────────────────────────────────

export type TopBarProps = {
  title: ReactNode;
  /** The line under the title: a count, the live state of a page. */
  sub?: ReactNode;
  /** The line over the title: where the thing lives (a chat's project), read before its name. */
  over?: ReactNode;
  /** A pushed page: the back arrow instead of the hamburger. */
  back?: () => void;
  /** The way out drawn as another glyph and name (a selection's ✕ "Done selecting"). */
  backIcon?: IconName;
  backLabel?: string;
  /** No hamburger and no back (a full-screen flow with its own way out). */
  bare?: boolean;
  pip?: boolean;
  /** The title is a button when it holds more than it shows: the full title, the model, the live state. */
  onTitle?: () => void;
  titleLabel?: string;
  /** At most two IconButtons; everything else belongs in a ⋮ sheet. */
  actions?: ReactNode;
  className?: string;
};

/** A screen drawn for the phone heads itself with this: the page header's own markup and classes
 *  (`pagehead`, `pagehead-row`, `pagehead-title`, `pagehead-actions`), so it is the same 52 px bar as
 *  every page's, from the same rules. The title starts beside the button at the left on every screen,
 *  as the desktop's starts 24 px in on every page: a centred title moved from screen to screen with
 *  the buttons around it. `ph-top` names a phone screen's bar for the screens and checks that look
 *  for it. */
export function TopBar({ title, sub, over, back, backIcon, backLabel, bare, pip, onTitle, titleLabel, actions, className = "" }: TopBarProps) {
  const words = (
    <>
      {over && <span className="pagehead-over">{over}</span>}
      <span className="pagehead-t">{title}</span>
      {sub && <span className="sub">{sub}</span>}
    </>
  );
  return (
    <header className={`pagehead ph-top ${className}`}>
      <div className="pagehead-row">
        {back ? <IconButton icon={backIcon ?? "back"} label={backLabel ?? t("shell.back")} onClick={back} className="ph-ib" /> : !bare && <MenuButton pip={pip} />}
        <div className="pagehead-title">
          {onTitle ? <button type="button" className="pagehead-tb" onClick={onTitle} aria-label={titleLabel}>{words}</button> : words}
        </div>
        {actions && <div className="pagehead-actions">{actions}</div>}
      </div>
    </header>
  );
}

// ── command palette ──────────────────────────────────────────────────────────────────────

/** The groups the palette draws, in order. */
export type PaletteGroup = "chats" | "projects" | "actions" | "go" | "settings";

/** One thing the palette can do. `right` is the short word at the row's far end (a state, a key, "Section")
 *  and `tone` colours it; `beside` is what Ctrl Enter does where opening in a second pane means something. */
export type PaletteItem = { id: string; group: PaletteGroup; label: string; hint?: string; icon: IconName; iconPath?: string | null; right?: string; tone?: "live" | "attention"; run: () => void; beside?: () => void };

/** The palette's search, over names and hints. A name that holds the words comes before one that only
 *  has them in its hint, so "menu" finds the chat called Menu before the chat that mentions it. */
export function matchPalette(items: PaletteItem[], query: string, limit = 12): PaletteItem[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return items.slice(0, limit);
  const named: PaletteItem[] = [];
  const hinted: PaletteItem[] = [];
  for (const it of items) {
    if (it.label.toLowerCase().includes(needle)) named.push(it);
    else if (it.hint?.toLowerCase().includes(needle)) hinted.push(it);
  }
  return [...named, ...hinted].slice(0, limit);
}

/** A chat opened in a second pane beside the one on screen; with none open, it is simply opened. */
export function besideSession(id: string, open: () => void): void {
  const route = parse();
  if (route.session && route.session !== id) navigate(sessionPath(route.session, id));
  else open();
}

/** What a chat says at the end of its palette row: working, waiting for you, failed, or how long ago. */
export function chatStatus(s: Pick<SessionSummary, "status" | "last_message_at">): Pick<PaletteItem, "right" | "tone"> {
  if (s.status === "waiting") return { right: t("side.state.waiting"), tone: "attention" };
  if (s.status === "failed") return { right: t("side.state.failed"), tone: "attention" };
  if (s.status === "running" || s.status === "compacting") return { right: t("side.state.running"), tone: "live" };
  return { right: relTime(s.last_message_at) };
}

const GO_KEYS: Record<string, Screen> = { a: "agents", o: "orchestration", v: "companion", i: "inbox", b: "board", t: "terminals", c: "changes", m: "memory", u: "usage", s: "settings" };

/** The two keys that go to a screen ("G C" for Changes), for the palette to say beside it; undefined where none does. */
export function goKey(screen: Screen): string | undefined {
  const letter = Object.keys(GO_KEYS).find((key) => GO_KEYS[key] === screen);
  return letter ? `G ${letter.toUpperCase()}` : undefined;
}

/** Keyboard on a desktop: Ctrl/⌘ K opens the palette, `g` then a letter goes to a screen. Never inside a text field. */
export function useShortcuts(onPalette: () => void, selfdev: SelfDevMode) {
  useEffect(() => {
    let pendingG = 0;
    const onKey = (e: KeyboardEvent) => {
      // A focused terminal keeps Ctrl+K and every letter: they are the shell's.
      if (insideTerminal(e.target)) return;
      const t = e.target as HTMLElement | null;
      const typing = !!t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
      if ((e.metaKey || e.ctrlKey) && latinKey(e) === "k") {
        e.preventDefault();
        onPalette();
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      const letter = latinKey(e);
      if (letter === "g") {
        pendingG = Date.now();
        return;
      }
      if (pendingG && Date.now() - pendingG < 1200 && GO_KEYS[letter] && visibleScreens([GO_KEYS[letter]], selfdev).length) {
        e.preventDefault();
        navigate(pathFor(GO_KEYS[letter]));
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
