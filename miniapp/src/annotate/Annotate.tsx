// The annotated snapshot: a picture of the page over the whole window, a few tools to point at what is
// wrong on it, and Attach, which hands the picture with its marks to the conversation's composer.
//
// One canvas at the picture's natural size holds the picture and the marks, scaled by CSS into the
// window. Drawing in the picture's own pixels means the attached file is simply this canvas: no second
// rendering at another scale, and no mark that drifts between what was seen and what was sent.

import { useContext, useEffect, useLayoutEffect, useReducer, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useLayer } from "../ui/dialogs";
import { Icon } from "../icons";
import { t } from "../i18n";
import { errorText } from "../ui";
import { EMPTY_BOARD, SWATCHES, boardReducer, drawMark, fitBox, snapshotName, strokeWidth, textSize, toPicture, type Mark, type Point, type Tool } from "./draw";
import { ComposeTarget, handToComposer } from "./compose";
import type { Snapshot } from "./source";
import "./annotate.css";

const TOOLS: Tool[] = ["arrow", "box", "pen", "text"];

/** The tools' glyphs: the icon set has a pen but no arrow, box or letter, and these are four strokes. */
function ToolGlyph({ tool }: { tool: Tool }) {
  if (tool === "pen") return <Icon name="pen" size={16} />;
  const path = tool === "arrow" ? "M4 12 L12 4 M6.5 4 H12 V9.5" : tool === "box" ? "M3 4.5 H13 V11.5 H3 Z" : "M4 4 H12 M8 4 V13";
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d={path} /></svg>;
}

type Typing = { at: Point; left: number; top: number; text: string };

