// The rail: a narrow column of icons at the left edge of a desktop, always there, beside the sidebar
// or alone when the sidebar is folded. It carries the places the operator goes every day — home, the
// two modes, terminals, the board, the inbox, services — and at its foot the menu with everything
// else, Settings and the account. It is the folded sidebar as well: there is no separate strip.
//
// Home is the one item that changes. With the sidebar folded, it is the way to unfold it: it shows the
// sidebar's icon in an outlined tile with a word under it (Chats, or Projects in orchestration), and
// pointing at it adds "Toggle sidebar" with the shortcut. With the sidebar open it goes home, which is
// the current mode's own.
//
// The folded tile used to be the logo, turning into the sidebar's icon only under a pointer. A tablet
// has no pointer to hover with, and below 1280 px the sidebar starts folded, so a newcomer on a tablet
// saw an empty start page and no sign that the chat list was one tap away.

import type { RefObject } from "react";
import { plural, t } from "./i18n";
import { Icon, type IconName } from "./icons";
import { type Mode, modeHome } from "./mode";
import { DESKTOP_PLACES, menuSections } from "./navigation";
import { type Screen, pathFor } from "./router";
import { type SelfDevMode, visibleScreens } from "./capabilities";
import { type Counts, ICONS, countFor, go, screenTitle } from "./shell";
import { openUpdate, releaseVersion, useUpdates } from "./updates";

/** A Mac says ⌘ where everything else says Ctrl; the shortcut is the same key either way. */
const MAC = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
export const KEYS = { sidebar: MAC ? "⌘ \\" : "Ctrl \\", menu: MAC ? "⌘ ⇧ M" : "Ctrl ⇧ M" };

/** The account is Settings' security section: the passkeys that sign this browser in, and signing
 *  every browser out. There is one operator, so there is no profile beyond it. */
const ACCOUNT = "security";

/** The destinations under the two modes, in the operator's order. */
const PLACES = DESKTOP_PLACES;

export type RailProps = {
  onSearch: () => void;
  screen: Screen;
  /** The route's detail: Settings' security section is the account. */
  detail: string | null;
  mode: Mode;
  /** The sidebar is folded: Home is then the way to unfold it. */
  collapsed: boolean;
  onToggle: () => void;
  counts: Counts;
  /** What waits for the operator in orchestration mode: a quiet count on its icon, from Agents. */
  waiting: number;
  selfdev: SelfDevMode;
  menuOpen: boolean;
  onMenu: () => void;
  menuButton: RefObject<HTMLButtonElement | null>;
};

/** The label under the pointer: a tooltip of our own, since the browser's waits a second and cannot show a key. */
function Tip({ text, keys }: { text: string; keys?: string }) {
  return (
    <span className="rail-tip" role="tooltip">
      {text}
      {keys && <kbd>{keys}</kbd>}
    </span>
  );
}

function Item({ href, icon, label, on, mode = false, badge, quiet, data }: { href: string; icon: IconName; label: string; on: boolean; mode?: boolean; badge?: number; quiet?: number; data: string }) {
  const counted = badge ? `${label} · ${badge}` : quiet ? `${label} · ${plural("mode.waiting", quiet)}` : label;
  return (
    <a className={`rail-item ${on ? "on" : ""} ${mode ? "mode" : ""}`} href={href} onClick={(e) => go(e, href)} aria-label={counted} aria-current={on ? "page" : undefined} data-rail={data}>
      <Icon name={icon} size={18} />
      <span className="rail-label">{label}</span>
      {!!badge && <span className="rail-badge" data-count={badge} aria-hidden>{badge > 99 ? "99+" : badge}</span>}
      {!badge && !!quiet && <span className="rail-badge quiet" data-waiting={quiet} aria-hidden>{quiet > 99 ? "99+" : quiet}</span>}
      <Tip text={counted} />
    </a>
  );
}

