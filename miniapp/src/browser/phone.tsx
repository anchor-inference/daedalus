// The browser on a phone: a button in the conversation's header with a live thumbnail, the panel's
// sheet for watching (BrowserPanel in its phone form), and the whole screen for driving.
//
// There is no floating card on a phone: the conversation is the whole screen, and a moving picture
// over the text reads badly. The first time a browser starts for a session, a line at the bottom says
// so and offers to open it.
//
// Driving takes the whole screen, landscape-friendly, because the page is 1280 px wide and a thumb
// needs every pixel of it: a tap is a click, a drag scrolls, a long press is a right click, two
// fingers zoom the picture. Typing goes two ways, as in the terminal: straight onto the page through
// the phone's keyboard (filtered for Gboard's doubled words, terminal/dedupe.ts), or composed on a line
// with autocorrect and dictation and sent whole. A phone has no Ctrl+V or Ctrl+C, so the row of keys
// carries Paste and Copy (clipboard.ts).

import { FormEvent, useEffect, useRef, useState } from "react";
import type { BrowserGroup } from "../api";
import { dismissToast, Popover, toast as statusToast } from "../ui/dialogs";
import { t } from "../i18n";
import { Icon } from "../icons";
import { useLiveSnapshot, useLiveView } from "./data";
import type { LiveSnapshot, LiveView } from "./live";
import { domainOf, driveState, needsOf, needWords, pipGroup } from "./model";
import { tapKey } from "./keys";
import { copyFromPage, pasteFromClipboard } from "./clipboard";
import { BrowserViewer, focusViewer } from "./viewer";
import { Favicon } from "./favicon";

/** The keys a phone's keyboard does not have, or hides. */
const KEYS: { key: string; cap: string; label: string }[] = [
  { key: "Escape", cap: "Esc", label: "browser.key.esc" },
  { key: "Tab", cap: "⇥", label: "browser.key.tab" },
  { key: "Backspace", cap: "⌫", label: "browser.key.backspace" },
  { key: "Enter", cap: "⏎", label: "browser.key.enter" },
  { key: "ArrowLeft", cap: "←", label: "browser.key.left" },
  { key: "ArrowUp", cap: "↑", label: "browser.key.up" },
  { key: "ArrowDown", cap: "↓", label: "browser.key.down" },
  { key: "ArrowRight", cap: "→", label: "browser.key.right" },
];

export function PhoneDrive({ group, live, snap, agent, url, saving, onGiveBack }: { group: BrowserGroup; live: LiveView; snap: LiveSnapshot; agent: string; url: string; saving: boolean; onGiveBack: (note: string) => void }) {
  const root = useRef<HTMLDivElement>(null);
  const [giving, setGiving] = useState<HTMLElement | null>(null);
  const [note, setNote] = useState("");
  const tab = group.tabs.find((x) => x.active) ?? group.tabs[0];
  // How the fingers work, said once as driving starts and then out of the way.
  const [hint, setHint] = useState(true);
  useEffect(() => {
    const timer = window.setTimeout(() => setHint(false), 3500);
    return () => window.clearTimeout(timer);
  }, []);
  // The system's back gesture and Telegram's back button close sheets on this app; driving is left
  // only through Give back, so a swipe cannot hand the page back half-typed.
  return (
    <div ref={root} className="bp-drive" role="dialog" aria-modal="true" aria-label={t("browser.drive.label")}>
      <div className="bp-drive-head">
        <Favicon url={tab?.favicon_url ?? ""} page={url} size={16} />
        <span className="bp-drive-domain truncate">{domainOf(url) || t("browser.blank")}</span>
        <span className="bp-drive-you"><i />{t("browser.drive.you")}</span>
        <button type="button" className="btn small primary bp-drive-give" onClick={(e) => setGiving(e.currentTarget)}>
          <Icon name="undo" size={14} />
          {t("browser.give")}
        </button>
      </div>
      <BrowserViewer live={live} snap={snap} tier="live" interactive touch agent={agent} saving={saving} className="bp-drive-viewer">
        {hint && <span className="bp-drive-hint" role="status">{t("browser.drive.hint")}</span>}
      </BrowserViewer>
      <div className="bp-drive-keys" role="toolbar" aria-label={t("browser.keys")}>
        <button
          type="button"
          className="term-key bp-drive-kbd"
          aria-label={t("browser.keyboard.show")}
          onPointerDown={(e) => e.preventDefault()}
          onClick={() => focusViewer(root.current)}
        >
          <Icon name="pen" size={14} />
        </button>
        <button
          type="button"
          className="term-key bp-drive-clip"
          data-clip="paste"
          aria-label={t("browser.paste")}
          title={t("browser.paste")}
          onPointerDown={(e) => e.preventDefault()}
          onClick={() => void pasteFromClipboard(live, true)}
        >
          <Icon name="paste" size={14} />
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
          <Icon name="copy" size={14} />
        </button>
        {KEYS.map((k) => (
          <button
            key={k.key}
            type="button"
            className="term-key"
            data-key={k.key}
            aria-label={t(k.label)}
            // The finger must not take the focus from the page's field, or the keyboard closes.
            onPointerDown={(e) => e.preventDefault()}
            onClick={() => {
              for (const m of tapKey(k.key)) live.input(m);
            }}
          >
            {k.cap}
          </button>
        ))}
      </div>
      <ComposeLine live={live} />
      {giving && (
        <Popover anchor={giving} onClose={() => setGiving(null)} align="right" className="bp-give" label={t("browser.give")}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setGiving(null);
              onGiveBack(note);
            }}
          >
            <label className="bp-give-label" htmlFor="bp-drive-note">{t("browser.give.note")}</label>
            <textarea id="bp-drive-note" className="bp-give-note" rows={3} value={note} maxLength={2000} placeholder={t("browser.give.placeholder")} onChange={(e) => setNote(e.target.value)} />
            <div className="bp-give-foot">
              <span className="sub">{t("browser.give.why")}</span>
              <button type="submit" className="btn small primary">{t("browser.give")}</button>
            </div>
          </form>
        </Popover>
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

/**
 * The header's button on a phone: a live thumbnail of the page and the state's dot, amber with "!"
 * when the agent asks for the operator. The thumbnail streams only while the button is on screen and
 * not while saving data; a second of a 28 px picture is not worth a train's bandwidth.
 */
export function BrowserHeadButton({ groups, onOpen, streaming, saving }: { groups: BrowserGroup[]; onOpen: () => void; streaming: boolean; saving: boolean }) {
  const group = pipGroup(groups);
  const live = useLiveView(group && streaming && !saving ? group.id : null, "thumb", { readOnly: true, box: () => ({ max_w: 128, max_h: 80 }) });
  const snap = useLiveSnapshot(live);
  if (!group) return null;
  const needs = needsOf(group.needs_you, snap);
  const drive = driveState({ ...group, needs_you: needs }, snap.control);
  const label = drive === "needs" ? t("browser.head.needs", { what: needWords(needs) }) : t("browser.head.open");
  return (
    <button type="button" className={`iconbtn browser-headbtn ${drive}`} onClick={onOpen} aria-label={label} title={label} data-drive={drive}>
      <span className="browser-headbtn-thumb">
        {live ? <BrowserViewer live={live} snap={snap} tier="thumb" interactive={false} compact agent="" /> : <Icon name="globe" size={14} />}
      </span>
      <span className={`browser-dot ${drive}`} aria-hidden="true">{drive === "needs" ? "!" : ""}</span>
    </button>
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
