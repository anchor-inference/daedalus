// The browser on a phone: a chip in the conversation's top bar (amber "Needs you" when the agent asks
// for the operator) and a row in the conversation that says the same with Open, the panel's sheet for
// watching (BrowserPanel in its phone form, or the list of them when a chat has several), and the
// whole screen for driving.
//
// There is no floating card on a phone: the conversation is the whole screen, and a moving picture
// over the text reads badly. The first time a browser starts for a session, a line at the bottom says
// so and offers to open it.
//
// Driving takes the whole screen, and the page takes the phone's shape while it does (usePhoneFit), so
// a thumb aims at text drawn at its own size rather than at a 1280 px page shrunk into a strip: a tap is a click, a drag scrolls, a long press is a right click, two
// fingers zoom the picture. Typing goes two ways, as in the terminal: straight onto the page through
// the phone's keyboard (filtered for Gboard's doubled words, terminal/dedupe.ts), or composed on a line
// with autocorrect and dictation and sent whole. A phone has no Ctrl+V or Ctrl+C, so the row of keys
// carries Paste and Copy (clipboard.ts).

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import type { BrowserGroup } from "../api";
import { dismissToast, toast as statusToast } from "../ui/dialogs";
import { ActionSheet, BottomSheet } from "../ui/phone";
import { t } from "../i18n";
import { Icon } from "../icons";
import { resizeViewport, useLiveSnapshot, useLiveView } from "./data";
import type { LiveSnapshot, LiveView } from "./live";
import { agentName, domainOf, driveState, needsOf, needWords, pipGroup } from "./model";
import { tapKey } from "./keys";
import { copyFromPage, pasteFromClipboard } from "./clipboard";
import { BrowserViewer, focusViewer } from "./viewer";
import { Favicon } from "./favicon";
import "./phone.css";

/** The keys a phone's keyboard does not have, or hides: one row of seven 44 px keys that fits a
 *  phone's width without scrolling (ten scrolled, and the last ones were never found), then the
 *  arrows in a cluster of their own beside Give back. */
const KEYS: { key: string; cap: string; label: string }[] = [
  { key: "Escape", cap: "Esc", label: "browser.key.esc" },
  { key: "Tab", cap: "⇥", label: "browser.key.tab" },
  { key: "Backspace", cap: "⌫", label: "browser.key.backspace" },
  { key: "Enter", cap: "⏎", label: "browser.key.enter" },
];
const ARROWS: { key: string; cap: string; label: string }[] = [
  { key: "ArrowLeft", cap: "←", label: "browser.key.left" },
  { key: "ArrowUp", cap: "↑", label: "browser.key.up" },
  { key: "ArrowDown", cap: "↓", label: "browser.key.down" },
  { key: "ArrowRight", cap: "→", label: "browser.key.right" },
];

const PAGE_MIN = 320;
const PAGE_MAX = 3840;
const FIT_QUIET_MS = 240;

/**
 * While this phone drives, the page takes the phone's shape, and gets its own size back before the
 * agent is woken. A 1280 px page drawn into a 412 px screen was a strip with 60 % of the screen black
 * above and below it, and text too small to aim a finger at. Watching never does this (the agent reads
 * the page's layout while it works); driving can, because the agent waits and reads nothing until the
 * page is given back. `restore` is awaited by Give back; leaving any other way (another device taking
 * over, the connection dropping) restores it on the way out.
 */
function usePhoneFit(group: BrowserGroup, stage: { current: HTMLDivElement | null }): () => Promise<void> {
  const original = useRef({ w: group.viewport.w, h: group.viewport.h });
  const fitted = useRef(false);
  const restore = useCallback(async () => {
    if (!fitted.current) return;
    fitted.current = false;
    await resizeViewport(group.id, original.current.w, original.current.h).catch(() => undefined);
  }, [group.id]);
  useEffect(() => {
    const el = stage.current;
    if (!el) return;
    let last = "";
    let timer = 0;
    let gone = false;
    const send = () => {
      if (gone) return;
      const w = Math.min(PAGE_MAX, Math.max(PAGE_MIN, Math.round(el.clientWidth)));
      const h = Math.min(PAGE_MAX, Math.max(PAGE_MIN, Math.round(el.clientHeight)));
      if (el.clientWidth < 64 || el.clientHeight < 64) return;
      const key = `${w}x${h}`;
      if (key === last) return;
      last = key;
      fitted.current = true;
      void resizeViewport(group.id, w, h).catch(() => { last = ""; });
    };
    const watched = new ResizeObserver(() => {
      window.clearTimeout(timer);
      timer = window.setTimeout(send, FIT_QUIET_MS);
    });
    watched.observe(el);
    timer = window.setTimeout(send, FIT_QUIET_MS);
    return () => {
      gone = true;
      window.clearTimeout(timer);
      watched.disconnect();
      void restore();
    };
  }, [group.id, stage, restore]);
  return restore;
}

