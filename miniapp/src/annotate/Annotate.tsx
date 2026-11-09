// The markup editor: a picture over the whole window, a few tools to point at what matters on it, and
// Done, which hands the picture with its marks back. Opened by the Browser and Preview tabs' Snapshot
// button, and by the pencil on a picture waiting in a composer.
//
// The picture is drawn at its natural size on one canvas, which is what is shown (scaled by CSS) and
// what is sent: no second rendering at another scale, and no mark that drifts between what was seen and
// what was sent. The steps are replayed over the untouched original (see ops.ts), but only when Undo
// asks for it; a new step is drawn onto the canvas as it stands. A stroke still under the finger lives
// on a second, screen-sized canvas over the first, so a move of the pointer redraws a few hundred
// thousand pixels rather than a large screenshot's sixteen million.

import { useContext, useEffect, useLayoutEffect, useReducer, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useLayer } from "../ui/dialogs";
import { Icon } from "../icons";
import { t } from "../i18n";
import { errorText } from "../ui";
import {
  SWATCHES, TEXT_FONT, WEIGHTS, blurBlock, contrastOf, drawMark, fitBox, labelBox, rectOf, snapArrow, snapshotName, squareOf, strokeWidth, textSize, toPicture,
  type DrawingTool, type Mark, type Point, type Tool,
} from "./draw";
import { EMPTY_HISTORY, applyOp, cropRect, drawOriginal, historyOf, historyReducer, startsWith, type Op } from "./ops";
import { ComposeTarget, handToComposer } from "./compose";
import type { Snapshot } from "./source";
import "./annotate.css";

const TOOLS: Tool[] = ["pen", "ellipse", "box", "arrow", "text", "blur", "crop"];

/**
 * One key per tool, read by the key's place on the keyboard rather than the letter it types, so the
 * same keys work in a Russian layout. The letters are the tools' English initials where they are free.
 */
const TOOL_KEYS: Record<Tool, string> = { pen: "KeyP", ellipse: "KeyO", box: "KeyR", arrow: "KeyA", text: "KeyT", blur: "KeyB", crop: "KeyC" };
const ROTATE_KEY = "KeyQ";
const letterOf = (code: string) => code.replace("Key", "");

/** The text's size follows the line weight, less steeply: thick letters crowd a picture sooner than thick lines. */
const TEXT_WEIGHTS = [0.8, 1, 1.4] as const;

/** The tools' glyphs: the icon set has a pen but none of the others, and each of these is a few strokes. */
function ToolGlyph({ tool }: { tool: Tool }) {
  if (tool === "pen") return <Icon name="pen" size={16} />;
  const paths: Record<Exclude<Tool, "pen">, string> = {
    arrow: "M4 12 L12 4 M6.5 4 H12 V9.5",
    box: "M3 4.5 H13 V11.5 H3 Z",
    ellipse: "M2.5 8 A5.5 4.2 0 1 0 13.5 8 A5.5 4.2 0 1 0 2.5 8 Z",
    text: "M4 4 H12 M8 4 V13",
    blur: "M3 3 H13 V13 H3 Z M3 8 H13 M8 3 V13 M5.5 5.5 h0.01 M10.5 10.5 h0.01",
    crop: "M4.5 1.5 V11.5 H14.5 M1.5 4.5 H11.5 V14.5",
  };
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d={paths[tool]} /></svg>;
}

function RotateGlyph() {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d="M12.8 8.5 A4.8 4.8 0 1 1 11 4.4 M11.6 1.8 V4.8 H8.6" /></svg>;
}

function WeightGlyph({ weight }: { weight: number }) {
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeLinecap="round" aria-hidden><path d="M3 8 H13" strokeWidth={[1.2, 2.4, 4][weight]} /></svg>;
}

/** What is under the pointer while a drag goes on: a mark being drawn, or the rectangle of a crop. */
type Draft = Mark | { tool: "crop"; from: Point; to: Point };
type Typing = { at: Point; left: number; top: number; text: string };