export function Annotate({ snapshot, onCancel, onAttach }: { snapshot: Snapshot; onCancel: () => void; onAttach: (file: File) => Promise<void> | void }) {
  const { width, height } = snapshot;
  const canvas = useRef<HTMLCanvasElement>(null);
  const stage = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const [board, dispatch] = useReducer(boardReducer, EMPTY_BOARD);
  const [tool, setTool] = useState<Tool>("arrow");
  const [color, setColor] = useState<string>(SWATCHES[0]);
  const [drawing, setDrawing] = useState<Mark | null>(null);
  const [typing, setTyping] = useState<Typing | null>(null);
  const [shown, setShown] = useState({ width: 0, height: 0 });
  const [busy, setBusy] = useState(false);
  // Lines and letters are never thinner on screen than a pen's stroke: a desktop frame shown at a third
  // of its size on a phone drew hairlines, and what is drawn is what is sent, so the file has them too.
  const scale = shown.width && width ? shown.width / width : 1;
  const line = Math.max(strokeWidth(width, height), Math.round(2.5 / scale));
  const letters = Math.max(textSize(width, height), Math.round(15 / scale));

  // The words being typed, read by whichever of Enter, a blur, a stroke, a tool change or Attach comes
  // first, and taken by it: a click on the picture both blurs the field and starts a stroke, and the
  // two would otherwise add the same words twice. Losing them to a stray click is worse still, which is
  // why everything that moves on commits rather than drops them.
  const pending = useRef<Typing | null>(null);
  const type = (next: Typing | null) => {
    pending.current = next;
    setTyping(next);
  };
  const textMark = (current: Typing): Mark => ({ tool: "text", color, size: letters, at: current.at, text: current.text.trim() });
  const commitText = (): Mark | null => {
    const current = pending.current;
    type(null);
    if (!current || !current.text.trim()) return null;
    const mark = textMark(current);
    dispatch({ kind: "add", mark });
    return mark;
  };

  // Escape inside the text field drops the words rather than closing the overlay.
  useLayer(() => {
    if (pending.current) type(null);
    else onCancel();
  });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey || e.key.toLowerCase() !== "z" || e.shiftKey) return;
      // Inside the text field, Ctrl+Z undoes the typing, as it does in every field.
      if (e.target instanceof HTMLInputElement) return;
      e.preventDefault();
      dispatch({ kind: "undo" });
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // The picture is as large as the stage allows at its own proportions; the stage follows the window.
  useLayoutEffect(() => {
    const el = stage.current;
    if (!el) return;
    const measure = () => setShown(fitBox(width, height, el.clientWidth, el.clientHeight));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, [width, height]);

  useEffect(() => {
    const ctx = canvas.current?.getContext("2d");
    if (!ctx) return;
    paint(ctx, snapshot, drawing ? [...board.marks, drawing] : board.marks);
  }, [snapshot, board.marks, drawing]);

  const typingOpen = typing !== null;
  useEffect(() => {
    if (typingOpen) field.current?.focus();
  }, [typingOpen]);

  const pointAt = (e: React.PointerEvent) => toPicture(e.clientX, e.clientY, canvas.current!.getBoundingClientRect(), width, height);

  const down = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const at = pointAt(e);
    commitText();
    if (tool === "text") {
      const box = stage.current!.getBoundingClientRect();
      type({ at, left: e.clientX - box.left, top: e.clientY - box.top, text: "" });
      return;
    }
    e.currentTarget.setPointerCapture(e.pointerId);
    setDrawing(tool === "pen" ? { tool, color, width: line, points: [at] } : { tool, color, width: line, from: at, to: at });
  };
  const move = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!drawing) return;
    const at = pointAt(e);
    setDrawing((d) => !d ? d : d.tool === "pen" ? { ...d, points: [...d.points, at] } : d.tool === "text" ? d : { ...d, to: at });
  };
  const up = () => {
    if (!drawing) return;
    dispatch({ kind: "add", mark: drawing });
    setDrawing(null);
  };

  const attach = async () => {
    const typed = commitText();
    const marks = typed ? [...board.marks, typed] : board.marks;
    setBusy(true);
    try {
      const out = document.createElement("canvas");
      out.width = width;
      out.height = height;
      const ctx = out.getContext("2d");
      if (!ctx) throw new Error(t("annotate.failed"));
      paint(ctx, snapshot, marks);
      const blob = await new Promise<Blob | null>((resolve) => out.toBlob(resolve, "image/png"));
      if (!blob) throw new Error(t("annotate.failed"));
      await onAttach(new File([blob], snapshotName(snapshot.where, new Date()), { type: "image/png" }));
    } finally {
      setBusy(false);
    }
  };

  return createPortal(
    <div className="annotate" role="dialog" aria-modal="true" aria-label={t("annotate.title")} data-tool={tool} data-marks={board.marks.length}>
      <div className="annotate-bar">
        <div className="annotate-group" role="group" aria-label={t("annotate.tools")}>
          {TOOLS.map((name) => (
            <button key={name} type="button" className={`iconbtn small annotate-tool ${tool === name ? "on" : ""}`} data-tool={name} aria-pressed={tool === name}
              aria-label={t(`annotate.tool.${name}`)} title={t(`annotate.tool.${name}`)} onClick={() => { commitText(); setTool(name); }}>
              <ToolGlyph tool={name} />
            </button>
          ))}
        </div>
        <div className="annotate-group" role="radiogroup" aria-label={t("annotate.color")}>
          {SWATCHES.map((swatch, i) => (
            <button key={swatch} type="button" role="radio" aria-checked={color === swatch} className={`annotate-swatch ${color === swatch ? "on" : ""}`}
              style={{ background: swatch }} aria-label={t(`annotate.color.${i}`)} title={t(`annotate.color.${i}`)} onClick={() => setColor(swatch)} />
          ))}
        </div>
        <div className="annotate-group">
          <button type="button" className="iconbtn small annotate-undo" disabled={!board.past.length} onClick={() => dispatch({ kind: "undo" })} aria-label={t("annotate.undo")} title={`${t("annotate.undo")} (Ctrl+Z)`}><Icon name="undo" size={16} /></button>
          <button type="button" className="iconbtn small annotate-clear" disabled={!board.marks.length} onClick={() => dispatch({ kind: "clear" })} aria-label={t("annotate.clear")} title={t("annotate.clear")}><Icon name="trash" size={16} /></button>
        </div>
        <span className="annotate-where truncate" title={snapshot.where}>{snapshot.where}</span>
        <div className="annotate-group annotate-end">
          <button type="button" className="btn small ghost annotate-cancel" onClick={onCancel}>{t("common.cancel")}</button>
          <button type="button" className="btn small primary annotate-attach" disabled={busy} onClick={() => void attach()}><Icon name="attach" size={14} />{t("annotate.attach")}</button>
        </div>
      </div>
      {snapshot.note && <div className="annotate-note" role="status">{snapshot.note}</div>}
      <div className="annotate-stage" ref={stage}>
        <canvas ref={canvas} className="annotate-canvas" width={width} height={height} style={{ width: shown.width, height: shown.height }}
          onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={() => setDrawing(null)} />
        {typing && (
          <input ref={field} className="annotate-text" value={typing.text} aria-label={t("annotate.text")} placeholder={t("annotate.text")}
            style={{ left: typing.left, top: typing.top, color, fontSize: Math.max(12, letters * scale) }}
            onChange={(e) => type({ ...typing, text: e.target.value })}
            onKeyDown={(e) => {
              if (e.key === "Enter") { e.preventDefault(); commitText(); }
            }}
            onBlur={() => commitText()} />
        )}
      </div>
    </div>,
    document.body,
  );
}

function paint(ctx: CanvasRenderingContext2D, snapshot: Snapshot, marks: Mark[]): void {
  ctx.clearRect(0, 0, snapshot.width, snapshot.height);
  if (snapshot.image) ctx.drawImage(snapshot.image, 0, 0, snapshot.width, snapshot.height);
  else {
    // A blank sheet is white whatever the theme: it stands for a page, and most pages are light.
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, snapshot.width, snapshot.height);
  }
  for (const mark of marks) drawMark(ctx, mark);
}

/**
 * The Snapshot button and the overlay it opens. `capture` takes the picture when pressed, not before:
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
      const where = await handToComposer(target, [file], open?.text ?? "");
      setOpen(null);
      toast(t(where === "composer" ? "annotate.attached" : "annotate.saved"));
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