export function PhoneDrive({ group, live, snap, agent, url, saving, onGiveBack }: { group: BrowserGroup; live: LiveView; snap: LiveSnapshot; agent: string; url: string; saving: boolean; onGiveBack: (note: string) => void }) {
  const root = useRef<HTMLDivElement>(null);
  const stage = useRef<HTMLDivElement | null>(null);
  const [giving, setGiving] = useState(false);
  const [menu, setMenu] = useState(false);
  const [note, setNote] = useState("");
  const tab = group.tabs.find((x) => x.active) ?? group.tabs[0];
  const restore = usePhoneFit(group, stage);
  const offline = snap.state.kind === "reconnecting" || snap.state.kind === "proxy-blocked";
  // How the fingers work, said once as driving starts and then out of the way.
  const [hint, setHint] = useState(true);
  useEffect(() => {
    const timer = window.setTimeout(() => setHint(false), 3500);
    return () => window.clearTimeout(timer);
  }, []);
  const give = async () => {
    setGiving(false);
    await restore();
    onGiveBack(note);
  };
  const key = (k: string) => { for (const m of tapKey(k)) live.input(m); };
  // The system's back gesture and Telegram's back button close sheets on this app; driving is left
  // only through Give back, so a swipe cannot hand the page back half-typed.
  return (
    <div ref={root} className={`bp-drive ${offline ? "offline" : ""}`} role="dialog" aria-modal="true" aria-label={t("browser.drive.label")}>
      <div className="bp-drive-head">
        <Favicon url={tab?.favicon_url ?? ""} page={url} size={20} />
        <span className="bp-drive-domain truncate">{domainOf(url) || t("browser.blank")}</span>
        {offline
          ? <span className="bp-drive-you lost"><Icon name="offline" size={14} />{t("browser.ph.offline")}</span>
          : <span className="bp-drive-you"><i />{t("browser.drive.you")}</span>}
        <button type="button" className="bp-drive-more" aria-label={t("browser.menu")} title={t("browser.menu")} aria-haspopup="menu" onClick={() => setMenu(true)}>
          <Icon name="vdots" size={22} />
        </button>
      </div>
      <BrowserViewer live={live} snap={snap} tier="live" interactive touch agent={agent} saving={saving} className="bp-drive-viewer" stageRef={(el) => { stage.current = el; }}>
        {hint && !offline && <span className="bp-drive-hint" role="status">{t("browser.drive.hint")}</span>}
        {offline && (
          <div className="bp-drive-lost" role="status">
            <div className="bp-drive-lost-card">
              <Icon name="offline" size={22} />
              <b>{t("browser.ph.lost.title")}</b>
              <span>{t("browser.ph.lost.sub")}</span>
            </div>
          </div>
        )}
      </BrowserViewer>
      <div className="bp-drive-keys" role="toolbar" aria-label={t("browser.keys")}>
        <div className="bp-drive-row">
          <button type="button" className="term-key bp-drive-kbd" aria-label={t("browser.keyboard.show")} title={t("browser.keyboard.show")} onPointerDown={(e) => e.preventDefault()} onClick={() => focusViewer(root.current)}>
            <Icon name="pen" size={18} />
          </button>
          <button type="button" className="term-key bp-drive-clip" data-clip="paste" aria-label={t("browser.paste")} title={t("browser.paste")} onPointerDown={(e) => e.preventDefault()} onClick={() => void pasteFromClipboard(live, true)}>
            <Icon name="paste" size={18} />
          </button>
          <button
            type="button"
            className="term-key bp-drive-clip"
            data-clip="copy"
            aria-label={t("browser.copy")}
            title={t("browser.copy")}
            onPointerDown={(e) => e.preventDefault()}
            // Called in the tap itself, not after anything awaited: Safari writes the clipboard only then.
            onClick={() => void copyFromPage(live)}
          >
            <Icon name="copy" size={18} />
          </button>
          {KEYS.map((k) => (
            // The finger must not take the focus from the page's field, or the keyboard closes.
            <button key={k.key} type="button" className="term-key" data-key={k.key} aria-label={t(k.label)} title={t(k.label)} onPointerDown={(e) => e.preventDefault()} onClick={() => key(k.key)}>
              {k.cap}
            </button>
          ))}
        </div>
        <div className="bp-drive-row">
          {/* At the bottom left, far from the corner the back arrow lives in, so a thumb reaching for
              one never presses the other. */}
          <button type="button" className="bp-drive-give" onClick={() => setGiving(true)} aria-haspopup="dialog">
            <Icon name="undo" size={18} />
            {t("browser.give")}
          </button>
          <span className="bp-drive-arrows">
            {ARROWS.map((k) => (
              <button key={k.key} type="button" className="term-key" data-key={k.key} aria-label={t(k.label)} title={t(k.label)} onPointerDown={(e) => e.preventDefault()} onClick={() => key(k.key)}>
                {k.cap}
              </button>
            ))}
          </span>
        </div>
      </div>
      <ComposeLine live={live} />
      {giving && (
        <BottomSheet
          title={t("browser.give.note")}
          onClose={() => setGiving(false)}
          className="bp-give ph-givesheet"
          footer={<>
            <button type="button" className="ph-givesheet-keep" onClick={() => setGiving(false)}>{t("browser.ph.keep")}</button>
            <button type="submit" form="bp-drive-give-form" className="ph-givesheet-go"><Icon name="undo" size={18} />{t("browser.give")}</button>
          </>}
        >
          <form id="bp-drive-give-form" className="ph-givesheet-form" onSubmit={(e) => { e.preventDefault(); void give(); }}>
            <textarea id="bp-drive-note" className="bp-give-note" rows={3} value={note} maxLength={2000} placeholder={t("browser.give.placeholder")} aria-label={t("browser.give.note")} onChange={(e) => setNote(e.target.value)} />
            <p className="ph-givesheet-why">{t("browser.give.why")}</p>
          </form>
        </BottomSheet>
      )}
      {menu && (
        <ActionSheet
          onClose={() => setMenu(false)}
          preview={{ title: domainOf(url) || t("browser.blank"), meta: url }}
          items={[
            { label: t("panel.back"), icon: "back", onSelect: () => live.input({ t: "nav", action: "back" }) },
            { label: t("panel.forward"), icon: "forward", onSelect: () => live.input({ t: "nav", action: "forward" }) },
            { label: t("panel.reload"), icon: "reload", onSelect: () => live.input({ t: "nav", action: "reload" }) },
            { label: t("browser.keyboard.show"), icon: "pen", onSelect: () => focusViewer(root.current) },
          ]}
        />
      )}
    </div>
  );
}