export type AnnotateProps = {
  snapshot: Snapshot;
  onCancel: () => void;
  /** The finished picture, and the steps that made it from the original, to start from next time. */
  onAttach: (file: File, ops: Op[]) => Promise<void> | void;
  /** Done with no step at all: the caller keeps what it had rather than a re-encoded copy of it. */
  onUnchanged?: () => void;
  /** The steps taken the last time, replayed over the original and each one undoable. */
  initial?: Op[];
  /** "attach" for a snapshot that goes to a composer, "done" for a picture that is already in one. */
  finish?: "attach" | "done";
  title?: string;
  /** The finished file's name; a snapshot's is made from where it was taken. */
  name?: string;
};

export function Annotate({ snapshot, onCancel, onAttach, onUnchanged, initial, finish = "attach", title, name }: AnnotateProps) {
  const host = useRef<HTMLDivElement>(null);
  const overlay = useRef<HTMLCanvasElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const [history, dispatch] = useReducer(historyReducer, initial, (start) => (start?.length ? historyOf(start) : EMPTY_HISTORY));
  const [tool, setTool] = useState<Tool>("arrow");
  const [color, setColor] = useState<string>(SWATCHES[0]);
  const [weight, setWeight] = useState(1);
  const [typing, setTyping] = useState<Typing | null>(null);
  const [room, setRoom] = useState({ width: 0, height: 0 });
  const [sheet, setSheet] = useState<HTMLCanvasElement | null>(null);
  const [busy, setBusy] = useState(false);
  const width = sheet?.width ?? snapshot.width;
  const height = sheet?.height ?? snapshot.height;
  const shown = fitBox(width, height, room.width, room.height);
  // Lines and letters are never thinner on screen than a pen's stroke: a desktop frame shown at a third
  // of its size on a phone drew hairlines, and what is drawn is what is sent, so the file has them too.
  const scale = shown.width && width ? shown.width / width : 1;
  const line = Math.max(1, Math.round(Math.max(strokeWidth(width, height) * WEIGHTS[weight], 2.5 / scale)));
  const letters = Math.max(Math.round(textSize(width, height) * TEXT_WEIGHTS[weight]), Math.round(15 / scale));
  const lastDrawing = useRef<DrawingTool>("arrow");
  const choose = (next: Tool) => {
    commitText();
    if (next !== "crop") lastDrawing.current = next;
    setTool(next);
  };

  // The canvas the steps so far are drawn on, and which steps those are.
  const rendered = useRef<{ ops: Op[]; canvas: HTMLCanvasElement } | null>(null);
  const bring = (ops: Op[]): HTMLCanvasElement => {
    const last = rendered.current;
    let canvas: HTMLCanvasElement;
    let done = 0;
    if (last && startsWith(ops, last.ops)) {
      canvas = last.canvas;
      done = last.ops.length;
    } else {
      // Undo, or the first drawing: start again from the original, on the old canvas when it is the
      // original's size, since a large one is worth not allocating twice.
      canvas = last && last.canvas.width === snapshot.width && last.canvas.height === snapshot.height ? last.canvas : document.createElement("canvas");
      canvas.width = snapshot.width;
      canvas.height = snapshot.height;
      drawOriginal(canvas, snapshot.image);
    }
    for (const op of ops.slice(done)) canvas = applyOp(canvas, op);
    rendered.current = { ops, canvas };
    return canvas;
  };

  // Laid out before the browser paints, so the finished stroke and the one under the finger never
  // both vanish for a frame between the overlay being cleared and the canvas getting the mark.
  useLayoutEffect(() => {
    setSheet(bring(history.ops));
    live.current = null;
    paintOverlay();
  }, [history.ops]); // eslint-disable-line react-hooks/exhaustive-deps

  useLayoutEffect(() => {
    const box = host.current;
    if (!box || !sheet) return;
    sheet.className = "annotate-canvas";
    if (box.firstChild !== sheet) box.replaceChildren(sheet);
  }, [sheet]);

  // The words being typed, read by whichever of Enter, a blur, a stroke, a tool change or Done comes
  // first, and taken by it: a click on the picture both blurs the field and starts a stroke, and the
  // two would otherwise add the same words twice. Losing them to a stray click is worse still, which is
  // why everything that moves on commits rather than drops them.
  const pending = useRef<Typing | null>(null);
  const type = (next: Typing | null) => {
    pending.current = next;
    setTyping(next);
  };
  const commitText = (): Op | null => {
    const current = pending.current;
    type(null);
    if (!current || !current.text.trim()) return null;
    const op: Op = { kind: "mark", mark: { tool: "text", color, size: letters, at: current.at, text: current.text.trim() } };
    dispatch({ kind: "add", op });
    return op;
  };

  // The stroke under the pointer, kept out of React's state: a pen sends a hundred moves a second, and
  // a render of the whole editor for each of them is what made long strokes lag on a phone.
  const live = useRef<{ id: number; draft: Draft } | null>(null);
  const penSeen = useRef(false);
  const frame = useRef(0);
  const paintOverlay = () => {
    const canvas = overlay.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const draft = live.current?.draft;
    if (!draft || !width) return;
    const k = canvas.width / width;
    ctx.setTransform(k, 0, 0, k, 0, 0);
    if (draft.tool === "crop" || draft.tool === "blur") {
      const r = rectOf(draft.from, draft.to);
      ctx.save();
      if (draft.tool === "crop") {
        // What the crop will cut away is dimmed, so the part that stays is the one still bright.
        ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
        ctx.beginPath();
        ctx.rect(0, 0, width, height);
        ctx.rect(r.x, r.y, r.w, r.h);
        ctx.fill("evenodd");
      } else {
        ctx.fillStyle = "rgba(128, 128, 128, 0.6)";
        ctx.fillRect(r.x, r.y, r.w, r.h);
      }
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.5 / k;
      ctx.setLineDash([6 / k, 4 / k]);
      ctx.strokeRect(r.x, r.y, r.w, r.h);
      ctx.restore();
      return;
    }
    drawMark(ctx, draft);
  };
  const schedule = () => {
    if (frame.current) return;
    frame.current = requestAnimationFrame(() => {
      frame.current = 0;
      paintOverlay();
    });
  };
  useEffect(() => () => cancelAnimationFrame(frame.current), []);

  // The overlay is the shown picture's size at the screen's density: sharp, and small whatever the picture.
  useLayoutEffect(() => {
    const canvas = overlay.current;
    if (!canvas) return;
    const density = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.max(1, Math.round(shown.width * density));
    canvas.height = Math.max(1, Math.round(shown.height * density));
    if (sheet) {
      sheet.style.width = `${shown.width}px`;
      sheet.style.height = `${shown.height}px`;
    }
    paintOverlay();
  }, [shown.width, shown.height, sheet]); // eslint-disable-line react-hooks/exhaustive-deps

  // Escape inside the text field drops the words, and in the middle of a drag drops the drag, rather
  // than closing the editor.
  useLayer(() => {
    if (pending.current) type(null);
    else if (live.current) {
      live.current = null;
      paintOverlay();
    } else onCancel();
  });

  const rotate = () => {
    commitText();
    dispatch({ kind: "add", op: { kind: "rotate" } });
  };

  const keys = useRef<(e: KeyboardEvent) => void>(() => undefined);
  keys.current = (e: KeyboardEvent) => {
    if (e.key === "Escape" || e.isComposing) return; // Escape is the layer's, above
    if (e.target instanceof HTMLInputElement) return; // the words being typed are the field's
    const command = e.ctrlKey || e.metaKey;
    if (command && !e.altKey && e.code === "KeyZ") dispatch({ kind: e.shiftKey ? "redo" : "undo" });
    else if (command && !e.altKey && e.code === "KeyY") dispatch({ kind: "redo" });
    else if (e.key === "Enter" && (command || !(e.target instanceof HTMLButtonElement))) {
      // Enter on a focused button presses that button, as everywhere; anywhere else it is Done.
      if (!busy) void finishEditing();
    } else if (command || e.altKey) return;
    else if (e.code === ROTATE_KEY) rotate();
    else if (e.code === "BracketLeft") setWeight((w) => Math.max(0, w - 1));
    else if (e.code === "BracketRight") setWeight((w) => Math.min(WEIGHTS.length - 1, w + 1));
    else if (/^Digit[1-9]$/.test(e.code) && Number(e.code.slice(5)) <= SWATCHES.length) setColor(SWATCHES[Number(e.code.slice(5)) - 1]);
    else {
      const picked = TOOLS.find((name) => TOOL_KEYS[name] === e.code);
      if (!picked) {
        if (e.key.length === 1) e.stopPropagation(); // a stray letter is not the chat's, under the editor
        return;
      }
      choose(picked);
    }
    e.preventDefault();
    // The chat under the editor answers single keys of its own (Y and N on a pending approval); while
    // the editor is open, the keys it uses are its alone.
    e.stopPropagation();
  };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => keys.current(e);
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true });
  }, []);

  // The picture is as large as the stage allows at its own proportions; the stage follows the window.
  useLayoutEffect(() => {
    const el = stage.current;
    if (!el) return;
    const measure = () => setRoom({ width: el.clientWidth, height: el.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const typingOpen = typing !== null;
  useEffect(() => {
    if (typingOpen) field.current?.focus();
  }, [typingOpen]);

  const pointAt = (e: { clientX: number; clientY: number }, box: DOMRect) => toPicture(e.clientX, e.clientY, box, width, height);

  const down = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.pointerType === "pen") penSeen.current = true;
    // Palm rejection: once a stylus has drawn, a touch is the hand resting on the glass.
    if (e.pointerType === "touch" && penSeen.current) return;
    if (live.current) {
      // A second finger mid-stroke is a pinch or a palm, never a wish to keep the line it started.
      if (e.pointerId !== live.current.id) {
        live.current = null;
        paintOverlay();
      }
      return;
    }
    if (e.button !== 0) return;
    e.preventDefault();
    const at = pointAt(e, e.currentTarget.getBoundingClientRect());
    commitText();
    if (tool === "text") {
      const box = stage.current!.getBoundingClientRect();
      type({ at, left: e.clientX - box.left, top: e.clientY - box.top, text: "" });
      return;
    }
    e.currentTarget.setPointerCapture?.(e.pointerId);
    const draft: Draft = tool === "pen" ? { tool, color, width: line, points: [at] }
      : tool === "blur" ? { tool, from: at, to: at, block: blurBlock(width, height) }
      : tool === "crop" ? { tool, from: at, to: at }
      : { tool, color, width: line, from: at, to: at };
    live.current = { id: e.pointerId, draft };
    schedule();
  };
  const move = (e: React.PointerEvent<HTMLDivElement>) => {
    const current = live.current;
    if (!current || current.id !== e.pointerId) return;
    const box = e.currentTarget.getBoundingClientRect();
    const draft = current.draft;
    if (draft.tool === "pen") {
      // Every sample the browser coalesced into this event, not only the last: a fast curve drawn with
      // a finger otherwise comes out as a polygon.
      const samples = e.nativeEvent.getCoalescedEvents?.() ?? [];
      const least = 1 / scale; // one screen pixel; closer samples add weight to the path and nothing to the line
      for (const sample of samples.length ? samples : [e.nativeEvent]) {
        const at = pointAt(sample, box);
        const prev = draft.points[draft.points.length - 1];
        if (Math.hypot(at.x - prev.x, at.y - prev.y) >= least) draft.points.push(at);
      }
    } else if (draft.tool !== "text") {
      const at = pointAt(e, box);
      draft.to = draft.tool === "arrow" ? snapArrow(draft.from, at, e.altKey, e.shiftKey) : e.shiftKey ? clampPoint(squareOf(draft.from, at), width, height) : at;
    }
    schedule();
  };
  const up = (e: React.PointerEvent<HTMLDivElement>) => {
    const current = live.current;
    if (!current || current.id !== e.pointerId) return;
    const draft = current.draft;
    if (draft.tool === "crop") {
      dispatch({ kind: "add", op: { kind: "crop", rect: cropRect(rectOf(draft.from, draft.to), width, height) } });
      // A crop is done once; the tool goes back to what was drawing, which is what comes next.
      setTool(lastDrawing.current);
    } else dispatch({ kind: "add", op: { kind: "mark", mark: draft } });
    live.current = null;
    schedule();
  };
  const cancelDrag = (e: React.PointerEvent<HTMLDivElement>) => {
    if (live.current?.id !== e.pointerId) return;
    live.current = null;
    schedule();
  };

  const finishEditing = async () => {
    const typed = commitText();
    const ops = typed ? [...history.ops, typed] : history.ops;
    if (!ops.length && onUnchanged) {
      onUnchanged();
      return;
    }
    setBusy(true);
    try {
      // The canvas on screen is the file: encoding it directly keeps one copy of a large picture in
      // memory, not two.
      const canvas = bring(ops);
      const blob = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
      if (!blob) throw new Error(t("annotate.failed"));
      await onAttach(new File([blob], name ?? snapshotName(snapshot.where, new Date()), { type: "image/png" }), ops);
    } finally {
      setBusy(false);
    }
  };

  const plate = labelBox(letters * scale, [0]);
  const key = (code: string) => ` (${letterOf(code)})`;
  return createPortal(
    <div className="annotate" role="dialog" aria-modal="true" aria-label={title ?? t("annotate.title")} data-tool={tool} data-marks={history.ops.length}>
      <div className="annotate-bar">
        <button type="button" className="btn small ghost annotate-cancel" onClick={onCancel}>{t("common.cancel")}</button>
        <span className="annotate-where truncate" title={snapshot.where}>{snapshot.where}</span>
        <div className="annotate-group annotate-history">
          <button type="button" className="iconbtn small annotate-undo" disabled={!history.past.length} onClick={() => dispatch({ kind: "undo" })} aria-label={t("annotate.undo")} title={`${t("annotate.undo")} (Ctrl+Z)`}><Icon name="undo" size={16} /></button>
          <button type="button" className="iconbtn small annotate-redo" disabled={!history.future.length} onClick={() => dispatch({ kind: "redo" })} aria-label={t("annotate.redo")} title={`${t("annotate.redo")} (Ctrl+Shift+Z)`}><Icon name="undo" size={16} /></button>
          <button type="button" className="iconbtn small annotate-clear" disabled={!history.ops.length} onClick={() => dispatch({ kind: "clear" })} aria-label={t("annotate.clear")} title={t("annotate.clear")}><Icon name="trash" size={16} /></button>
        </div>
        <button type="button" className="btn small primary annotate-attach" disabled={busy} onClick={() => void finishEditing()} title={finish === "done" ? `${t("annotate.done")} (Enter)` : undefined}>
          {finish === "attach" && <Icon name="attach" size={14} />}{finish === "done" ? t("annotate.done") : t("annotate.attach")}
        </button>
      </div>
      {snapshot.note && <div className="annotate-note" role="status">{snapshot.note}</div>}
      <div className="annotate-stage" ref={stage}>
        <div className="annotate-sheet" style={{ width: shown.width, height: shown.height }}
          onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={cancelDrag}>
          <div ref={host} className="annotate-host" />
          <canvas ref={overlay} className="annotate-live" aria-hidden />
        </div>
        {typing && (
          <input ref={field} className="annotate-text" value={typing.text} aria-label={t("annotate.text")} placeholder={t("annotate.text")}
            style={{
              left: typing.left, top: typing.top, color: contrastOf(color), background: color, fontSize: letters * scale, lineHeight: `${letters * scale}px`,
              height: letters * scale + plate.pad * 2, padding: plate.pad, borderRadius: plate.radius, fontFamily: TEXT_FONT,
              width: `calc(${Math.max(8, typing.text.length + 2)}ch + ${plate.pad * 2}px)`,
            }}
            onChange={(e) => type({ ...typing, text: e.target.value })}
            onKeyDown={(e) => {
              if (e.key !== "Enter") return;
              e.preventDefault();
              if (e.ctrlKey || e.metaKey) void finishEditing();
              else commitText();
            }}
            onBlur={() => commitText()} />
        )}
      </div>
      <div className="annotate-strip">
        <div className="annotate-group" role="group" aria-label={t("annotate.tools")}>
          {TOOLS.map((name) => (
            <button key={name} type="button" className={`iconbtn small annotate-tool ${tool === name ? "on" : ""}`} data-tool={name} aria-pressed={tool === name}
              aria-label={t(`annotate.tool.${name}`)} title={t(`annotate.tool.${name}`) + key(TOOL_KEYS[name])} onClick={() => choose(name)}>
              <ToolGlyph tool={name} />
            </button>
          ))}
        </div>
        <div className="annotate-group">
          <button type="button" className="iconbtn small annotate-rotate" onClick={rotate} aria-label={t("annotate.rotate")} title={t("annotate.rotate") + key(ROTATE_KEY)}><RotateGlyph /></button>
          <button type="button" className="iconbtn small annotate-weight" data-weight={weight} onClick={() => setWeight((w) => (w + 1) % WEIGHTS.length)}
            aria-label={t(`annotate.weight.${weight}`)} title={`${t(`annotate.weight.${weight}`)} ([ ])`}><WeightGlyph weight={weight} /></button>
        </div>
        <div className="annotate-group" role="radiogroup" aria-label={t("annotate.color")}>
          {SWATCHES.map((swatch, i) => (
            <button key={swatch} type="button" role="radio" aria-checked={color === swatch} className={`annotate-swatch ${color === swatch ? "on" : ""}`}
              style={{ background: swatch }} aria-label={t(`annotate.color.${i}`)} title={`${t(`annotate.color.${i}`)} (${i + 1})`} onClick={() => setColor(swatch)} />
          ))}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function clampPoint(p: Point, width: number, height: number): Point {
  return { x: Math.min(width, Math.max(0, p.x)), y: Math.min(height, Math.max(0, p.y)) };
}

/**
 * The Snapshot button and the editor it opens. `capture` takes the picture when pressed, not before:
 * the live view's frame is the one on screen at the press. Without a conversation to attach to, there
 * is no button, since its one outcome would have nowhere to go.
 */
export function SnapshotButton({ capture, session, toast, className = "iconbtn small", children }: {
  capture: () => Promise<Snapshot | null> | Snapshot | null;
  /** The conversation to attach to, when it is not the panel's own (the browser's page of its own). */
  session?: string | null;
  toast: (text: string) => void;
  className?: string;
  children?: ReactNode;
}) {
  const panelSession = useContext(ComposeTarget);
  const target = panelSession ?? session ?? null;
  const [open, setOpen] = useState<Snapshot | null>(null);
  if (!target) return null;
  const press = async () => {
    try {
      const shot = await capture();
      if (shot) setOpen(shot);
      else toast(t("annotate.nopicture"));
    } catch (e) {
      toast(errorText(e));
    }
  };
  const attach = async (file: File) => {
    try {
      await handToComposer(target, [file], open?.text ?? "");
      setOpen(null);
    } catch (e) {
      toast(errorText(e));
    }
  };
  return (
    <>
      <button type="button" className={`${className} annotate-open`} onClick={() => void press()} aria-label={t("annotate.open")} title={t("annotate.open")}>
        {children ?? <Icon name="camera" size={16} />}
      </button>
      {open && <Annotate snapshot={open} onCancel={() => setOpen(null)} onAttach={attach} />}
    </>
  );
}
