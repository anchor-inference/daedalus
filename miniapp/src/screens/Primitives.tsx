// /app/primitives: every primitive of ui/primitives on one page, in the state a screen draws it, and
// a button for each layer that opens the real one. Not in the navigation: it is where a screen being
// redrawn is compared against the parts it is drawn from, where the screenshots of the parts are
// taken, and where tests/browser/check_select_stress.py finds a select to open fifty times.

import { useRef, useState } from "react";
import "./primitives.css";
import { Icon } from "../icons";
import { t } from "../i18n";
import { PageHeader } from "../shell";
import { Badge, Button, Combobox, Dot, Field, IconButton, Kbd, MenuRow, OverflowMenu, Popover, Row, Segmented, Select, Sheet, Switch, Tabs, Tooltip, NoticeGlyph, confirmDialog, menuRowBody, raiseNotice, toast, type MenuItem } from "../ui/primitives";

/** Models for the sample picker: names, not words, so they are not translated. */
const MODELS = ["Opus 5.5", "Sonnet 5.5", "Haiku 5.5", "GPT-6 Codex", "Gemini 3 Pro", "DeepSeek Flash", "Qwen 3.8"];

export function PrimitivesScreen() {
  const [view, setView] = useState<"list" | "board" | "calendar">("list");
  const [prices, setPrices] = useState(true);
  const [photos, setPhotos] = useState(false);
  const [language, setLanguage] = useState<"en" | "ru">("ru");
  const [tab, setTab] = useState("changes");
  const [query, setQuery] = useState("");
  const [model, setModel] = useState(MODELS[0]);
  const [popover, setPopover] = useState(false);
  const [sheet, setSheet] = useState(false);
  const popAnchor = useRef<HTMLButtonElement>(null);
  const items: MenuItem[] = [
    { group: t("primitives.sample.page") },
    { label: t("canvas.rename"), icon: "pen", kbd: "F2", onSelect: () => {} },
    { label: t("common.copy"), icon: "copy", onSelect: () => {} },
    { label: t("primitives.sample.autoupdate"), checked: prices, onSelect: () => setPrices((on) => !on) },
    "-",
    { label: t("common.delete"), icon: "trash", danger: true, onSelect: () => {} },
  ];
  const shown = MODELS.filter((name) => name.toLowerCase().includes(query.trim().toLowerCase()));
  const confirm = () => void confirmDialog({ title: t("primitives.sample.confirm"), body: t("primitives.sample.confirm.body"), action: t("common.delete"), danger: true });
  return (
    <div className="screen primitives">
      <PageHeader title={t("primitives.title")} subtitle={t("primitives.note")} />
      <div className="prim-grid">
        <section className="prim-card" aria-label={t("primitives.buttons")}>
          <h2 className="prim-h">{t("primitives.buttons")}</h2>
          <div className="prim-line">
            <Button variant="primary" kbd="Enter">{t("primitives.sample.publish")}</Button>
            <Button>{t("primitives.sample.draft")}</Button>
            <Button variant="quiet">{t("common.cancel")}</Button>
            <Button variant="danger" icon="trash">{t("common.delete")}</Button>
          </div>
          <div className="prim-line">
            <Button size="sm" icon="copy">{t("common.copy")}</Button>
            <Button size="sm" variant="primary" busy>{t("primitives.sample.deploy")}</Button>
            <Button size="sm" disabled>{t("common.save")}</Button>
          </div>
        </section>

        <section className="prim-card" aria-label={t("primitives.iconbuttons")}>
          <h2 className="prim-h">{t("primitives.iconbuttons")}</h2>
          <div className="prim-line">
            <IconButton icon="search" label={t("common.search")} kbd="Ctrl K" />
            <IconButton icon="panel" label={t("primitives.sample.panel")} pressed />
            <IconButton icon="bell" label={t("primitives.sample.inbox")} badge={4} />
            <IconButton icon="settings" label={t("nav.settings")} pip />
            <IconButton icon="close" label={t("common.close")} size={28} />
            <OverflowMenu items={items} label={t("primitives.open.menu")} />
            <Tooltip label={t("primitives.sample.tooltip")} kbd="Ctrl ⇧ O"><span className="prim-tip" tabIndex={0}>{t("primitives.sample.hover")}</span></Tooltip>
          </div>
        </section>

        <section className="prim-card" aria-label={t("primitives.choices")}>
          <h2 className="prim-h">{t("primitives.choices")}</h2>
          <div className="prim-line">
            <Segmented label={t("primitives.sample.view")} value={view} onChange={setView}
              options={[{ id: "list", label: t("primitives.sample.view.list") }, { id: "board", label: t("primitives.sample.view.board"), badge: <Badge tone="attention">2</Badge> }, { id: "calendar", label: t("primitives.sample.view.calendar") }]} />
          </div>
          <div className="prim-kv"><span>{t("primitives.sample.autoupdate")}</span><Switch checked={prices} onChange={setPrices} label={t("primitives.sample.autoupdate")} /></div>
          <div className="prim-kv"><span>{t("primitives.sample.photos")}</span><Switch checked={photos} onChange={setPhotos} label={t("primitives.sample.photos")} /></div>
          <div className="prim-kv"><span>{t("primitives.sample.language")}</span>
            <Select id="primitives-select" label={t("primitives.sample.language")} value={language} onChange={setLanguage}
              options={[{ id: "en", label: t("lang.name.en") }, { id: "ru", label: t("lang.name.ru"), hint: t("primitives.sample.language.hint") }]} />
          </div>
        </section>

        <section className="prim-card" aria-label={t("primitives.tabs")}>
          <h2 className="prim-h">{t("primitives.tabs")}</h2>
          <Tabs variant="panel" label={t("primitives.tabs")} value={tab} onChange={setTab}
            tabs={[{ id: "overview", label: t("primitives.sample.overview"), icon: "grid" }, { id: "changes", label: t("primitives.sample.changes"), icon: "changes", count: 2 }, { id: "details", label: t("primitives.sample.details"), icon: "question", onClose: () => setTab("overview") }]} />
          <div className="prim-kvs">
            <div className="prim-kv"><span className="sub">{t("primitives.sample.status")}</span><span><Dot tone="live" /> {t("primitives.sample.working")}</span></div>
            <div className="prim-kv"><span className="sub">{t("primitives.sample.waits")}</span><span><Dot tone="attention" /> {t("primitives.sample.needsyou")}</span></div>
            <div className="prim-kv"><span className="sub">{t("primitives.sample.shortcut")}</span><span><Kbd>Ctrl</Kbd> <Kbd>,</Kbd></span></div>
          </div>
        </section>

        <section className="prim-card" aria-label={t("primitives.menu")}>
          <h2 className="prim-h">{t("primitives.menu")}</h2>
          {/* The menu as it opens, drawn in place so the page shows it without a press. */}
          <div className="menu prim-still" role="menu" aria-label={t("primitives.menu")}>
            <div className="menu-group">{t("primitives.sample.page")}</div>
            <button type="button" role="menuitem" className="menu-row">{menuRowBody({ icon: "pen", label: t("canvas.rename"), kbd: "F2" })}</button>
            <button type="button" role="menuitem" className="menu-row" data-highlighted="">{menuRowBody({ icon: "copy", label: t("common.copy") })}</button>
            <button type="button" role="menuitemcheckbox" aria-checked={prices} className="menu-row">{menuRowBody({ icon: "reload", label: t("primitives.sample.autoupdate") })}<span className={`menu-check ${prices ? "on" : ""}`} aria-hidden="true" /></button>
            <div className="menu-sep" />
            <button type="button" role="menuitem" className="menu-row danger">{menuRowBody({ icon: "trash", label: t("common.delete") })}</button>
          </div>
        </section>

        <section className="prim-card" aria-label={t("primitives.popover")}>
          <h2 className="prim-h">{t("primitives.popover")}</h2>
          <div className="menu pop prim-still prim-picker">
            <Combobox inline label={t("primitives.sample.search")} placeholder={t("primitives.sample.search")} items={shown} query={query} onQuery={setQuery} value={model} onChoose={setModel}
              render={(name) => <><span className="grow">{name}</span>{name === model && <span className="dropdown-check"><Icon name="check" size={16} /></span>}</>} />
          </div>
        </section>

        <section className="prim-card" aria-label={t("primitives.rows")}>
          <h2 className="prim-h">{t("primitives.rows")}</h2>
          <Row title={t("primitives.sample.chat")} meta={t("primitives.sample.working")} trail={<Badge tone="live">{t("primitives.sample.live")}</Badge>} selected onOpen={() => {}} actions={items} />
          <Row title={t("primitives.sample.chat2")} meta={t("primitives.sample.needsyou")} trail="12:40" onOpen={() => {}} actions={items} />
          <Row one title={t("primitives.sample.chat3")} trail={t("primitives.sample.date")} onOpen={() => {}} />
          <MenuRow icon="folder" label={t("primitives.sample.project")} sub={t("primitives.sample.project.sub")} aside="3" chevron onSelect={() => {}} />
          <MenuRow icon="bolt" label={t("primitives.sample.fast")} toggle={{ checked: photos, onChange: setPhotos }} />
        </section>

        <section className="prim-card" aria-label={t("primitives.fields")}>
          <h2 className="prim-h">{t("primitives.fields")}</h2>
          <Field label={t("primitives.sample.name")} defaultValue={t("primitives.sample.chat")} hint={t("primitives.sample.name.hint")} />
          <Field label={t("primitives.sample.address")} mono defaultValue="deploy@127.0.0.1:/var/www/menu" error={t("primitives.sample.address.error")} />
          <div className="prim-line"><Badge>{t("primitives.sample.draftword")}</Badge><Badge tone="live">{t("primitives.sample.live")}</Badge><Badge tone="attention">{t("primitives.sample.needsyou")}</Badge><Badge tone="accent">{t("primitives.sample.chosen")}</Badge></div>
        </section>

        <section className="prim-card" aria-label={t("primitives.layers")}>
          <h2 className="prim-h">{t("primitives.layers")}</h2>
          <div className="dialog prim-still" role="presentation">
            <h3>{t("primitives.sample.confirm")}</h3>
            <div className="dialog-body">{t("primitives.sample.confirm.body")}</div>
            <div className="dialog-actions"><button type="button" className="btn">{t("common.cancel")}</button><button type="button" className="btn danger solid">{t("common.delete")}</button></div>
          </div>
          <div className="toast prim-still" role="presentation"><span>{t("primitives.sample.toast")}</span><button type="button" className="btn small ghost">{t("common.undo")}</button></div>
          <div className="prim-line">
            <Button size="sm" onClick={() => setSheet(true)}>{t("primitives.open.sheet")}</Button>
            <Button size="sm" onClick={confirm}>{t("primitives.open.confirm")}</Button>
            <Button size="sm" onClick={() => toast(t("primitives.sample.toast"), { undo: () => {} })}>{t("primitives.open.toast")}</Button>
            <Button size="sm" onClick={() => raiseNotice({ key: -Date.now(), seq: Date.now(), ms: 7000, view: { title: t("primitives.sample.notice.title"), body: t("primitives.sample.notice.body"), icon: <NoticeGlyph name="inbox" className="ok" />, onOpen: () => {} } })}>{t("primitives.open.notice")}</Button>
            <button ref={popAnchor} type="button" className="btn small" aria-haspopup="menu" aria-expanded={popover} onClick={() => setPopover((open) => !open)}>{t("primitives.open.popover")}</button>
          </div>
        </section>
      </div>
      {popover && (
        <Popover anchor={popAnchor.current} onClose={() => setPopover(false)} label={t("primitives.popover")} width={336} className="prim-picker">
          <Combobox inline label={t("primitives.sample.search")} placeholder={t("primitives.sample.search")} items={shown} query={query} onQuery={setQuery} value={model} onChoose={(name) => { setModel(name); setPopover(false); }} />
        </Popover>
      )}
      {sheet && (
        <Sheet title={t("primitives.sample.sheet")} size="md" onClose={() => setSheet(false)}
          footer={<><Button variant="quiet" onClick={() => setSheet(false)}>{t("common.cancel")}</Button><Button variant="primary" onClick={() => setSheet(false)}>{t("common.save")}</Button></>}>
          <Field label={t("primitives.sample.name")} defaultValue={t("primitives.sample.chat")} />
          <div className="prim-kv"><span>{t("primitives.sample.autoupdate")}</span><Switch checked={prices} onChange={setPrices} label={t("primitives.sample.autoupdate")} /></div>
        </Sheet>
      )}
    </div>
  );
}
