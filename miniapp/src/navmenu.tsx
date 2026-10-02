// Secondary destinations open beside their button. Primary destinations already have their own
// place in the desktop column and are not repeated here.

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { LangPicker } from "./ui/components";
import { Overlay, useLayer } from "./ui/dialogs";
import { Icon } from "./icons";
import { t } from "./i18n";
import { DESKTOP_PLACES, menuSections, moveIndex } from "./navigation";
import { Screen, pathFor } from "./router";
import { SelfDevMode, screenTag } from "./capabilities";
import { Counts, ICONS, countFor, go, screenTitle } from "./shell";

const BETA: Screen[] = ["voice"];

export function NavMenu({ screen, counts, selfdev, onClose, opener }: { screen: Screen; counts: Counts; selfdev: SelfDevMode; onClose: () => void; opener?: HTMLElement | null }) {
  const panel = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left:8, top:8 });
  useLayoutEffect(() => {
    const place = () => {
      if (!panel.current || !opener) return;
      const column = opener.closest(".desktop-column")?.getBoundingClientRect();
      const anchor = opener.getBoundingClientRect();
      const menu = panel.current.getBoundingClientRect();
      setPosition({ left:Math.min((column?.right ?? anchor.right) + 6, window.innerWidth - menu.width - 8), top:Math.max(8, Math.min(anchor.top, window.innerHeight - menu.height - 8)) });
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
  const item = (s: Screen, kbd?: string) => {
    const n = countFor(s, counts);
    const tag = screenTag(s, selfdev, BETA);
    return (
      <a key={s} role="menuitem" href={pathFor(s)} className={`navmenu-item ${screen === s ? "active" : ""}`} aria-current={screen === s ? "page" : undefined} onClick={(e) => { go(e, pathFor(s)); if (!e.defaultPrevented) return; onClose(); }}>
        <Icon name={ICONS[s]} size={18} />
        <span className="truncate">{screenTitle(s)}</span>
        {tag && <span className="beta-tag">{t(tag)}</span>}
        {n > 0 && <span className={`count ${s === "services" ? "ok" : s === "changes" ? "attn" : ""}`}>{n}</span>}
        {kbd && n === 0 && <kbd>{kbd}</kbd>}
      </a>
    );
  };
  return (
    <Overlay>
      <div className="navmenu-backdrop" onClick={onClose} />
      <div ref={panel} className="navmenu" style={{ ...position, bottom:"auto" }} role="menu" aria-label={t("nav.menu")} onKeyDown={onKey}>
        {menuSections(selfdev).map((g) => ({ ...g, items:g.items.filter((s) => s !== "agents" && !DESKTOP_PLACES.includes(s)) })).filter((g) => g.items.length > 0).map((g) => (
          <div key={g.key} className="navmenu-section" role="group" aria-label={t(`nav.group.${g.key}`)}>
            <div className="navmenu-label">{t(`nav.group.${g.key}`)}</div>
            {g.items.map((s) => item(s))}
          </div>
        ))}
        <div className="navmenu-section">
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
