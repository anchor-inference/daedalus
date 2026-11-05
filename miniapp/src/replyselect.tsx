// Replying to a passage of an answer, on the screen: the pill over a selection in an agent's answer, the
// popover (a sheet on a phone) where the reply is written, the cards the replies become in the
// composer, the dialog that edits one, and the quotes a sent message is drawn with. The wire form and
// the list per chat are in quotereply.ts.

import { type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Icon } from "./icons";
import { t } from "./i18n";
import { addQuoteReply, cleanQuote, type QuoteReply, removeQuoteReply, type SentReply, updateQuoteReply } from "./quotereply";
import { Sheet, useLayer } from "./ui/dialogs";
import { BottomSheet } from "./ui/phone";

/** The attribute an element carries when a selection inside it may be answered: the seq of the agent
 *  message it draws, or empty when that is not known. Only an agent's answer carries it, so the
 *  operator's own words, the composer and the tool cards never offer the pill. */
export const QUOTABLE = "data-quote";

type Picked = { quote: string; seq: number | null; range: Range };
type Place = { left: number; top: number; below: boolean };

const PILL_GAP = 8;
const PILL_HEIGHT = 34;

// The selection is painted by name while the popover has the focus: a field that takes the focus
// takes the document's selection with it, and the passage being answered would go blank under the
// popover that answers it. The registry is missing in older engines, where the popover simply opens.
function paint(name: string, range: Range | null): void {
  try {
    if (typeof CSS === "undefined" || !("highlights" in CSS) || typeof Highlight === "undefined") return;
    if (range) CSS.highlights.set(name, new Highlight(range));
    else CSS.highlights.delete(name);
  } catch {
    /* an engine without the registry */
  }
}

function reducedMotion(): boolean {
  try {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  } catch {
    return false;
  }
}

/** What the selection answers, if it lies in one answer of this conversation and nowhere else. */
export function pickedFrom(selection: Selection | null, scroller: HTMLElement | null): Picked | null {
  if (!selection || selection.isCollapsed || selection.rangeCount === 0 || !scroller) return null;
  const range = selection.getRangeAt(0);
  const node = range.commonAncestorContainer;
  const element = node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement;
  const source = element?.closest(`[${QUOTABLE}]`);
  // A conversation embedded in this one (the side question's window) has a list of its own; its
  // selections belong to its own pill.
  if (!source || !scroller.contains(source) || source.closest(".chat-scroll") !== scroller) return null;
  const quote = cleanQuote(selection.toString());
  if (!quote) return null;
  const seq = Number(source.getAttribute(QUOTABLE));
  return { quote, seq: Number.isFinite(seq) && seq > 0 ? seq : null, range: range.cloneRange() };
}

/** Whether two picks are the same passage: the same words of the same answer, between the same points. */
function samePick(a: Picked, b: Picked): boolean {
  return a.quote === b.quote && a.seq === b.seq
    && a.range.startContainer === b.range.startContainer && a.range.startOffset === b.range.startOffset
    && a.range.endContainer === b.range.endContainer && a.range.endOffset === b.range.endOffset;
}

/**
 * Where the pill goes: above the selection's first line, centred on the selection; below its last line
 * when there is no room above or on a touch screen, where the system's own copy menu takes the space
 * above a long-press selection and would cover the pill.
 */
export function pillPlace(range: Range, bounds: DOMRect, width: number, touch: boolean): Place | null {
  const rects = Array.from(range.getClientRects()).filter((rect) => rect.width > 0 || rect.height > 0);
  if (!rects.length) return null;
  const first = rects[0];
  const last = rects[rects.length - 1];
  const whole = range.getBoundingClientRect();
  if (whole.bottom < bounds.top || whole.top > bounds.bottom) return null;
  const centre = whole.left + whole.width / 2;
  const left = Math.max(bounds.left + 8, Math.min(centre - width / 2, bounds.right - width - 8));
  const above = first.top - PILL_HEIGHT - PILL_GAP;
  const below = touch || above < bounds.top + 4;
  return { left, top: below ? Math.min(last.bottom + PILL_GAP, bounds.bottom - PILL_HEIGHT - 4) : above, below };
}

/**
 * The pill over a selection in an agent's answer, and the reply it opens. Mounted once per conversation;
 * it draws nothing until the operator selects words in an answer of that conversation.
 */
