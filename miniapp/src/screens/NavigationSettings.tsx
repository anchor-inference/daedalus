// Settings → Navigation: where each destination sits. The desktop half is three lists — the rail, the
// More menu, hidden — that an item is dragged between or sent to with its selector; the phone half is
// the drawer's order and which items are in it at all. Every change is saved at once through the same
// PUT the other sections use, and the rail redraws from the answer.
//
// Dragging is the quick way and the arrows are the way that always works: a keyboard, a screen reader
// and a phone's browser (which has no HTML drag and drop) move an item one place at a time.

import { useEffect, useState, type DragEvent } from "react";
import type { Settings } from "../api";
import type { SelfDevMode } from "../capabilities";
import { t } from "../i18n";
import { ICON_NAMES, Icon, type IconName } from "../icons";
import { type NavConfig, type NavItem, type PageInfo, desktopLayout, drawerLayout, moveItem, resolveNav } from "../navconfig";
import { prime, useQuery } from "../store";
import { Sheet } from "../ui/dialogs";
import { Switch } from "../ui/index";
import { NAV_KEY, PAGES_KEY } from "../usenav";

type Place = "rail" | "more" | "hidden";
const PLACES: Place[] = ["rail", "more", "hidden"];
const EMPTY: Required<NavConfig> = { rail: [], more: [], drawer: [], hidden: [], icons: {} };

