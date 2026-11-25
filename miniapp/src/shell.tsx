// The application shell: a page header with the screen's name and its actions, the command palette
// and the keyboard. On a desktop the sessions are the left column (sidebar.tsx) and the destinations
// are a menu over the content (navmenu.tsx); on a phone both are in the drawer (drawer.tsx), which
// the header's hamburger opens.

import { useEffect, useState, type ReactNode } from "react";
import { Icon, IconName } from "./icons";
import { Sheet } from "./ui/dialogs";
import { IconButton } from "./ui/primitives";
import { Screen, navigate, pathFor } from "./router";
import { SelfDevMode, visibleScreens } from "./capabilities";
import { t } from "./i18n";
import { insideTerminal } from "./terminal/keys";
import { latinKey } from "./navigation";
import { MenuButton } from "./ui/phone";

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
 *  itself with TopBar (ui/phone.tsx), which draws this same markup.
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

// ── command palette ──────────────────────────────────────────────────────────────────────

export type PaletteItem = { id: string; label: string; hint?: string; icon: IconName; iconPath?: string | null; run: () => void };

/** The palette's search, shared with the More launcher's field so the two never find different
 *  things for the same words. */
export function matchPalette(items: PaletteItem[], query: string, limit = 12): PaletteItem[] {
  const needle = query.trim().toLowerCase();
  return (needle ? items.filter((it) => `${it.label} ${it.hint ?? ""}`.toLowerCase().includes(needle)) : items).slice(0, limit);
}

/** Ctrl/⌘ K: go somewhere by name — a screen, a session, an action. */
export function Palette({ items, onClose }: { items: PaletteItem[]; onClose: () => void }) {
  const [q, setQ] = useState("");
  const [cursor, setCursor] = useState(0);
  const needle = q.trim().toLowerCase();
  const shown = matchPalette(items, q);
  useEffect(() => setCursor(0), [needle]);
  const run = (it: PaletteItem) => {
    onClose();
    it.run();
  };
  return (
    <Sheet ariaLabel={t("shell.search.label")} onClose={onClose} size="sm" className="palette-sheet">
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
            <Icon name={it.icon} size={16} path={it.iconPath} />
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

const GO_KEYS: Record<string, Screen> = { a: "agents", o: "orchestration", v: "companion", i: "inbox", b: "board", t: "terminals", c: "changes", m: "memory", u: "usage", s: "settings" };

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