/** A line with the phone keyboard's autocorrect and dictation; its text goes to the page whole. */
function ComposeLine({ live }: { live: LiveView }) {
  const [text, setText] = useState("");
  const [enter, setEnter] = useState(false);
  const send = (e?: FormEvent) => {
    e?.preventDefault();
    if (!text && !enter) return;
    if (text) live.input({ t: "text", text });
    if (enter) for (const m of tapKey("Enter")) live.input(m);
    setText("");
  };
  return (
    <form className="bp-compose term-compose" onSubmit={send}>
      <textarea
        className="term-compose-field"
        rows={1}
        value={text}
        placeholder={t("browser.compose")}
        aria-label={t("browser.compose")}
        autoCapitalize="sentences"
        enterKeyHint="send"
        onChange={(e) => setText(e.target.value)}
      />
      <button type="button" className={`iconbtn term-compose-enter ${enter ? "on" : ""}`} aria-pressed={enter} aria-label={t("browser.compose.enter")} title={t("browser.compose.enter")} onPointerDown={(e) => e.preventDefault()} onClick={() => setEnter((on) => !on)}>⏎</button>
      <button type="submit" className="iconbtn primary term-compose-send" aria-label={t("browser.compose.send")} title={t("browser.compose.send")} onPointerDown={(e) => e.preventDefault()}>
        <Icon name="up" />
      </button>
    </form>
  );
}

