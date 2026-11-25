// The phone's navigation is the desktop's sidebar in a drawer from the left: the same head (the mark,
// the mode menu, search), the same pinned rows in the same order with the same words, the same list of
// projects and chats with the same filters, the same foot (the account and Settings). Only the drawer
// around it is the phone's own — the scrim, the swipe that closes it, the history entry Back takes —
// and its sizes are the phone's touch sizes (ui/parts/sidebar.css). A drawer of its own used to list
// other destinations under other names, so the two sizes of the app taught two different maps.
// Every screen's hamburger opens it (ui/primitives/drawer.tsx, openDrawer).

import { useEffect, useRef, useState, type MouseEvent } from "react";
import type { Project } from "./api";
import { SelfDevMode } from "./capabilities";
import { t } from "./i18n";
import { type Mode } from "./mode";
import { inLayer, navigate, pathFor, useRoute } from "./router";
import { type ResolvedNav, currentId } from "./navconfig";
import { Launcher } from "./navmenu";
import { placeItem } from "./navplace";
import { OrchestrationSidebar } from "./orchestration";
import { Sidebar, SidebarFrame } from "./sidebar";
import { Drawer, closeDrawer, useDrawerOpen } from "./ui/primitives";
import { Sheet } from "./ui/primitives";

/** Where Chats lives: the agents screen with its list open, since /app/agents/<id> is a session. */
export const CHATS_PATH = pathFor("agents", null, { view: "chats" });

/** Initials for a project's avatar: the first letters of its first two words. */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "") + (words[1]?.[0] ?? words[0]?.[1] ?? "")).toUpperCase();
}

const STAFF_COLOURS = ["orange", "green", "blue", "violet", "rose", "teal", "amber", "slate"];
export function colourOf(id: string): string {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `var(--staff-${STAFF_COLOURS[h % STAFF_COLOURS.length]})`;
}

export type AppDrawerProps = {
  mode: Mode;
  /** What the sidebar and its More launcher hold, badges included. */
  nav: ResolvedNav;
  /** What waits for the operator in orchestration mode. */
  waiting: number;
  selfdev: SelfDevMode;
  /** The palette, which the head's search opens outside the Agents list. */
  onPalette: () => void;
  /** The palette on what was typed in the More sheet's field, which finds chats and settings too. */
  onSearchAll: (query: string) => void;
  /** Orchestration mode's list: whether the main chat is the page under the drawer. */
  mainOpen: boolean;
  /** Agents mode's list, as the desktop's column gets it. */
  session: string | null;
  projects: Project[];
  project: string;
  /** Every project, adding one, the archived ones: a sheet over the screen on a phone. */
  onProjects: () => void;
  onClearProject: () => void;
  onOpen: (id: string) => void;
  toast: (t: string) => void;
};

/** Closes the drawer and runs `then` once its history entry is gone. A sheet opened at once (the
 *  projects, the palette) was there before the drawer's Back landed, and that late Back moved the
 *  address under the new sheet, which a busy phone then showed closing again. */
function closeThen(then: () => void): void {
  if (!inLayer()) { closeDrawer(); then(); return; }
  window.addEventListener("popstate", () => window.setTimeout(then, 0), { once: true });
  closeDrawer();
}

export function AppDrawer(p: AppDrawerProps) {
  const open = useDrawerOpen();
  const route = useRoute();
  const [more, setMore] = useState(false);
  const moreButton = useRef<HTMLButtonElement>(null);
  // Every move from inside the drawer closes it. The router has already put the destination in the
  // drawer's own history entry (router.ts, navigate), so Back from there returns to the page under it.
  const at = window.location.pathname + window.location.search;
  useEffect(() => { closeDrawer(); }, [at]);
  // A link or a chat's row for where the reader already is changes no address, so the drawer closes
  // itself once the row has had its click. Not before it: closing first took the drawer's history
  // entry back while the row's own move was still to come, and Back's late arrival undid that move.
  const onClick = (e: MouseEvent) => {
    const link = (e.target as Element).closest?.("a[href], [data-nav='chat']");
    if (link && !(e.metaKey || e.ctrlKey || e.shiftKey)) closeDrawer();
  };
  const here = currentId(route.screen, route.detail);
  const frame = {
    mode: p.mode, screen: route.screen, detail: route.detail, waiting: p.waiting, nav: p.nav,
    menuOpen: more, onMenu: () => setMore(true), menuButton: moreButton, onToggle: closeDrawer, onPalette: () => closeThen(p.onPalette), drawer: true,
  };
  return (
    <SidebarFrame value={frame}>
      <Drawer open={open} onClose={closeDrawer} label={t("nav.menu")}>
        <div className="ph-drawer-nav" onClick={onClick}>
          {p.mode === "orchestration" ? <OrchestrationSidebar onMain={p.mainOpen} /> : (
            <Sidebar session={p.session} projects={p.projects} project={p.project} onProjects={() => closeThen(p.onProjects)}
              onClearProject={p.onClearProject} onOpen={p.onOpen} toast={p.toast} />
          )}
        </div>
      </Drawer>
      {more && (
        // The desktop's More launcher, as a sheet over the drawer: the dot marks what the pinned rows
        // already hold, as it does beside the desktop's column.
        <Sheet title={t("nav.more")} onClose={() => setMore(false)} className="ph-sheet ph-more-sheet launch-sheet">
          <Launcher
            nav={p.nav}
            placed={p.nav.pinned}
            current={route.session ? "" : here}
            selfdev={p.selfdev}
            phone
            onGo={(path) => { setMore(false); navigate(path); }}
            onPlace={(id, on) => placeItem(p.nav, id, on ? "pinned" : "more")}
            onEverywhere={(query) => { setMore(false); closeThen(() => p.onSearchAll(query)); }}
            onClose={() => setMore(false)}
          />
        </Sheet>
      )}
    </SidebarFrame>
  );
}
