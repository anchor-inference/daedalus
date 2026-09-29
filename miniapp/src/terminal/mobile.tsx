// A terminal on a phone: the whole screen, a row of the keys a soft keyboard does not have, a line to
// compose text in with the keyboard's own autocorrect and dictation, and touch for what a mouse does
// on a desktop — a pinch for the font size, a long press to select and copy.
//
// It wraps the same `TerminalView` every other place uses, so the instance, its connection and its
// screen are the registry's and survive coming here and going back. What is the phone's own lives
// here and nowhere else:
// - the keys go out through `instance.sendKeys`, as typed input, and the row never takes the focus
//   (pointerdown is cancelled), so the soft keyboard stays up while a key is tapped;
// - what the soft keyboard types passes the doubled-input filter (`dedupe.ts`, Android's Gboard
//   replays a word when its composition ends) and the armed Ctrl and Alt before it leaves;
// - the layer is exactly as tall as the visible area (`--vh`, from visualViewport in App.tsx), so the
//   soft keyboard takes rows and never columns, and the rows go out once the keyboard has settled.

import { FormEvent, ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { TerminalEnvName, TerminalView as TerminalRow } from "../api";
import { MenuItem, OverflowMenu, toast } from "../dialogs";
import { t } from "../i18n";
import { EnvPill } from "../envpill";
import { Icon } from "../icons";
import { useEdgeFade } from "../edgefade";
import { InputDeduper } from "./dedupe";
import { copyText, fontSizeStep, setFontSize, storedFontSize, TerminalState } from "./instance";
import { applyModifiers, composeBytes, keyBytes, PHONE_KEYS, PhoneKey, pinchFont, StickyModifier } from "./phonekeys";
import { instanceFor } from "./terminals";
import { copyLastOutput, CopyOutputButton, TerminalView } from "./view";

/** A change in rows alone waits this long, so the soft keyboard's slide is one RESIZE (fit.ts). */
export const KEYBOARD_SETTLE_MS = 150;
/** How long a finger must rest, without moving, to open the selection layer. */
export const LONG_PRESS_MS = 500;
/** How far a finger may drift and still count as resting. */
const LONG_PRESS_SLOP = 10;
/** An arrow held down repeats, as a hardware key does: after this long, then at the interval below. */
const REPEAT_DELAY_MS = 400;
const REPEAT_EVERY_MS = 70;
const REPEATS = new Set(["up", "down", "left", "right", "pgup", "pgdn"]);

const ENTER_KEY = "daedalus.term.phone.enter";
const COMPOSE_KEY = "daedalus.term.phone.compose";

function readFlag(key: string, fallback: boolean): boolean {
  try {
    const value = localStorage.getItem(key);
    return value === null ? fallback : value === "1";
  } catch {
    return fallback;
  }
}

function writeFlag(key: string, on: boolean): void {
  try {
    localStorage.setItem(key, on ? "1" : "0");
  } catch {
    /* remembered for this page only */
  }
}

export type PhoneTerminalProps = {
  id: string;
  /** The terminal as the host lists it: its environment, folder, status, sandbox. Null until listed. */
  row: TerminalRow | null;
  onBack: () => void;
  /** End the program (the caller asks first when it is busy). */
  onEnd?: () => void;
  onRestart?: () => void;
  onRemove?: () => void;
  workspace?: string;
  fileOpener?: (path: string, line?: number) => void;
  focusToken?: number;
  onState?: (id: string, state: TerminalState) => void;
  /**
   * Where the compose line's text goes instead of the terminal: a staff member's terminal routes it
   * through the team's delivery (a message with a receipt) rather than typing it into the program.
   */
  compose?: { placeholder: string; onSend: (text: string) => Promise<boolean> };
  /** Above the keys: the answers to a request the program is waiting on (a permission, a question). */
  actions?: ReactNode;
  /** More items for the header's menu, after the terminal's own. */
  menu?: MenuItem[];
};

export function PhoneTerminal({ id, row, onBack, onEnd, onRestart, onRemove, workspace, fileOpener, focusToken, onState, compose, actions, menu }: PhoneTerminalProps) {
  const [state, setState] = useState<TerminalState | null>(null);
  const [selecting, setSelecting] = useState<string | null>(null);
  const [composeShown, setComposeShown] = useState(() => readFlag(COMPOSE_KEY, true));
  const stage = useRef<HTMLDivElement>(null);
  const zoom = useRef<HTMLDivElement>(null);
  // The keyboard's word so far, for the doubled-input filter. Anything sent from the row or the
  // compose line ends that word: a replay can only repeat what the keyboard itself composed.
  const deduper = useRef(new InputDeduper());
  const ctrl = useRef(new StickyModifier());
  const alt = useRef(new StickyModifier());
  // The modifiers live in refs (the input hook reads them outside React); this only redraws their keys.
  const [, redraw] = useState(0);
  const touched = useCallback(() => redraw((n) => n + 1), []);

  const changed = useCallback(
    (tid: string, next: TerminalState) => {
      setState(next);
      onState?.(tid, next);
    },
    [onState],
  );

  // The soft keyboard's input: doubled chunks dropped, then the armed modifiers applied to the key
  // they were armed for. The hook goes when the layer goes; the desktop never has one.
  useEffect(() => {
    const instance = instanceFor(id);
    if (!instance) return;
    const off = instance.setInputHook((data) => {
      if (!deduper.current.accept(data, performance.now())) return null;
      if (!ctrl.current.on && !alt.current.on) return data;
      const out = applyModifiers(data, { ctrl: ctrl.current.on, alt: alt.current.on });
      ctrl.current.consume();
      alt.current.consume();
      touched();
      return out;
    });
    return off;
  }, [id, touched]);

  const press = useCallback(
    (key: PhoneKey) => {
      const instance = instanceFor(id);
      if (!instance) return;
      if (key.modifier) {
        (key.modifier === "ctrl" ? ctrl : alt).current.tap(performance.now());
        touched();
        return;
      }
      const mods = { ctrl: ctrl.current.on, alt: alt.current.on };
      deduper.current.reset();
      instance.sendKeys(keyBytes(key.id, { appCursor: instance.modes.appCursor, mods }));
      if (mods.ctrl || mods.alt) {
        ctrl.current.consume();
        alt.current.consume();
        touched();
      }
    },
    [id, touched],
  );

  const openSelection = useCallback(() => {
    const instance = instanceFor(id);
    if (!instance?.terminal) return;
    setSelecting((open) => open ?? instance.screenText(200));
  }, [id]);

  // A pinch scales the screen as the fingers move and settles on a font size when they lift: one
  // new size, one fit, one RESIZE. A finger resting still opens the selection layer.
  useEffect(() => {
    const el = stage.current;
    if (!el) return;
    let pinch: { distance: number; base: number; scale: number } | null = null;
    let press: { x: number; y: number; timer: ReturnType<typeof setTimeout> } | null = null;
    const cancelPress = () => {
      if (press) clearTimeout(press.timer);
      press = null;
    };
    const spread = (e: TouchEvent) => Math.hypot(e.touches[0].clientX - e.touches[1].clientX, e.touches[0].clientY - e.touches[1].clientY);
    const onStart = (e: TouchEvent) => {
      if (e.touches.length === 1) {
        cancelPress();
        const { clientX: x, clientY: y } = e.touches[0];
        press = { x, y, timer: setTimeout(() => { press = null; openSelection(); }, LONG_PRESS_MS) };
      } else if (e.touches.length === 2) {
        cancelPress();
        const box = el.getBoundingClientRect();
        const midX = (e.touches[0].clientX + e.touches[1].clientX) / 2 - box.left;
        const midY = (e.touches[0].clientY + e.touches[1].clientY) / 2 - box.top;
        if (zoom.current) zoom.current.style.transformOrigin = `${midX}px ${midY}px`;
        pinch = { distance: Math.max(1, spread(e)), base: storedFontSize(), scale: 1 };
      }
    };
    const onMove = (e: TouchEvent) => {
      if (press && e.touches.length === 1 && Math.hypot(e.touches[0].clientX - press.x, e.touches[0].clientY - press.y) > LONG_PRESS_SLOP) cancelPress();
      if (!pinch || e.touches.length !== 2) return;
      // Neither the page nor the terminal's own scrolling may take a two-finger gesture.
      e.preventDefault();
      e.stopPropagation();
      pinch.scale = spread(e) / pinch.distance;
      if (zoom.current) zoom.current.style.transform = `scale(${pinch.scale})`;
    };
    const onEnd = (e: TouchEvent) => {
      if (e.touches.length === 0) cancelPress();
      if (!pinch || e.touches.length >= 2) return;
      const { base, scale } = pinch;
      pinch = null;
      if (zoom.current) zoom.current.style.transform = "";
      const next = pinchFont(base, scale);
      if (next !== base) {
        instanceFor(id)?.interact();
        setFontSize(next);
      }
    };
    el.addEventListener("touchstart", onStart, { passive: true, capture: true });
    el.addEventListener("touchmove", onMove, { passive: false, capture: true });
    el.addEventListener("touchend", onEnd, { capture: true });
    el.addEventListener("touchcancel", onEnd, { capture: true });
    return () => {
      cancelPress();
      el.removeEventListener("touchstart", onStart, { capture: true });
      el.removeEventListener("touchmove", onMove, { capture: true });
      el.removeEventListener("touchend", onEnd, { capture: true });
      el.removeEventListener("touchcancel", onEnd, { capture: true });
    };
  }, [id, openSelection]);

  const toggleCompose = () => {
    setComposeShown((on) => {
      writeFlag(COMPOSE_KEY, !on);
      return !on;
    });
  };

  const title = state?.title || row?.title || t("term.untitled");
  const cwd = state?.cwd || row?.cwd || "";
  const env: TerminalEnvName = row?.env ?? "container";
  const running = !row || row.status === "running";
  const commands = state?.commands;
  const instance = () => instanceFor(id);
  const items: MenuItem[] = [
    { label: t("term.phone.select"), icon: "copy", onSelect: openSelection },
    ...(commands?.active
      ? [
          { label: t("term.marks.copy"), icon: "copy" as const, disabled: !commands.ended, onSelect: () => { const i = instance(); if (i) void copyLastOutput(i); } },
          { label: t("term.marks.previous"), icon: "up" as const, disabled: commands.prompts === 0, onSelect: () => instance()?.jumpToCommand(-1) },
          { label: t("term.marks.next"), icon: "down" as const, disabled: commands.prompts === 0, onSelect: () => instance()?.jumpToCommand(1) },
        ]
      : []),
    { label: t("term.search"), icon: "search", onSelect: () => instance()?.openSearch() },
    "-",
    { label: t("term.font.smaller"), icon: "compact", onSelect: () => fontSizeStep("font-smaller") },
    { label: t("term.font.bigger"), icon: "expand", onSelect: () => fontSizeStep("font-bigger") },
    { label: t(composeShown ? "term.phone.compose.hide" : "term.phone.compose.show"), icon: "pen", onSelect: toggleCompose },
    ...(menu?.length ? ["-" as const, ...menu] : []),
    ...(onEnd && running ? ["-" as const, { label: t("term.end"), icon: "stop" as const, danger: true, onSelect: onEnd }] : []),
  ];

  return createPortal(
    <div className="term-full term-phone" role="dialog" aria-label={title} data-full={id} data-phone={id}>
      <div className="term-full-head term-phone-head">
        <button className="iconbtn" onClick={onBack} aria-label={t("shell.back")} title={t("shell.back")}>
          <Icon name="back" />
        </button>
        <div className="term-phone-titles">
          <div className="term-phone-title truncate">
            {row?.sandbox && <Icon name="shield" size={12} />}
            <span className="truncate">{title}</span>
          </div>
          {/* The folder alone: the pill beside says the environment, and the line saying it again
              read "Container" twice across one header. */}
          <div className="term-phone-cwd truncate">{cwd}</div>
        </div>
        <EnvPill env={env} />
        <CopyOutputButton id={id} state={state ?? undefined} />
        <OverflowMenu items={items} label={t("term.phone.menu")} />
      </div>
      <div className="term-phone-screen" ref={stage}>
        <div className="term-phone-zoom" ref={zoom}>
          <TerminalView
            id={id}
            visible
            env={row?.env}
            workspace={workspace}
            fileOpener={fileOpener}
            focusToken={focusToken}
            onState={changed}
            onRestart={onRestart}
            onRemove={onRemove}
            rowsDelay={KEYBOARD_SETTLE_MS}
            onLongPress={openSelection}
          />
        </div>
      </div>
      {actions && <div className="term-phone-actions">{actions}</div>}
      {running && <KeyRow ctrl={ctrl.current.state} alt={alt.current.state} onPress={press} />}
      {running && composeShown && <ComposeLine id={id} compose={compose} onSent={() => deduper.current.reset()} />}
      {selecting !== null && <SelectionLayer text={selecting} onClose={() => { setSelecting(null); instanceFor(id)?.focus(); }} />}
    </div>,
    document.body,
  );
}

// ── the keys ────────────────────────────────────────────────────────────────────────────────

function KeyRow({ ctrl, alt, onPress }: { ctrl: string; alt: string; onPress: (key: PhoneKey) => void }) {
  // One key at a time: the one under the finger, whether its repeat has begun, and its timers.
  const held = useRef<{ id: string; repeated: boolean; timer: ReturnType<typeof setTimeout> | null; every: ReturnType<typeof setInterval> | null } | null>(null);
  const release = () => {
    const h = held.current;
    if (h?.timer) clearTimeout(h.timer);
    if (h?.every) clearInterval(h.every);
    held.current = null;
  };
  useEffect(() => release, []);
  // More keys than a phone is wide: the edge with more behind it fades (edgefade.ts), where a bare
  // cut left the tenth key a sliver that read as a broken button.
  const row = useRef<HTMLDivElement>(null);
  useEdgeFade(row);
  return (
    <div ref={row} className="term-keys" role="toolbar" aria-label={t("term.phone.keys")}>
      {PHONE_KEYS.map((key) => {
        const mod = key.modifier === "ctrl" ? ctrl : key.modifier === "alt" ? alt : "";
        return (
          <button
            key={key.id}
            type="button"
            className={`term-key ${mod === "once" ? "on" : ""} ${mod === "locked" ? "on locked" : ""} ${key.id.startsWith("ctrl-") ? "combo" : ""}`}
            data-key={key.id}
            aria-label={t(`term.phone.key.${key.id}`)}
            {...(key.modifier ? { "aria-pressed": mod !== "off" } : {})}
            // The finger must not take the focus from the terminal, or the soft keyboard closes: the
            // pointerdown is cancelled. The key acts when the finger lifts, so a finger that drags the
            // row sideways to reach ^C scrolls it (the browser cancels the pointer) and sends nothing.
            // An arrow held still repeats, as a hardware key does.
            onPointerDown={(e) => {
              e.preventDefault();
              release();
              const h = { id: key.id, repeated: false, timer: null as ReturnType<typeof setTimeout> | null, every: null as ReturnType<typeof setInterval> | null };
              held.current = h;
              if (REPEATS.has(key.id)) {
                h.timer = setTimeout(() => {
                  h.repeated = true;
                  onPress(key);
                  h.every = setInterval(() => onPress(key), REPEAT_EVERY_MS);
                }, REPEAT_DELAY_MS);
              }
            }}
            onPointerUp={() => {
              const h = held.current;
              release();
              if (h && h.id === key.id && !h.repeated) onPress(key);
            }}
            onPointerCancel={release}
            onPointerLeave={release}
            onClick={(e) => {
              // A click with no pointer behind it: a hardware keyboard's Enter on a focused key.
              if (e.detail === 0) onPress(key);
            }}
          >
            {key.cap}
          </button>
        );
      })}
    </div>
  );
}

// ── the compose line ────────────────────────────────────────────────────────────────────────

/** How tall the compose line may grow before it scrolls: four lines of text. */
const COMPOSE_MAX_PX = 104;

function ComposeLine({ id, compose, onSent }: { id: string; compose?: PhoneTerminalProps["compose"]; onSent: () => void }) {
  const [text, setText] = useState("");
  const [enter, setEnter] = useState(() => readFlag(ENTER_KEY, true));
  const [busy, setBusy] = useState(false);
  const field = useRef<HTMLTextAreaElement>(null);

  const grow = () => {
    const el = field.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, COMPOSE_MAX_PX)}px`;
  };
  useEffect(grow, [text]);

  const send = async (e?: FormEvent) => {
    e?.preventDefault();
    if (busy) return;
    if (compose) {
      if (!text.trim()) return;
      setBusy(true);
      try {
        if (await compose.onSend(text)) setText("");
      } finally {
        setBusy(false);
      }
      return;
    }
    const instance = instanceFor(id);
    if (!instance) return;
    // An empty line with Enter on is a bare Enter: the one key the soft keyboard's own Enter gives a
    // text field and not the terminal.
    if (!text && !enter) return;
    onSent();
    instance.sendKeys(composeBytes(text, { bracketed: instance.modes.bracketedPaste, enter }));
    setText("");
  };

  const toggleEnter = () => {
    setEnter((on) => {
      writeFlag(ENTER_KEY, !on);
      return !on;
    });
  };

  return (
    <form className="term-compose" onSubmit={(e) => void send(e)}>
      <textarea
        ref={field}
        className="term-compose-field"
        rows={1}
        value={text}
        placeholder={compose?.placeholder ?? t("term.phone.compose")}
        aria-label={compose?.placeholder ?? t("term.phone.compose")}
        // Autocorrect, suggestions and dictation stay on: they are why this line exists. A shell's
        // words are not sentences, so only a message to a person starts with a capital.
        autoCapitalize={compose ? "sentences" : "off"}
        enterKeyHint="send"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send();
        }}
      />
      {!compose && (
        <button
          type="button"
          className={`iconbtn term-compose-enter ${enter ? "on" : ""}`}
          aria-pressed={enter}
          aria-label={t("term.phone.enter")}
          title={t("term.phone.enter")}
          onPointerDown={(e) => e.preventDefault()}
          onClick={toggleEnter}
        >
          ⏎
        </button>
      )}
      <button type="submit" className="iconbtn primary term-compose-send" aria-label={t("term.phone.send")} title={t("term.phone.send")} disabled={busy || (!!compose && !text.trim())} onPointerDown={(e) => e.preventDefault()}>
        <Icon name="up" />
      </button>
    </form>
  );
}

// ── the selection layer ─────────────────────────────────────────────────────────────────────

/**
 * The terminal's text as plain text, over the terminal, where the phone's own selection handles work:
 * xterm.js draws on a canvas and its selection is a mouse's. The rows on screen and 200 above them.
 */
function SelectionLayer({ text, onClose }: { text: string; onClose: () => void }) {
  const pre = useRef<HTMLPreElement>(null);
  const [selected, setSelected] = useState("");
  useEffect(() => {
    if (pre.current) pre.current.scrollTop = pre.current.scrollHeight;
    const onChange = () => {
      const selection = document.getSelection();
      const inside = !!selection && !!pre.current && selection.rangeCount > 0 && pre.current.contains(selection.anchorNode);
      setSelected(inside ? selection!.toString() : "");
    };
    document.addEventListener("selectionchange", onChange);
    return () => document.removeEventListener("selectionchange", onChange);
  }, []);
  const copy = async (what: string, done: string) => {
    toast((await copyText(what)) ? done : t("term.phone.copyFailed"));
    onClose();
  };
  return (
    <div className="term-select" role="dialog" aria-label={t("term.phone.select")}>
      <div className="term-select-head">
        <span className="grow sub">{t("term.phone.select.hint")}</span>
        <button className="btn small primary" disabled={!selected} onClick={() => void copy(selected, t("term.phone.copied"))}>{t("term.copy")}</button>
        <button className="btn small" onClick={() => void copy(text, t("term.phone.copiedAll"))}>{t("term.phone.copyAll")}</button>
        <button className="iconbtn" onClick={onClose} aria-label={t("common.close")} title={t("common.close")}><Icon name="close" /></button>
      </div>
      <pre className="term-select-text" ref={pre}>{text}</pre>
    </div>
  );
}