export function NavigationSettings({ s, save, selfdev }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void>; selfdev: SelfDevMode }) {
  const pages = useQuery<{ pages?: PageInfo[] }>(PAGES_KEY, { staleMs: 30000 });
  // What was just asked for, shown until the answer replaces it, so a dragged row does not jump back
  // for the length of a round trip.
  const [draft, setDraft] = useState<NavConfig | null>(null);
  const [dragging, setDragging] = useState<{ list: "desktop" | "phone"; id: string } | null>(null);
  const [picking, setPicking] = useState<NavItem | null>(null);
  const saved = s.navigation ?? EMPTY;
  const config: NavConfig = draft ?? saved;
  // The shell reads the section from its own light route; the fresh copy goes there too, so the rail
  // changes the moment Settings does rather than at its next poll.
  useEffect(() => {
    if (s.navigation) prime(NAV_KEY, s.navigation);
  }, [s.navigation]);

  const nav = resolveNav({ config, pages: pages.data?.pages, selfdev });
  const known = new Set(nav.catalogue.map((it) => it.id));
  const sections: Record<Place, NavItem[]> = {
    rail: nav.rail,
    more: nav.more.flatMap((g) => g.items),
    hidden: nav.catalogue.filter((it) => nav.hidden.has(it.id)),
  };
  const placeOf = (id: string): Place => PLACES.find((p) => sections[p].some((it) => it.id === id)) ?? "more";
  const drawerOn = nav.drawer.agents;
  const drawerOff = nav.phoneMore;

  async function write(next: NavConfig) {
    setDraft(next);
    try {
      await save({ navigation: { ...EMPTY, ...next } });
    } finally {
      setDraft(null);
    }
  }

  /** Puts `id` into `place`, before `before` or at the end of that list. */
  function place(id: string, to: Place, before: string | null = null) {
    const order = PLACES.flatMap((p) => sections[p].map((it) => it.id)).filter((x) => x !== id);
    const placement: Record<string, Place> = {};
    for (const p of PLACES) for (const it of sections[p]) placement[it.id] = p;
    placement[id] = to;
    // The order only ranks items inside one list, so the end of the whole order is the end of its list.
    const at = before ? order.indexOf(before) : -1;
    if (at >= 0) order.splice(at, 0, id);
    else order.push(id);
    void write({ ...config, ...desktopLayout(config, order, placement, known) });
  }

  /** One step up or down inside its own list; in More, inside its own group, which is what the menu draws. */
  function step(id: string, by: -1 | 1) {
    const p = placeOf(id);
    const list = sections[p];
    const item = list.find((it) => it.id === id)!;
    const peers = p === "more" ? list.filter((it) => it.group === item.group) : list;
    const i = peers.findIndex((it) => it.id === id);
    const target = peers[i + by];
    if (!target) return;
    const ids = list.map((it) => it.id);
    const next = moveItem(ids, ids.indexOf(id), ids.indexOf(target.id));
    const order = PLACES.flatMap((q) => (q === p ? next : sections[q].map((it) => it.id)));
    const placement: Record<string, Place> = {};
    for (const q of PLACES) for (const it of sections[q]) placement[it.id] = q;
    void write({ ...config, ...desktopLayout(config, order, placement, known) });
  }

  function setDrawer(on: string[]) {
    void write({ ...config, drawer: drawerLayout(config, on, known) });
  }

  function setIcon(item: NavItem, name: IconName | null) {
    const icons = { ...(config.icons ?? {}) };
    if (name) icons[item.id] = name;
    else delete icons[item.id];
    setPicking(null);
    void write({ ...config, icons });
  }

  const drop = (e: DragEvent, list: "desktop" | "phone", run: (id: string) => void) => {
    if (!dragging || dragging.list !== list) return;
    e.preventDefault();
    e.stopPropagation();
    const id = dragging.id;
    setDragging(null);
    run(id);
  };
  const allowDrop = (e: DragEvent, list: "desktop" | "phone") => {
    if (dragging?.list === list) e.preventDefault();
  };

  const glyph = (it: NavItem) => (
    <button type="button" className="iconbtn nav-cfg-icon" onClick={() => setPicking(it)} aria-label={t("nav.cfg.icon", { name: it.title })} title={t("nav.cfg.icon", { name: it.title })}>
      <Icon name={it.icon} size={18} path={it.iconPath} />
    </button>
  );
  const arrows = (it: NavItem, up: () => void, down: () => void) => (
    <span className="nav-cfg-arrows">
      <button type="button" className="iconbtn" onClick={up} aria-label={t("nav.cfg.up", { name: it.title })} title={t("nav.cfg.up", { name: it.title })}><Icon name="up" size={16} /></button>
      <button type="button" className="iconbtn" onClick={down} aria-label={t("nav.cfg.down", { name: it.title })} title={t("nav.cfg.down", { name: it.title })}><Icon name="down" size={16} /></button>
    </span>
  );

  const desktopRow = (it: NavItem, p: Place) => (
    <div
      key={it.id}
      className={`nav-cfg-row ${dragging?.id === it.id ? "dragging" : ""}`}
      data-nav-row={it.id}
      data-place={p}
      draggable
      onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", it.id); setDragging({ list: "desktop", id: it.id }); }}
      onDragEnd={() => setDragging(null)}
      onDragOver={(e) => allowDrop(e, "desktop")}
      onDrop={(e) => drop(e, "desktop", (id) => id !== it.id && place(id, p, it.id))}
    >
      <span className="nav-cfg-grip" aria-hidden><Icon name="vdots" size={16} /></span>
      {glyph(it)}
      <span className="nav-cfg-title truncate">{it.title}</span>
      {arrows(it, () => step(it.id, -1), () => step(it.id, 1))}
      <select className="field nav-cfg-place" value={p} aria-label={t("nav.cfg.place", { name: it.title })} onChange={(e) => place(it.id, e.target.value as Place)}>
        {PLACES.map((q) => <option key={q} value={q}>{t(`nav.cfg.place.${q}`)}</option>)}
      </select>
    </div>
  );

  const phoneRow = (it: NavItem, on: boolean) => (
    <div
      key={it.id}
      className={`nav-cfg-row ${on ? "" : "off"} ${dragging?.id === it.id ? "dragging" : ""}`}
      data-drawer-row={it.id}
      data-on={on ? "1" : "0"}
      draggable
      onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", it.id); setDragging({ list: "phone", id: it.id }); }}
      onDragEnd={() => setDragging(null)}
      onDragOver={(e) => allowDrop(e, "phone")}
      onDrop={(e) => drop(e, "phone", (id) => {
        if (id === it.id) return;
        const ids = drawerOn.map((x) => x.id).filter((x) => x !== id);
        // Dropped on an item outside the drawer: it goes in at the end.
        const at = on ? ids.indexOf(it.id) : ids.length;
        ids.splice(at, 0, id);
        setDrawer(ids);
      })}
    >
      <span className="nav-cfg-grip" aria-hidden><Icon name="vdots" size={16} /></span>
      <span className="nav-cfg-glyph"><Icon name={it.icon} size={18} path={it.iconPath} /></span>
      <span className="nav-cfg-title truncate">{it.title}</span>
      {on && arrows(it, () => {
        const ids = drawerOn.map((x) => x.id);
        const i = ids.indexOf(it.id);
        if (i > 0) setDrawer(moveItem(ids, i, i - 1));
      }, () => {
        const ids = drawerOn.map((x) => x.id);
        const i = ids.indexOf(it.id);
        if (i < ids.length - 1) setDrawer(moveItem(ids, i, i + 1));
      })}
      <Switch checked={on} label={t("nav.cfg.indrawer", { name: it.title })} onChange={(next) => setDrawer(next ? [...drawerOn.map((x) => x.id), it.id] : drawerOn.map((x) => x.id).filter((x) => x !== it.id))} />
    </div>
  );

  return (
    <>
      <div className="card nav-cfg" data-setting="navigation.rail">
        <div className="section-title" style={{ marginTop: 0 }}>{t("nav.cfg.desktop")}</div>
        <div className="sub">{t("nav.cfg.desktop.sub")}</div>
        {PLACES.map((p) => (
          <div key={p} className="nav-cfg-list" data-nav-list={p} onDragOver={(e) => allowDrop(e, "desktop")} onDrop={(e) => drop(e, "desktop", (id) => place(id, p))}>
            <div className="nav-cfg-head">{t(`nav.cfg.list.${p}`)}</div>
            {sections[p].map((it) => desktopRow(it, p))}
            {sections[p].length === 0 && <div className="nav-cfg-empty sub">{t("nav.cfg.empty")}</div>}
          </div>
        ))}
      </div>
      <div className="card nav-cfg" data-setting="navigation.drawer">
        <div className="section-title" style={{ marginTop: 0 }}>{t("nav.cfg.phone")}</div>
        <div className="sub">{t("nav.cfg.phone.sub")}</div>
        <div className="nav-cfg-list" data-drawer-list="on" onDragOver={(e) => allowDrop(e, "phone")} onDrop={(e) => drop(e, "phone", (id) => setDrawer([...drawerOn.map((x) => x.id).filter((x) => x !== id), id]))}>
          <div className="nav-cfg-head">{t("nav.cfg.list.drawer")}</div>
          {drawerOn.map((it) => phoneRow(it, true))}
          {drawerOn.length === 0 && <div className="nav-cfg-empty sub">{t("nav.cfg.empty")}</div>}
        </div>
        <div className="nav-cfg-list" data-drawer-list="off">
          <div className="nav-cfg-head">{t("nav.cfg.list.phonemore")}</div>
          {drawerOff.map((it) => phoneRow(it, false))}
        </div>
      </div>
      <div className="btnrow">
        <button type="button" className="btn" data-nav="reset" data-setting="navigation.reset" onClick={() => void write({ ...EMPTY })}>{t("nav.cfg.reset")}</button>
      </div>
      {picking && (
        <Sheet title={t("nav.cfg.icon", { name: picking.title })} onClose={() => setPicking(null)} size="narrow">
          <div className="nav-cfg-icons" role="group" aria-label={t("nav.cfg.icon", { name: picking.title })}>
            {ICON_NAMES.map((name) => (
              <button key={name} type="button" className={`iconbtn ${picking.icon === name && !picking.iconPath ? "on" : ""}`} data-icon={name} aria-label={name} title={name} aria-pressed={picking.icon === name && !picking.iconPath} onClick={() => setIcon(picking, name)}>
                <Icon name={name} size={20} />
              </button>
            ))}
          </div>
          <div className="btnrow">
            <button type="button" className="btn" data-icon="default" onClick={() => setIcon(picking, null)}>{t("nav.cfg.icon.default")}</button>
          </div>
        </Sheet>
      )}
    </>
  );
}
