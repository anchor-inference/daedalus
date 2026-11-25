// Settings → Navigation: where each destination sits. Three lists — pinned at the top of the sidebar,
// the More launcher, hidden — that an item is dragged between or sent to with its selector. A phone's
// drawer is the sidebar, so these lists are the phone's too. Every change is saved at once through
// the same PUT the other sections use, and the sidebar redraws from the answer.
//
// Dragging is the quick way and the arrows are the way that always works: a keyboard, a screen reader
// and a phone's browser (which has no HTML drag and drop) move an item one place at a time.

import { useEffect, useState, type DragEvent } from "react";
import type { Settings } from "../api";
import type { SelfDevMode } from "../capabilities";
import { t } from "../i18n";
import { ICON_NAMES, Icon, type IconName } from "../icons";
import { type NavConfig, type NavItem, type PageInfo, desktopLayout, moveItem, resolveNav } from "../navconfig";
import { prime, useQuery } from "../store";
import { Sheet } from "../ui/dialogs";
import { Button, Select } from "../ui/primitives";
import { SetBlock, SetGroup } from "../settingsrow";
import { NAV_KEY, PAGES_KEY } from "../usenav";

type Place = "pinned" | "more" | "hidden";
const PLACES: Place[] = ["pinned", "more", "hidden"];
const EMPTY: Required<NavConfig> = { pinned: [], more: [], hidden: [], icons: {} };

export function NavigationSettings({ s, save, selfdev }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void>; selfdev: SelfDevMode }) {
  const pages = useQuery<{ pages?: PageInfo[] }>(PAGES_KEY, { staleMs: 30000 });
  // What was just asked for, shown until the answer replaces it, so a dragged row does not jump back
  // for the length of a round trip.
  const [draft, setDraft] = useState<NavConfig | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [picking, setPicking] = useState<NavItem | null>(null);
  const saved = s.navigation ?? EMPTY;
  const config: NavConfig = draft ?? saved;
  // The shell reads the section from its own light route; the fresh copy goes there too, so the
  // sidebar changes the moment Settings does rather than at its next poll.
  useEffect(() => {
    if (s.navigation) prime(NAV_KEY, s.navigation);
  }, [s.navigation]);

  const nav = resolveNav({ config, pages: pages.data?.pages, selfdev });
  const known = new Set(nav.catalogue.map((it) => it.id));
  const sections: Record<Place, NavItem[]> = {
    pinned: nav.pinned,
    more: nav.more.flatMap((g) => g.items),
    hidden: nav.catalogue.filter((it) => nav.hidden.has(it.id)),
  };
  const placeOf = (id: string): Place => PLACES.find((p) => sections[p].some((it) => it.id === id)) ?? "more";

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

  function setIcon(item: NavItem, name: IconName | null) {
    const icons = { ...(config.icons ?? {}) };
    if (name) icons[item.id] = name;
    else delete icons[item.id];
    setPicking(null);
    void write({ ...config, icons });
  }

  const drop = (e: DragEvent, run: (id: string) => void) => {
    if (!dragging) return;
    e.preventDefault();
    e.stopPropagation();
    const id = dragging;
    setDragging(null);
    run(id);
  };
  const allowDrop = (e: DragEvent) => {
    if (dragging) e.preventDefault();
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

  const row = (it: NavItem, p: Place) => (
    <div
      key={it.id}
      className={`nav-cfg-row ${dragging === it.id ? "dragging" : ""}`}
      data-nav-row={it.id}
      data-place={p}
      draggable
      onDragStart={(e) => { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", it.id); setDragging(it.id); }}
      onDragEnd={() => setDragging(null)}
      onDragOver={allowDrop}
      onDrop={(e) => drop(e, (id) => id !== it.id && place(id, p, it.id))}
    >
      <span className="nav-cfg-grip" aria-hidden><Icon name="vdots" size={16} /></span>
      {glyph(it)}
      <span className="nav-cfg-title truncate">{it.title}</span>
      {arrows(it, () => step(it.id, -1), () => step(it.id, 1))}
      <Select className="nav-cfg-place set-value" value={p} label={t("nav.cfg.place", { name: it.title })} onChange={(q) => place(it.id, q)} options={PLACES.map((q) => ({ id: q, label: t(`nav.cfg.place.${q}`) }))} />
    </div>
  );

  return (
    <>
      <div className="nav-cfg" data-setting="navigation.pinned">
        <p className="set-help">{t("nav.cfg.desktop.sub")}</p>
        {PLACES.map((p) => (
          <div key={p} className="nav-cfg-list" data-nav-list={p} onDragOver={allowDrop} onDrop={(e) => drop(e, (id) => place(id, p))}>
            <SetGroup title={t(`nav.cfg.list.${p}`)}>
              {sections[p].map((it) => row(it, p))}
              {sections[p].length === 0 && <SetBlock className="nav-cfg-empty"><span className="set-hint">{t("nav.cfg.empty")}</span></SetBlock>}
            </SetGroup>
          </div>
        ))}
      </div>
      <div className="btnrow set-actions">
        <Button data-nav="reset" data-setting="navigation.reset" onClick={() => void write({ ...EMPTY })}>{t("nav.cfg.reset")}</Button>
      </div>
      {picking && (
        <Sheet title={t("nav.cfg.icon", { name: picking.title })} onClose={() => setPicking(null)} size="sm">
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
