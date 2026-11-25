// The left column on a desktop, the whole of the frame's navigation: there is no strip of icons beside
// it. Every mode draws the same frame around its own list — the head (the mark, the mode menu, search
// and the fold), the pinned block (New chat, the operator's daily destinations, More) and the foot (the
// account and Settings) — and only the list between them changes: the projects and chats in Agents
// mode (screens/Sessions.tsx), the orchestrated projects in orchestration mode (orchestration.tsx), a
// project's own column in its focus mode (project/ProjectSidebar.tsx).
//
// The rows are labelled. An icon rail told its destinations apart by glyphs alone, and its names were in
// tooltips a pointer had to wait for; a tablet had no pointer to wait with. Folded, the column is gone
// entirely and one button at the top-left corner of the window brings it back.

import { Suspense, createContext, lazy, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import { retried } from "./chunks";
import { type Project } from "./api";
import { Icon } from "./icons";
import { plural, t } from "./i18n";
import { type Mode, modeHome } from "./mode";
import { type NavItem, type ResolvedNav, currentId } from "./navconfig";
import { placeItem } from "./navplace";
import { type Screen, navigate, newAgentPath, pathFor, useRoute } from "./router";
import { go } from "./shell";
import { openUpdate, releaseVersion, useUpdates } from "./updates";
import { type MenuItem, Menu, OverflowMenu, Tooltip, useContextActions } from "./ui/primitives";
import { openImport } from "./imports/ImportExplorer";

const SessionsScreen = lazy(retried(() => import("./screens/Sessions"), (m) => ({ default: m.SessionsScreen })));

/** A Mac says ⌘ where everything else says Ctrl; the shortcut is the same key either way. */
const MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
export const KEYS = { sidebar: MAC ? "⌘ \\" : "Ctrl \\", menu: MAC ? "⌘ ⇧ M" : "Ctrl ⇧ M", search: MAC ? "⌘ K" : "Ctrl K", settings: MAC ? "⌘ ," : "Ctrl ," , newChat: MAC ? "⌘ ⇧ O" : "Ctrl ⇧ O", escape: "Esc", enter: "Enter", beside: MAC ? "⌘ Enter" : "Ctrl Enter" };

/** The account is Settings' security section: two-factor sign-in, the operator's secrets, and signing
 *  every browser out. There is one operator, so there is no profile beyond it. */
const ACCOUNT = "security";

/** What the frame around every mode's list needs from the shell. App.tsx provides it once; each mode's
 *  column draws the head, the pinned block and the foot from it, so switching modes changes the list
 *  and nothing around it. */
export type FrameProps = {
  mode: Mode;
  screen: Screen;
  /** The route's detail: Settings' security section is the account. */
  detail: string | null;
  /** What waits for the operator in orchestration mode, said beside it in the mode menu. */
  waiting: number;
  /** What sits where: navconfig.ts's answer for this installation. */
  nav: ResolvedNav;
  menuOpen: boolean;
  onMenu: () => void;
  menuButton: RefObject<HTMLButtonElement | null>;
  onToggle: () => void;
  onPalette: () => void;
};

const Frame = createContext<FrameProps | null>(null);
export const SidebarFrame = Frame.Provider;

function useFrame(): FrameProps {
  const frame = useContext(Frame);
  if (!frame) throw new Error("the sidebar is drawn outside its frame");
  return frame;
}

/** The fold, at the end of the head. Folded, the button in the window's corner (App.tsx) is the way back. */
export function FoldButton({ onToggle }: { onToggle: () => void }) {
  return (
    <Tooltip label={t("shell.sidebar.collapse")} kbd={KEYS.sidebar}>
      <button type="button" className="iconbtn sidebar-fold" onClick={onToggle} aria-label={t("shell.sidebar.collapse")} aria-expanded>
        <Icon name="sidebar" size={16} />
      </button>
    </Tooltip>
  );
}

/** The column's first row: the mark (home of the mode), the mode menu, search and the fold. `onSearch`
 *  is the column's own search where it has one (the Agents list); elsewhere the button opens the
 *  palette, which finds a screen, a project or a chat by name. */
export function SidebarHead({ onSearch, searching = false }: { onSearch?: () => void; searching?: boolean }) {
  const f = useFrame();
  const home = modeHome(f.mode);
  const modes: MenuItem[] = (["agents", "orchestration"] as Mode[]).map((m) => ({
    label: t(`mode.${m}`),
    icon: m === "agents" ? "bots" : "compass",
    // The current mode carries the check; the other says how much waits in it. A checkbox item would
    // keep the menu open, and choosing a mode is a move, not a setting.
    aside: m === f.mode ? <Icon name="check" size={16} /> : m === "orchestration" && f.waiting > 0 ? <span className="sb-mode-wait num" aria-label={plural("mode.waiting", f.waiting)}>{f.waiting}</span> : undefined,
    onSelect: () => navigate(modeHome(m)),
  }));
  const search = onSearch ?? f.onPalette;
  return (
    <div className="sidebar-head">
      <a className="sb-mark" href={home} onClick={(e) => go(e, home)} aria-label={t("side.home")} title="Daedalus">
        <Icon name="logo" size={14} />
      </a>
      <Menu label={t("side.mode.label")} side="bottom" align="start" items={modes} trigger={
        <button type="button" className="sb-mode" data-mode={f.mode} aria-label={t("side.mode.current", { mode: t(`mode.${f.mode}`) })}>
          <span className="truncate">{t(`mode.${f.mode}`)}</span>
          {f.mode === "agents" && f.waiting > 0 && <span className="sb-mode-pip" aria-hidden />}
          <Icon name="chevron" size={14} />
        </button>
      } />
      <Tooltip label={t(onSearch ? "side.search.title" : "side.search.palette")} kbd={onSearch ? "/" : KEYS.search}>
        <button type="button" className={`iconbtn sb-search-btn ${searching ? "on" : ""}`} onClick={search} aria-label={t(onSearch ? "side.search.title" : "side.search.palette")} aria-pressed={onSearch ? searching : undefined}>
          <Icon name="search" size={16} />
        </button>
      </Tooltip>
      <FoldButton onToggle={f.onToggle} />
    </div>
  );
}

/** One pinned destination: its icon, its name and its count. A right click offers the destination and
 *  where it sits, as the launcher's rows do. */
function PinnedRow({ item, on, nav }: { item: NavItem; on: boolean; nav: ResolvedNav }) {
  const link = useRef<HTMLAnchorElement>(null);
  const items = useMemo<MenuItem[]>(() => [
    { label: t("common.open"), icon: "open", onSelect: () => link.current?.click() },
    { label: t("nav.ctx.newtab"), icon: "external", onSelect: () => void window.open(new URL(item.path, location.href).href, "_blank", "noopener") },
    "-",
    { label: t("nav.ctx.tomore"), icon: "apps", onSelect: () => void placeItem(nav, item.id, "more") },
    { label: t("nav.ctx.hide"), icon: "eyeoff", onSelect: () => void placeItem(nav, item.id, "hidden") },
    "-",
    { label: t("nav.ctx.customise"), icon: "sliders", onSelect: () => navigate(pathFor("settings", "navigation")) },
  ], [item, nav]);
  useContextActions(link, items);
  // The Inbox's count is what waits for the operator: the attention colour. Any other count is a plain number.
  const urgent = item.screen === "inbox";
  return (
    <a ref={link} className={`sb-nav ${on ? "on" : ""}`} href={item.path} onClick={(e) => go(e, item.path)} data-nav={item.id}
      aria-current={on ? "page" : undefined} aria-label={item.badge ? `${item.title} · ${item.badge}` : item.title}>
      <Icon name={item.icon} size={16} path={item.iconPath} />
      <span className="sb-nav-t">{item.title}</span>
      {item.badge > 0 && <span className={`sb-nav-n num ${urgent ? "urgent" : ""}`} data-count={item.badge} aria-hidden>{item.badge > 99 ? "99+" : item.badge}</span>}
    </a>
  );
}

/** New chat, the operator's pinned destinations, and More with everything else. */
export function SidebarPinned() {
  const f = useFrame();
  const here = currentId(f.screen, f.detail);
  const inMore = f.nav.more.some((g) => g.items.some((it) => it.id === here));
  const route = useRoute();
  const newChat = pathFor("agents");
  // Lit on the start canvas only: a chat open under /app/agents is the chat's row's to light.
  const newChatOn = f.screen === "agents" && !route.session && !route.query.get("view") && !f.menuOpen;
  // The ways a chat begins, on the chevron at the end of the row and on its right click.
  const newSplit = useRef<HTMLDivElement>(null);
  const newChatItems = useMemo<MenuItem[]>(() => [
    { label: t("ph.newchat"), icon: "compose", kbd: KEYS.newChat, onSelect: () => navigate(newChat) },
    { label: t("shell.search.newagent"), icon: "user", onSelect: () => navigate(newAgentPath()) },
    { label: t("side.import"), icon: "import", hint: t("imp.entry.hint"), onSelect: () => openImport() },
  ], [newChat]);
  useContextActions(newSplit, newChatItems);
  return (
    <div className="sb-pinned-nav" role="group" aria-label={t("side.places")}>
      <div ref={newSplit} className={`sb-nav sb-newchat ${newChatOn ? "on" : ""}`}>
        <a className="sb-nav-main" href={newChat} onClick={(e) => go(e, newChat)} data-nav="new" title={t("side.newchat.title")}>
          <Icon name="compose" size={16} />
          <span className="sb-nav-t">{t("ph.newchat")}</span>
          <kbd className="sb-nav-kbd">{KEYS.newChat}</kbd>
        </a>
        <OverflowMenu className="sb-new-more" icon="chevron" label={t("side.newchat.more")} items={newChatItems} />
      </div>
      {f.nav.pinned.map((it) => <PinnedRow key={it.id} item={it} on={here === it.id} nav={f.nav} />)}
      <button ref={f.menuButton} type="button" className={`sb-nav sb-more-nav ${f.menuOpen || inMore ? "on" : ""}`} onClick={f.onMenu} data-nav="menu"
        aria-label={t("nav.more")} aria-haspopup="dialog" aria-expanded={f.menuOpen}>
        <Icon name="apps" size={16} />
        <span className="sb-nav-t">{t("nav.more")}</span>
        <kbd className="sb-nav-kbd">{KEYS.menu}</kbd>
      </button>
    </div>
  );
}

/** A new desktop release, offered in the foot where Settings is: absent the rest of the time, so the
 *  foot does not grow a permanent slot for something that happens once a month. */
function UpdateButton() {
  const { state } = useUpdates();
  if (!state?.connected || !state.upgrade) return null;
  const label = t("update.offer", { version: releaseVersion(state.upgrade.to) });
  return (
    <Tooltip label={label}>
      <button type="button" className="iconbtn sb-update" onClick={openUpdate} aria-label={label} data-nav="update">
        <Icon name="up" size={16} />
      </button>
    </Tooltip>
  );
}

/** The foot: the account, which is Settings' security section, and Settings. */
export function SidebarFoot() {
  const f = useFrame();
  const account = pathFor("settings", ACCOUNT);
  const settings = pathFor("settings");
  const accountOn = f.screen === "settings" && f.detail === ACCOUNT;
  return (
    <div className="sidebar-foot">
      <a className={`sb-account ${accountOn ? "on" : ""}`} href={account} onClick={(e) => go(e, account)} data-nav="account" aria-label={t("side.account")} aria-current={accountOn ? "page" : undefined}>
        <span className="sb-avatar" aria-hidden>{t("side.operator").slice(0, 1)}</span>
        <span className="truncate">{t("side.operator")}</span>
      </a>
      <UpdateButton />
      <Tooltip label={t("nav.settings")} kbd={KEYS.settings}>
        <a className={`iconbtn sb-settings ${f.screen === "settings" && !accountOn ? "on" : ""}`} href={settings} onClick={(e) => go(e, settings)} data-nav="settings" aria-label={t("nav.settings")}>
          <Icon name="settings" size={16} />
        </a>
      </Tooltip>
    </div>
  );
}

export type SidebarProps = {
  session: string | null;
  projects: Project[];
  project: string;
  onProjects: () => void;
  /** Lifts the project lens: every project's chats again. */
  onClearProject: () => void;
  onOpen: (id: string) => void;
  toast: (t: string) => void;
};

/** Agents mode's column: the frame around the projects and the chats. */
export function Sidebar(p: SidebarProps) {
  // The search is the column's own: a field over the list, opened from the head or with `/`, and gone
  // again with Escape on an empty field. It searches by meaning where the host can, so it stays here
  // rather than in the palette, which matches names.
  const [searchOpen, setSearchOpen] = useState(false);
  const toggleSearch = useCallback(() => setSearchOpen((open) => !open), []);
  return (
    <nav className="sidebar" aria-label={t("shell.sidebar.label")}>
      <SidebarHead onSearch={toggleSearch} searching={searchOpen} />
      <SidebarPinned />
      <div className="sidebar-body">
        <Suspense fallback={null}>
          <SessionsScreen onOpen={p.onOpen} toast={p.toast} current={p.session ?? undefined} project={p.project} projects={p.projects} onProjects={p.onProjects} onClearProject={p.onClearProject}
            searchOpen={searchOpen} onSearchOpen={setSearchOpen} />
        </Suspense>
      </div>
      <SidebarFoot />
    </nav>
  );
}

/** The sidebar's collapse, with the window's width as the default until the operator chooses. */
export function useSidebar(read: (w: number) => boolean, remember: (c: boolean) => void): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(() => read(window.innerWidth));
  const toggle = useCallback(() => {
    setCollapsed((c) => {
      remember(!c);
      return !c;
    });
  }, [remember]);
  // A choice never made follows the window: a laptop lid at 1100 px gets the conversation alone, docked at 1440 the column.
  useEffect(() => {
    const on = () => setCollapsed(read(window.innerWidth));
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, [read]);
  return [collapsed, toggle];
}

/** Folded, the one way back: a button in the window's top-left corner, over the header's padding. */
export function UnfoldButton({ onToggle }: { onToggle: () => void }): ReactNode {
  return (
    <Tooltip label={t("side.unfold")} kbd={KEYS.sidebar} side="right">
      <button type="button" className="iconbtn sidebar-unfold" onClick={onToggle} aria-label={t("side.unfold")} aria-expanded={false} data-nav="unfold">
        <Icon name="sidebar" size={16} />
      </button>
    </Tooltip>
  );
}