/** A new desktop release, offered where the operator's eye already goes for Settings: blue, so it reads
 *  as an offer rather than a warning, and absent the rest of the time so the rail does not grow a
 *  permanent slot for something that happens once a month. */
function UpdateItem() {
  const { state } = useUpdates();
  if (!state?.connected || !state.upgrade) return null;
  const label = t("update.rail", { version: releaseVersion(state.upgrade.to) });
  return (
    <button className="rail-item rail-update" onClick={openUpdate} aria-label={label} data-rail="update">
      <Icon name="up" size={18} />
      <Tip text={label} />
    </button>
  );
}

export function Rail(p: RailProps) {
  const home = modeHome(p.mode);
  // A destination the menu alone reaches lights the menu's button, so Changes or Usage answers
  // "where am I" on the rail the way Services does; without it nothing on the rail was lit.
  const inMenu = !PLACES.includes(p.screen) && p.screen !== "agents" && menuSections(p.selfdev).some((g) => g.items.includes(p.screen));
  const modeItem = (m: Mode) => (
    <Item
      key={m}
      data={m}
      href={modeHome(m)}
      icon={ICONS[m]}
      label={t(`mode.${m}`)}
      // The mode item is lit on the mode's own screens; on a screen both modes share (Terminals,
      // Settings…) the marker at its edge still says which mode's column is open.
      on={p.screen === m}
      mode={p.mode === m}
      quiet={m === "orchestration" && p.mode !== "orchestration" ? p.waiting : 0}
    />
  );
  return (
    <nav className={`rail ${p.collapsed ? "collapsed" : "expanded"}`} aria-label={t("rail.label")}>
      {p.collapsed ? (
        <button className="rail-item rail-home folded" onClick={p.onToggle} aria-label={t("rail.toggle")} aria-expanded={false} data-rail="home">
          <span className="rail-unfold"><Icon name="columns" size={18} /></span>
          <span className="rail-unfold-word" aria-hidden>{t(p.mode === "orchestration" ? "rail.unfold.projects" : "rail.unfold.chats")}</span>
          <Tip text={t("rail.toggle")} keys={KEYS.sidebar} />
        </button>
      ) : (
        <a className="rail-item rail-home" href={home} onClick={(e) => go(e, home)} aria-label={t("rail.home")} data-rail="home">
          <img src="/app/icons/icon-192.png" alt="" width={24} height={24} />
          <span className="rail-label rail-brand">Daedalus</span>
          <Tip text={t("rail.home")} />
        </a>
      )}
      <button className="rail-item rail-search" onClick={p.onSearch} aria-label={t("shell.search.label")}><Icon name="search" size={18} /><Tip text={t("shell.search.label")} /></button>
      <div className="rail-modes">{modeItem("agents")}{modeItem("orchestration")}</div>
      <div className="rail-rule" />
      <div className="rail-places">{visibleScreens(PLACES, p.selfdev).map((s) => (
        <Item key={s} data={s} href={pathFor(s)} icon={ICONS[s]} label={screenTitle(s)} on={p.screen === s} badge={countFor(s, p.counts)} />
      ))}</div>
      <div className="rail-foot">
        <UpdateItem />
        <button ref={p.menuButton} className={`rail-item ${p.menuOpen || inMenu ? "on" : ""}`} onClick={p.onMenu} aria-label={t("nav.menu")} aria-haspopup="menu" aria-expanded={p.menuOpen} data-rail="menu">
          <Icon name="more" size={18} /><span className="rail-label">{t("nav.more")}</span>
          {(p.counts.changes ?? 0) > 0 && <span className="rail-dot" aria-hidden />}
          <Tip text={t("nav.menu")} keys={KEYS.menu} />
        </button>
        <Item data="settings" href={pathFor("settings")} icon="settings" label={screenTitle("settings")} on={p.screen === "settings" && p.detail !== ACCOUNT} />

      </div>
    </nav>
  );
}
