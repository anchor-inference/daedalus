// The recording played back: a keyframe the daemon took, over the live picture, with the element the
// logged action named framed on it, and a scrubber over every keyframe of the group.
//
// The frame is the page as it was right after the action (or when the page changed between actions),
// with its secret fields masked when it was taken. The box and point come from the action log, in the
// page's CSS pixels, and the picture is the viewport scaled to at most 1280 pixels, so one ratio —
// the picture's width over the viewport's — places them. The live view keeps running underneath:
// "Back to live" only lifts this layer.

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, type BrowserActionRow, type BrowserFrame } from "../api";
import { clock } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { fit } from "./geometry";
import { actionWords } from "./model";

/** The keyframe of a row of the action log: the one its action left, when the recording has it. */
export function frameOfRow(frames: BrowserFrame[], row: Pick<BrowserActionRow, "id" | "action_id">): number {
  const id = row.action_id || row.id;
  // The newest match: an action id belongs to one daemon's run, and a recording can outlive a run.
  if (!id) return -1;
  for (let i = frames.length - 1; i >= 0; i--) if (frames[i].kind === "action" && frames[i].action_id === id) return i;
  return -1;
}

/**
 * The CSS width the keyframe was taken at. New frames say so (`vw`). A frame without it was taken
 * before the page followed the pane: the picture is at most 1280 px of a page that was itself at
 * most 1280 CSS pixels wide, so the picture's width is the page. The group's size now would slide
 * those boxes once the pane is resized.
 */
export function frameCssWidth(frame: Pick<BrowserFrame, "vw" | "w">, current: number): number {
  if (frame.vw) return frame.vw;
  if (frame.w > 0 && frame.w <= 1280) return frame.w;
  return current;
}

/** Where the logged box sits on a picture drawn at `rect`, for a page `viewportW` CSS pixels wide. */
export function placeBox(box: { x: number; y: number; w: number; h: number }, rect: { x: number; y: number; w: number }, viewportW: number): { x: number; y: number; w: number; h: number } {
  const scale = viewportW > 0 ? rect.w / viewportW : 0;
  return { x: rect.x + box.x * scale, y: rect.y + box.y * scale, w: box.w * scale, h: box.h * scale };
}

export function ReplayStage({ group, viewportW, frames, index, rows, agent, onIndex, onLive, phone = false }: {
  group: string;
  viewportW: number;
  frames: BrowserFrame[];
  index: number;
  rows: BrowserActionRow[];
  agent: string;
  onIndex: (index: number) => void;
  onLive: () => void;
  /** A phone: the step as a page of its own in the sheet, with its words and previous / next under
   *  the picture instead of a scrubber over it. */
  phone?: boolean;
}) {
  const frame = frames[Math.max(0, Math.min(index, frames.length - 1))];
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const cache = useRef(new Map<number, string>());

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const kept = cache.current;
    return () => {
      for (const u of kept.values()) URL.revokeObjectURL(u);
      kept.clear();
    };
  }, []);

  useEffect(() => {
    if (!frame) return;
    let gone = false;
    const known = cache.current.get(frame.no);
    if (known) {
      setUrl(known);
      setFailed(false);
      return;
    }
    api.browserFrame(group, frame.no).then(
      (u) => {
        if (gone) return URL.revokeObjectURL(u);
        cache.current.set(frame.no, u);
        setUrl(u);
        setFailed(false);
      },
      () => !gone && setFailed(true),
    );
    return () => {
      gone = true;
    };
  }, [group, frame]);

  // ← and → step through the keyframes; Escape goes back to live.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement && e.target.type !== "range") return;
      if (e.key === "ArrowLeft" && index > 0) onIndex(index - 1);
      else if (e.key === "ArrowRight" && index < frames.length - 1) onIndex(index + 1);
      else if (e.key === "Escape") onLive();
      else return;
      e.preventDefault();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [index, frames.length, onIndex, onLive]);

  const row = useMemo(() => (frame?.action_id ? rows.find((r) => (r.action_id || r.id) === frame.action_id) ?? null : null), [frame, rows]);
  const rect = frame ? fit(size.w, size.h, frame.w, frame.h) : { x: 0, y: 0, w: 0, h: 0 };
  const cssW = frame ? frameCssWidth(frame, viewportW) : viewportW;
  const placed = row?.box && rect.w ? placeBox(row.box, rect, cssW) : null;
  const point = row?.point && rect.w ? placeBox({ ...row.point, w: 0, h: 0 }, rect, cssW) : null;
  if (!frame) return null;
  const words = row ? actionWords(row) : null;
  const picture = (
    <div className="bp-replay-stage" ref={box} style={phone ? { aspectRatio: `${frame.w} / ${frame.h}` } : undefined}>
      {url && <img className="bp-replay-img" src={url} alt="" style={{ left: rect.x, top: rect.y, width: rect.w, height: rect.h }} draggable={false} />}
      {failed && <div className="bp-replay-gone sub">{t("browser.replay.gone")}</div>}
      {placed && (
        <div className="bp-replay-box" style={{ left: placed.x, top: placed.y, width: placed.w, height: placed.h }}>
          {words && !phone && <span className="bp-replay-label truncate">{t(words.key, { ...words.vars, name: agent })}</span>}
        </div>
      )}
      {point && <i className="bp-replay-point" style={{ left: point.x, top: point.y }} aria-hidden="true" />}
    </div>
  );
  if (phone) {
    return (
      <div className="bp-replay phone" data-no={frame.no} role="region" aria-label={t("browser.replay")}>
        <div className="bp-replay-phead">
          <button type="button" className="bp-replay-live" onClick={onLive} aria-label={t("browser.replay.live")} title={t("browser.replay.live")}><Icon name="back" size={22} /></button>
          <span className="bp-replay-ptitle">
            <b>{t("browser.ph.step", { n: index + 1, of: frames.length })}</b>
            <span>{t("browser.ph.step.sub", { time: clock(new Date(frame.at).toISOString()) })}</span>
          </span>
        </div>
        {picture}
        {words && <p className="bp-replay-words">{t(words.key, { ...words.vars, name: agent })}</p>}
        <div className="bp-replay-nav">
          <button type="button" disabled={index <= 0} onClick={() => onIndex(index - 1)}><Icon name="back" size={18} />{t("browser.ph.step.n", { n: Math.max(1, index) })}</button>
          <button type="button" disabled={index >= frames.length - 1} onClick={() => onIndex(index + 1)}>{t("browser.ph.step.n", { n: Math.min(frames.length, index + 2) })}<Icon name="forward" size={18} /></button>
        </div>
      </div>
    );
  }
  return (
    <div className="bp-replay" data-no={frame.no} role="region" aria-label={t("browser.replay")}>
      <div className="bp-replay-bar">
        <Icon name="image" size={14} />
        <span className="bp-replay-when">{t("browser.replay.at", { time: clock(new Date(frame.at).toISOString()), n: index + 1, of: frames.length })}</span>
        <input
          className="bp-replay-scrub"
          type="range"
          min={0}
          max={Math.max(0, frames.length - 1)}
          step={1}
          value={index}
          aria-label={t("browser.replay.scrub")}
          onChange={(e) => onIndex(Number(e.target.value))}
        />
        <button type="button" className="btn small bp-replay-live" onClick={onLive}>
          <Icon name="play" size={12} />
          {t("browser.replay.live")}
        </button>
      </div>
      {picture}
    </div>
  );
}