export function SelectionReply({ sessionId, scroller, phone, onAskAside }: { sessionId: string; scroller: RefObject<HTMLElement | null>; phone: boolean; onAskAside?: (quote: string) => void }) {
  const [picked, setPicked] = useState<Picked | null>(null);
  const [writing, setWriting] = useState(false);
  const [place, setPlace] = useState<Place | null>(null);
  const pill = useRef<HTMLDivElement>(null);
  const pointerDown = useRef(false);
  const writingNow = useRef(false);
  writingNow.current = writing;
  const pickedNow = useRef(picked);
  pickedNow.current = picked;
  const touch = useRef(phone);
  touch.current = phone;

  const close = useCallback(() => {
    setWriting(false);
    setPicked(null);
    setPlace(null);
    paint("reply-pick", null);
  }, []);

  // Read the selection once it has settled: after the mouse button is up (a drag fires a change per
  // letter), and a moment after a touch screen's handles stop moving.
  useEffect(() => {
    let timer = 0;
    const read = () => {
      // A pane's edge being dragged is not a selection being made, and the drag is held to commit
      // React no more than a still pointer does (check_pane_resize.py).
      if (writingNow.current || pointerDown.current || document.body.classList.contains("resizing")) return;
      const next = pickedFrom(window.getSelection(), scroller.current);
      // Every click anywhere on the page ends here. Nothing in an answer selected before or now, or
      // the very same passage again, changes nothing, and setting state for it re-rendered the
      // conversation's pill host for nothing.
      const before = pickedNow.current;
      if (next === null ? before === null : before !== null && samePick(before, next)) return;
      setPicked(next);
      if (!next) setPlace(null);
    };
    const later = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(read, touch.current ? 260 : 120);
    };
    const down = (event: PointerEvent) => {
      if (pill.current?.contains(event.target as Node)) return;
      if (event.pointerType === "mouse") pointerDown.current = true;
    };
    const up = () => {
      if (!pointerDown.current) return;
      pointerDown.current = false;
      later();
    };
    document.addEventListener("selectionchange", later);
    document.addEventListener("pointerdown", down, true);
    document.addEventListener("pointerup", up, true);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("selectionchange", later);
      document.removeEventListener("pointerdown", down, true);
      document.removeEventListener("pointerup", up, true);
    };
  }, [scroller]);

  // Leaving the chat takes the pill with it, and the popover with what was half written.
  useEffect(() => close, [sessionId, close]);

  // The pill follows its passage while the list scrolls or the window changes size.
  useLayoutEffect(() => {
    if (!picked || writing) return;
    const host = scroller.current;
    if (!host) return;
    const measure = () => setPlace(pillPlace(picked.range, host.getBoundingClientRect(), pill.current?.offsetWidth || 200, touch.current));
    measure();
    host.addEventListener("scroll", measure, { passive: true });
    window.addEventListener("resize", measure);
    return () => {
      host.removeEventListener("scroll", measure);
      window.removeEventListener("resize", measure);
    };
  }, [picked, writing, scroller]);

  if (!picked) return null;
  const startReply = () => {
    paint("reply-pick", picked.range);
    setWriting(true);
  };
  const askAside = () => {
    const quote = picked.quote;
    window.getSelection()?.removeAllRanges();
    close();
    onAskAside?.(quote);
  };
  const add = (text: string) => {
    addQuoteReply(sessionId, { quote: picked.quote, text, seq: picked.seq });
    window.getSelection()?.removeAllRanges();
    close();
  };

  if (writing) {
    return phone
      ? <ReplySheet quote={picked.quote} onAdd={add} onClose={close} />
      : <ReplyPopover quote={picked.quote} range={picked.range} scroller={scroller} onAdd={add} onClose={close} />;
  }
  return createPortal(
    <div ref={pill} className="reply-pill" role="toolbar" aria-label={t("quote.toolbar")} data-below={place?.below ? "" : undefined}
      style={place ? { left: place.left, top: place.top } : { visibility: "hidden", left: 0, top: 0 }}
      // The selection must outlive the press on the pill: a button that takes the focus clears it.
      onPointerDown={(event) => event.preventDefault()} onMouseDown={(event) => event.preventDefault()}>
      <button type="button" className="reply-pill-btn" data-action="reply" onClick={startReply}><Icon name="reply" size={14} />{t("quote.reply")}</button>
      {onAskAside && <>
        <span className="reply-pill-sep" aria-hidden />
        <button type="button" className="reply-pill-btn" data-action="aside" onClick={askAside}><Icon name="ask" size={14} />{t("quote.aside")}</button>
      </>}
    </div>,
    document.body,
  );
}

/** The quote behind its accent bar, clamped to `lines`. */
function QuoteBar({ quote, lines }: { quote: string; lines?: 2 | 3 }) {
  return <div className={`reply-quote ${lines ? `clamp-${lines}` : ""}`}>{quote}</div>;
}

