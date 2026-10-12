// Secondary destinations open beside their button. Primary destinations already have their own
// place in the desktop column and are not repeated here.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { LangPicker } from "./ui/components";
import { Overlay, useLayer } from "./ui/dialogs";
import { Icon } from "./icons";
import { t } from "./i18n";
import { moveIndex } from "./navigation";
import type { NavItem, ResolvedNav } from "./navconfig";
import { Screen, pathFor } from "./router";
import { SelfDevMode, screenTag } from "./capabilities";
import { go } from "./shell";
import { usePetPreference } from "./ui/pet";
import { KEYS } from "./rail";

const BETA: Screen[] = ["voice"];

/** `current` is the id of the item the route stands on (navconfig's `currentId`). */
export function NavMenu({ current, nav, selfdev, onClose, opener, onSearch }: { current: string; nav: ResolvedNav; selfdev: SelfDevMode; onClose: () => void; opener?: HTMLElement | null; onSearch?: () => void }) {
  const [pet, setPet] = usePetPreference();
  const panel = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left:8, top:8 });
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
  useLayer(onClose);
  // The first item takes focus on open; when the menu closes focus goes back to the button that
  // opened it, or to wherever it was for a keyboard shortcut. The callback's identity changes on
  // every render of the shell and must not re-run this.
  useEffect(() => {
    const from = opener ?? (document.activeElement as HTMLElement | null);
    panel.current?.querySelector<HTMLElement>("[role='menuitem']")?.focus();
    return () => {
      from?.focus?.();
    };
  }, []);
  const items = () => Array.from(panel.current?.querySelectorAll<HTMLElement>("[role='menuitem']") ?? []);
  const focusable = () => Array.from(panel.current?.querySelectorAll<HTMLElement>("[role='menuitem'], .lang button") ?? []);
  const onKey = (e: React.KeyboardEvent) => {
    const list = items();
    const next = moveIndex(list.indexOf(document.activeElement as HTMLElement), e.key, list.length);
    if (next !== null) {
      list[next]?.focus();
      e.preventDefault();
      return;
    }
    // Tab stays inside: the menu is the only thing that can take the keyboard while it is open.
    if (e.key === "Tab") {
      const all = focusable();
      const i = all.indexOf(document.activeElement as HTMLElement);
      const to = e.shiftKey ? all[(i - 1 + all.length) % all.length] : all[(i + 1) % all.length];
      to?.focus();
      e.preventDefault();
    }
  };
  const item = (it: NavItem) => {
    const n = it.badge;
    const tag = it.screen ? screenTag(it.screen, selfdev, BETA) : "";
    const on = current === it.id;
    return (
      <a key={it.id} role="menuitem" href={it.path} data-nav-item={it.id} className={`navmenu-item ${on ? "active" : ""}`} aria-current={on ? "page" : undefined} onClick={(e) => { go(e, it.path); if (!e.defaultPrevented) return; onClose(); }}>
        <Icon name={it.icon} size={18} path={it.iconPath} />
        <span className="truncate">{it.title}</span>
        {tag && <span className="beta-tag">{t(tag)}</span>}
        {n > 0 && <span className={`count ${it.screen === "services" ? "ok" : it.screen === "changes" ? "attn" : ""}`}>{n}</span>}
      </a>
    );
  };
  const customise = pathFor("settings", "navigation");
  return (
    <Overlay>
      <div className="navmenu-backdrop" onClick={onClose} />
      <div ref={panel} className="navmenu" style={{ ...position, bottom:"auto" }} role="menu" aria-label={t("nav.menu")} onKeyDown={onKey}>
        {/* The search left the rail's top, where it duplicated Ctrl+K; it is kept here, first, with
            its key, so a mouse still reaches it and the key is learnt from it. */}
        {onSearch && (
          <div className="navmenu-section">
            <button role="menuitem" className="navmenu-item navmenu-action" data-nav="search" onClick={() => { onClose(); onSearch(); }}>
              <Icon name="search" size={18} />
              <span className="truncate">{t("shell.search.label")}</span>
              <kbd className="navmenu-kbd">{KEYS.search}</kbd>
            </button>
          </div>
        )}
        {nav.more.map((g) => (
          <div key={g.key} className="navmenu-section" role="group" aria-label={t(`nav.group.${g.key}`)}>
            <div className="navmenu-label">{t(`nav.group.${g.key}`)}</div>
            {g.items.map(item)}
          </div>
        ))}
        <div className="navmenu-section">
          <button role="menuitem" className="navmenu-item navmenu-action" onClick={() => { setPet(!pet); onClose(); }}>
            <Icon name="bots" size={18} />
            <span className="truncate">{pet ? t("pet.off") : t("pet.on")}</span>
          </button>
          <a role="menuitem" href={customise} className="navmenu-item" data-nav="customise" onClick={(e) => { go(e, customise); if (e.defaultPrevented) onClose(); }}>
            <Icon name="sliders" size={18} />
            <span className="truncate">{t("nav.customise")}</span>
          </a>
          <div className="navmenu-lang">
            <span><Icon name="globe" size={18} /> {t("lang.menu")}</span>
            <LangPicker />
          </div>
          <a role="menuitem" href={pathFor("settings", "about")} className="navmenu-item" onClick={(e) => { go(e, pathFor("settings", "about")); if (e.defaultPrevented) onClose(); }}>
            <Icon name="question" size={18} />
            <span className="truncate">{t("settings.sec.about")}</span>
          </a>
        </div>
      </div>
    </Overlay>
  );
}