/** The header's browser button on a phone, wherever a header has one (a chat, a command-line staff
 *  member): the chip below. It was a 28 px live thumbnail with a dot, too small to read what the page
 *  showed and too quiet to say the agent was waiting. */
export function BrowserHeadButton(props: { groups: BrowserGroup[]; onOpen: () => void; streaming: boolean; saving: boolean }) {
  return <BrowserChip {...props} />;
}

/**
 * The top bar's browser chip on a phone: a 32 px pill (44 px to the finger) with the page's icon. It is
 * amber and says "Needs you" while the agent waits for the operator, counts the browsers that do when
 * there are several, and is a quiet icon with a live dot otherwise. Null with no browser open. The
 * classes of the button it replaced stay on it (`browser-headbtn`, `browser-dot`), so whatever finds
 * the header's browser still finds it.
 */
export function BrowserChip({ groups, onOpen, streaming = true, saving = false }: { groups: BrowserGroup[]; onOpen: () => void; streaming?: boolean; saving?: boolean }) {
  const group = pipGroup(groups);
  // A small read-only stream of the busiest browser: its socket says a request has arrived (or been
  // answered) before the listing is read again.
  const live = useLiveView(group && streaming && !saving ? group.id : null, "thumb", { readOnly: true, box: () => ({ max_w: 64, max_h: 40 }) });
  const snap = useLiveSnapshot(live);
  if (!group) return null;
  const open = groups.filter((g) => g.status !== "closed" && g.status !== "lost");
  const needs = needsOf(group.needs_you, snap);
  const drive = driveState({ ...group, needs_you: needs }, snap.control);
  const waiting = Math.max(drive === "needs" ? 1 : 0, open.filter((g) => g.needs_you).length);
  const tab = group.tabs.find((x) => x.active) ?? group.tabs[0];
  const label = drive === "needs" ? t("browser.head.needs", { what: needWords(needs) }) : t("browser.head.open");
  const words = drive === "needs" ? (open.length > 1 ? t(waiting > 1 ? "browser.ph.needs.many" : "browser.ph.needs.one", { n: waiting }) : t("browser.ph.needs")) : open.length > 1 ? String(open.length) : null;
  return (
    <button type="button" className={`browser-headbtn ph-bchip ${drive} ${words ? "" : "bare"}`} onClick={onOpen} aria-label={label} title={label} data-drive={drive}>
      <span className="ph-bchip-ico">
        <Favicon url={tab?.favicon_url ?? ""} page={tab?.url ?? ""} size={20} />
        <span className={`browser-dot ${drive}`} aria-hidden="true" />
      </span>
      {words && <span className="ph-bchip-t">{words}</span>}
    </button>
  );
}

/**
 * The same request in the conversation, under the answer that stopped for it: the header's chip is
 * easy to miss, and this says what the agent wants and opens the browser in one tap. Null while no
 * browser of the chat waits for the operator.
 */
export function BrowserNeedsRow({ groups, onOpen }: { groups: BrowserGroup[]; onOpen: () => void }) {
  const group = groups.find((g) => g.needs_you && g.status !== "closed" && g.status !== "lost");
  if (!group) return null;
  return (
    <div className="ph-bneeds" role="status">
      <Icon name="globe" size={18} />
      <span className="ph-bneeds-main">
        <span className="ph-bneeds-t">{t("browser.ph.row.title")}</span>
        <span className="ph-bneeds-m">{needWords(group.needs_you)}</span>
      </span>
      <button type="button" className="ph-bneeds-open" onClick={onOpen}>{t("browser.ph.open")}</button>
    </div>
  );
}

/**
 * A chat with several browsers opens on their list, grouped by whose they are (this chat first, then
 * each staff member's), each with a live thumbnail; the one that waits for the operator carries Take
 * control in its row. A row opens that browser in the sheet; Take control opens it and takes it.
 */