function ReplyField({ onAdd, onCancel }: { onAdd: (text: string) => void; onCancel: () => void }) {
  const [text, setText] = useState("");
  const ready = text.trim() !== "";
  const input = useRef<HTMLInputElement>(null);
  // Focused a frame after mounting rather than by `autoFocus`: the popover is hidden until it has been
  // measured, and a hidden field cannot take the focus; a sheet focuses its own panel as it opens.
  useEffect(() => {
    const frame = requestAnimationFrame(() => input.current?.focus({ preventScroll: true }));
    return () => cancelAnimationFrame(frame);
  }, []);
  return (
    <form className="reply-form" onSubmit={(event) => { event.preventDefault(); if (ready) onAdd(text); }}>
      <input ref={input} className="reply-input" value={text} placeholder={t("quote.placeholder")} aria-label={t("quote.placeholder")} maxLength={4000}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onCancel(); } }} />
      <button type="submit" className="roundbtn primary reply-add" disabled={!ready} aria-label={t("quote.add")} title={t("quote.add")}><Icon name="up" size={16} /></button>
    </form>
  );
}

/** The popover where a desktop writes the reply, anchored where the pill was. */
function ReplyPopover({ quote, range, scroller, onAdd, onClose }: { quote: string; range: Range; scroller: RefObject<HTMLElement | null>; onAdd: (text: string) => void; onClose: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [point, setPoint] = useState<{ left: number; top: number } | null>(null);
  useLayer(onClose);
  useLayoutEffect(() => {
    const host = scroller.current;
    const measure = () => {
      const panel = box.current;
      if (!panel || !host) return;
      const bounds = host.getBoundingClientRect();
      const whole = range.getBoundingClientRect();
      const rects = Array.from(range.getClientRects());
      const width = panel.offsetWidth;
      const height = panel.offsetHeight;
      const left = Math.max(8, Math.min(whole.left + whole.width / 2 - width / 2, window.innerWidth - width - 8));
      const firstTop = rects[0]?.top ?? whole.top;
      const above = firstTop - height - PILL_GAP;
      const top = above >= Math.max(8, bounds.top) ? above : Math.min((rects.at(-1)?.bottom ?? whole.bottom) + PILL_GAP, window.innerHeight - height - 8);
      setPoint({ left, top });
    };
    measure();
    host?.addEventListener("scroll", measure, { passive: true });
    window.addEventListener("resize", measure);
    return () => {
      host?.removeEventListener("scroll", measure);
      window.removeEventListener("resize", measure);
    };
  }, [range, scroller]);
  // A press anywhere else gives the reply up, as Escape does.
  useEffect(() => {
    const outside = (event: PointerEvent) => { if (!box.current?.contains(event.target as Node)) onClose(); };
    document.addEventListener("pointerdown", outside, true);
    return () => document.removeEventListener("pointerdown", outside, true);
  }, [onClose]);
  return createPortal(
    <div ref={box} className="reply-popover" role="dialog" aria-label={t("quote.sheet")} style={point ?? { visibility: "hidden", left: 0, top: 0 }}>
      <QuoteBar quote={quote} lines={2} />
      <ReplyField onAdd={onAdd} onCancel={onClose} />
    </div>,
    document.body,
  );
}

/** The phone's reply: the same quote and field, in a sheet from the bottom, over the keyboard. */
function ReplySheet({ quote, onAdd, onClose }: { quote: string; onAdd: (text: string) => void; onClose: () => void }) {
  return (
    <BottomSheet title={t("quote.sheet")} onClose={onClose} className="reply-sheet">
      <QuoteBar quote={quote} lines={3} />
      <ReplyField onAdd={onAdd} onCancel={onClose} />
    </BottomSheet>
  );
}

// ── in the composer ──────────────────────────────────────────────────────────────────────────

/** A reply waiting in the composer: its words over a "Reply" chip; it opens to the whole of it. */
export function ReplyCard({ reply, onOpen, onRemove }: { reply: QuoteReply; onOpen: () => void; onRemove: () => void }) {
  return (
    <div className="attachment reply-card" data-reply={reply.id}>
      <button type="button" className="reply-card-open" onClick={onOpen} title={reply.quote} aria-label={t("quote.open", { text: reply.text })}>
        <span className="reply-card-text clamp-2">{reply.text}</span>
        <span className="reply-card-chip"><Icon name="reply" size={11} />{t("quote.chip")}</span>
      </button>
      <button type="button" className="attachment-x reply-card-x" onClick={onRemove} aria-label={t("quote.remove")} title={t("quote.remove")}>
        <Icon name="close" size={12} />
      </button>
    </div>
  );
}

/** The whole of one reply: the passage, and the words, which can be changed here. */
export function ReplyDialog({ sessionId, reply, phone, onClose }: { sessionId: string; reply: QuoteReply; phone: boolean; onClose: () => void }) {
  const [text, setText] = useState(reply.text);
  const save = () => {
    if (text.trim() && text !== reply.text) updateQuoteReply(sessionId, reply.id, text);
    onClose();
  };
  const body = (
    <div className="reply-dialog">
      <QuoteBar quote={reply.quote} />
      <textarea className="field reply-dialog-text" value={text} rows={3} maxLength={4000} aria-label={t("quote.placeholder")} placeholder={t("quote.placeholder")}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={(event) => { if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); save(); } }} />
      <div className="reply-dialog-actions">
        <button type="button" className="btn small danger" onClick={() => { removeQuoteReply(sessionId, reply.id); onClose(); }}>{t("common.remove")}</button>
        <span className="grow" />
        <button type="button" className="btn small" onClick={onClose}>{t("common.cancel")}</button>
        <button type="button" className="btn small primary" disabled={!text.trim()} onClick={save}>{t("common.save")}</button>
      </div>
    </div>
  );
  return phone
    ? <BottomSheet title={t("quote.dialog")} onClose={onClose} className="reply-sheet">{body}</BottomSheet>
    : <Sheet title={t("quote.dialog")} onClose={onClose} size="narrow" className="reply-dialog-sheet">{body}</Sheet>;
}

// ── in a sent message ────────────────────────────────────────────────────────────────────────

/** The text range of `needle` inside `root`, matched with every run of white space as one space. */
export function findPassage(root: Element, needle: string): Range | null {
  const want = needle.replace(/\s+/g, " ").trim();
  if (!want) return null;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let flat = "";
  const map: [Text, number][] = [];
  let space = true;
  for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
    const value = node.data;
    for (let i = 0; i < value.length; i++) {
      const blank = /\s/.test(value[i]);
      if (blank && space) continue;
      flat += blank ? " " : value[i];
      space = blank;
      map.push([node, i]);
    }
  }
  const at = flat.indexOf(want);
  if (at < 0) return null;
  const [startNode, startOffset] = map[at];
  const [endNode, endOffset] = map[at + want.length - 1];
  const range = document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset + 1);
  return range;
}

let revealTimer = 0;

/**
 * Take the reader to the passage a reply quoted and light it for a moment. The whole quote is looked
 * for first and its first line after, since a passage across blocks reads differently once a list's
 * markers or a code block's buttons sit between its lines. Returns false when the answer is not drawn.
 */
export function revealPassage(root: HTMLElement | null, seq: number | null, quote: string): boolean {
  if (!root) return false;
  const source = seq != null ? root.querySelector<HTMLElement>(`[${QUOTABLE}="${seq}"]`) : null;
  if (!source) return false;
  const firstLine = quote.split("\n").find((line) => line.trim()) ?? "";
  const range = findPassage(source, quote) ?? findPassage(source, firstLine);
  const target = (range?.startContainer.parentElement ?? source);
  target.scrollIntoView({ block: "center", behavior: reducedMotion() ? "auto" : "smooth" });
  window.clearTimeout(revealTimer);
  if (range && typeof Highlight !== "undefined" && typeof CSS !== "undefined" && "highlights" in CSS) {
    paint("reply-reveal", range);
    revealTimer = window.setTimeout(() => paint("reply-reveal", null), 2600);
  } else {
    source.classList.remove("anchored");
    void source.offsetWidth;
    source.classList.add("anchored");
    revealTimer = window.setTimeout(() => source.classList.remove("anchored"), 2600);
  }
  return true;
}

/** The replies a sent message opens with: each quote, which leads back to its passage, and its words. */
export function SentReplies({ replies, onReveal }: { replies: SentReply[]; onReveal?: (reply: SentReply) => void }) {
  return (
    <div className="sent-replies">
      {replies.map((reply, index) => (
        <div key={index} className="sent-reply">
          <button type="button" className="sent-reply-quote" onClick={() => onReveal?.(reply)} disabled={!onReveal || reply.seq == null}
            title={reply.seq != null ? t("quote.reveal") : undefined} aria-label={t("quote.reveal")}>
            <span className="clamp-3">{reply.quote}</span>
          </button>
          {reply.text && <div className="sent-reply-text">{reply.text}</div>}
        </div>
      ))}
    </div>
  );
}