export function PhoneBrowserList({ groups, onPick, onTake, saving }: { groups: BrowserGroup[]; onPick: (id: string) => void; onTake: (id: string) => void; saving: boolean }) {
  const waiting = groups.filter((g) => g.needs_you).length;
  const owners: { key: string; label: string; items: BrowserGroup[] }[] = [];
  for (const g of groups) {
    const key = g.owner.kind === "session" ? "" : `${g.owner.kind}:${g.owner.id}`;
    let bucket = owners.find((o) => o.key === key);
    if (!bucket) owners.push(bucket = { key, label: key ? g.owner.label || agentName(g) : t("browser.ph.list.chat"), items: [] });
    bucket.items.push(g);
  }
  owners.sort((a, b) => (a.key ? 1 : 0) - (b.key ? 1 : 0));
  return (
    <div className="bp-list">
      <div className="bp-list-head">
        <span className="bp-list-t">{t("browser.ph.list.title")}</span>
        <span className="bp-list-m">{[t("browser.ph.list.live", { n: groups.length }), waiting ? t(waiting > 1 ? "browser.ph.needs.many" : "browser.ph.needs.one", { n: waiting }) : ""].filter(Boolean).join(" · ")}</span>
      </div>
      {owners.map((o) => (
        <section key={o.key || "chat"} className="bp-list-sec">
          <div className="bp-list-sl">{o.label}</div>
          {o.items.map((g) => <ListItem key={g.id} group={g} onPick={() => onPick(g.id)} onTake={() => onTake(g.id)} saving={saving} />)}
        </section>
      ))}
    </div>
  );
}

function ListItem({ group, onPick, onTake, saving }: { group: BrowserGroup; onPick: () => void; onTake: () => void; saving: boolean }) {
  const live = useLiveView(saving ? null : group.id, "thumb", { readOnly: true, box: () => ({ max_w: 160, max_h: 100 }) });
  const snap = useLiveSnapshot(live);
  const tab = group.tabs.find((x) => x.active) ?? group.tabs[0];
  const drive = driveState(group, snap.control);
  const state = t(`browser.ph.row.${drive}`);
  return (
    <div className={`bp-list-row ${drive}`} data-group={group.id}>
      <button type="button" className="bp-list-open" onClick={onPick}>
        <span className="bp-list-thumb">
          {live ? <BrowserViewer live={live} snap={snap} tier="thumb" interactive={false} compact agent="" /> : <Favicon url={tab?.favicon_url ?? ""} page={tab?.url ?? ""} size={20} />}
        </span>
        <span className="bp-list-main">
          <span className="bp-list-rt truncate">{domainOf(tab?.url ?? "") || t("browser.blank")}</span>
          <span className="bp-list-rm truncate"><span className={`bp-list-state ${drive}`}>{state}</span>{group.needs_you ? <> · {needWords(group.needs_you)}</> : null}</span>
        </span>
        {drive !== "needs" && <Icon name="forward" size={18} />}
      </button>
      {drive === "needs" && <button type="button" className="bp-list-take" onClick={onTake}>{t("browser.take")}</button>}
    </div>
  );
}

/**
 * On a phone, the first time this device sees a browser start for a session, a line says so and
 * offers to open it. Remembered per group, so a reload does not say it again.
 */
export function useFirstOpenToast(groups: BrowserGroup[], enabled: boolean, onView: () => void): void {
  const onViewRef = useRef(onView);
  onViewRef.current = onView;
  const shown = useRef(0);
  const fresh = groups.find((g) => g.status === "running" && !announced(g.id));
  const freshId = enabled ? fresh?.id ?? null : null;
  useEffect(() => {
    if (!freshId) return;
    remember(freshId);
    const g = groups.find((x) => x.id === freshId);
    shown.current = statusToast(t("browser.toast.opened", { name: g && g.owner.kind === "staff" ? g.owner.label : t("browser.agent") }), { action: { label: t("browser.toast.view"), run: () => onViewRef.current() }, ms: 7000 });
    // The groups are read for the name only; the toast is about the id appearing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [freshId]);
  // Opened another way (the header's button), the offer to open it has nothing left to offer.
  useEffect(() => {
    if (!enabled && shown.current) {
      dismissToast(shown.current);
      shown.current = 0;
    }
  }, [enabled]);
}

const ANNOUNCED_KEY = "daedalus.browser.announced";

function announcedList(): string[] {
  try {
    const v = JSON.parse(localStorage.getItem(ANNOUNCED_KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((x) => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function announced(id: string): boolean {
  return announcedList().includes(id);
}

function remember(id: string): void {
  try {
    localStorage.setItem(ANNOUNCED_KEY, JSON.stringify([id, ...announcedList().filter((x) => x !== id)].slice(0, 50)));
  } catch {
    /* private mode: announced again next visit */
  }
}
