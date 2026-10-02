import { useEffect, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
import type { MediaItem, MediaPresentation } from "./api";
import { api } from "./api";
import { Overlay, useLayer } from "./ui/dialogs";
import { Icon } from "./icons";
import { t } from "./i18n";
export { mediaCopyText, splitMediaAnswer } from "./mediaformat";

// A clip opened from a chat used to start at the browser's full volume. A tenth is loud enough
// to hear and quiet enough that opening an album does not take over the room.
const QUIET_VOLUME = 0.1;
const SEEK_STEP_SECONDS = 5;

function seekBy(el: HTMLVideoElement | null, delta: number) {
  if (!el || el.readyState < 1) return;
  const limit = Number.isFinite(el.duration) ? el.duration : Number.POSITIVE_INFINITY;
  el.currentTime = Math.min(limit, Math.max(0, el.currentTime + delta));
}

function source(sessionId: string, presentationId: string, item: MediaItem): string {
  // A remote item is already a URL. Routing it through the content endpoint would only download
  // it onto this machine, which is what attaching by link is there to avoid.
  if (item.url) return item.url;
  return `/api/sessions/${encodeURIComponent(sessionId)}/media/${encodeURIComponent(presentationId)}/${encodeURIComponent(item.id)}/content`;
}

function clock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor(whole / 60) % 60;
  const rest = whole % 60;
  const padded = String(rest).padStart(2, "0");
  if (hours) return `${hours}:${String(minutes).padStart(2, "0")}:${padded}`;
  return `${minutes}:${padded}`;
}

let accessRequest: Promise<void> | null = null;
function requestMediaAccess(): Promise<void> {
  if (!accessRequest) {
    accessRequest = api.post("/api/media/access").then(() => undefined).catch((error) => {
      accessRequest = null;
      throw error;
    });
  }
  return accessRequest;
}

function VideoPlayer({ src }: { src: string }) {
  const video = useRef<HTMLVideoElement>(null);
  const hide = useRef<number>(0);
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(QUIET_VOLUME);
  const [muted, setMuted] = useState(false);
  const [shown, setShown] = useState(true);

  const reveal = (stay: boolean) => {
    setShown(true);
    window.clearTimeout(hide.current);
    if (!stay) hide.current = window.setTimeout(() => setShown(false), 2200);
  };
  const toggle = () => {
    const el = video.current;
    if (!el) return;
    if (el.paused) void el.play();
    else el.pause();
  };
  useEffect(() => () => window.clearTimeout(hide.current), []);
  // In fullscreen the browser delivers keys to the fullscreen element, not to the dialog
  // underneath, so the arrows never arrived there. This listener is that path only.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const shell = video.current?.parentElement;
      const full = document.fullscreenElement;
      if (!shell || !full || (full !== shell && !shell.contains(full))) return;
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      if ((event.target as HTMLElement | null)?.closest("input.video-player-volume")) return;
      event.preventDefault();
      event.stopPropagation();
      seekBy(video.current, event.key === "ArrowRight" ? SEEK_STEP_SECONDS : -SEEK_STEP_SECONDS);
      setShown(true);
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);
  const onKey = (event: ReactKeyboardEvent) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    if ((event.target as HTMLElement).closest("input.video-player-volume")) return;
    event.preventDefault();
    event.stopPropagation();
    seekBy(video.current, event.key === "ArrowRight" ? SEEK_STEP_SECONDS : -SEEK_STEP_SECONDS);
    setShown(true);
  };
  return <div className={`video-player ${shown || !playing ? "shown" : ""}`} tabIndex={-1} onKeyDown={onKey} onMouseMove={() => reveal(!playing)} onMouseLeave={() => playing && setShown(false)}>
    <video
      ref={video}
      src={src}
      playsInline
      preload="metadata"
      onLoadedMetadata={(event) => {
        event.currentTarget.volume = QUIET_VOLUME;
        setVolume(QUIET_VOLUME);
        setMuted(false);
        setDuration(event.currentTarget.duration || 0);
      }}
      onTimeUpdate={(event) => setTime(event.currentTarget.currentTime)}
      onPlay={() => { setPlaying(true); reveal(false); }}
      onPause={() => { setPlaying(false); setShown(true); }}
      onClick={toggle}
    />
    {!playing && <button type="button" className="video-player-play" onClick={toggle} aria-label={t("media.play")}><Icon name="play" size={28} /></button>}
    <div className="video-player-bar">
      <button type="button" onClick={toggle} aria-label={t(playing ? "media.pause" : "media.play")}><Icon name={playing ? "pause" : "play"} /></button>
      <span className="video-player-time">{clock(time)}/{clock(duration)}</span>
      <input className="video-player-seek" type="range" min={0} max={duration || 0} step={0.1} value={Math.min(time, duration || 0)} aria-label={t("media.seek")} onChange={(event) => {
        const next = Number(event.target.value);
        if (video.current) video.current.currentTime = next;
        setTime(next);
      }} />
      <button type="button" onClick={() => {
        const el = video.current;
        if (!el) return;
        el.muted = !el.muted;
        setMuted(el.muted);
      }} aria-label={t(muted ? "media.unmute" : "media.mute")}><Icon name={muted || volume === 0 ? "mute" : "volume"} /></button>
      <input className="video-player-volume" type="range" min={0} max={1} step={0.05} value={muted ? 0 : volume} aria-label={t("media.volume")} onChange={(event) => {
        const next = Number(event.target.value);
        setVolume(next);
        setMuted(next === 0);
        if (video.current) {
          video.current.volume = next;
          video.current.muted = next === 0;
        }
      }} />
      <button type="button" onClick={() => {
        const shell = video.current?.parentElement;
        if (!shell) return;
        if (document.fullscreenElement) void document.exitFullscreen();
        else void shell.requestFullscreen();
      }} aria-label={t("media.fullscreen")}><Icon name="expand" /></button>
    </div>
  </div>;
}

function MediaElement({ sessionId, presentation, item, onOpen }: { sessionId: string; presentation: MediaPresentation; item: MediaItem; onOpen: () => void }) {
  const src = source(sessionId, presentation.id, item);
  if (item.kind === "image" || item.kind === "animation" || (item.kind === "video" && presentation.layout === "album")) {
    return <figure className="inline-media-image">
      <button type="button" className="inline-media-surface" onClick={onOpen} aria-label={t("media.open", { name: item.alt || item.filename })}>
        {item.kind === "video" ? <><video src={src} muted playsInline preload="metadata" /><span className="inline-media-play"><Icon name="play" /></span></> : <img src={src} alt={item.alt || item.filename} loading="lazy" width={item.width || undefined} height={item.height || undefined} />}
      </button>
      <div className="inline-media-actions">
        <button type="button" onClick={onOpen} aria-label={t("media.open", { name: item.alt || item.filename })}><Icon name="expand" /></button>
        <a href={src} download={item.filename} aria-label={t("common.download")}><Icon name="download" /></a>
      </div>
      {item.caption && <figcaption>{item.caption}</figcaption>}
    </figure>;
  }
  if (item.kind === "video") return <figure className="inline-media-player">
    <VideoPlayer src={src} />
    {item.caption && <figcaption>{item.caption}</figcaption>}
  </figure>;
  return <figure className="inline-media-player audio">
    <div className="inline-media-player-head"><span>{item.caption || item.filename}</span><a href={src} download={item.filename} aria-label={t("common.download")}><Icon name="download" /></a></div>
    <audio controls preload="metadata" src={src} />
  </figure>;
}

function Viewer({ sessionId, presentation, start, onClose }: { sessionId: string; presentation: MediaPresentation; start: number; onClose: () => void }) {
  const [index, setIndex] = useState(start);
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const root = useRef<HTMLDivElement>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const drag = useRef<{ x: number; y: number; ox: number; oy: number; distance?: number } | null>(null);
  const item = presentation.items[index];
  const video = item.kind === "video";
  const name = item.alt || item.filename;
  const src = source(sessionId, presentation.id, item);
  useLayer(onClose);
  useEffect(() => { root.current?.focus(); }, []);
  useEffect(() => { setZoom(1); setOffset({ x: 0, y: 0 }); }, [index]);
  const changeZoom = (next: number) => {
    const bounded = Math.max(1, Math.min(4, next));
    setZoom(bounded);
    if (bounded === 1) setOffset({ x: 0, y: 0 });
  };
  const down = (event: ReactPointerEvent) => {
    if ((event.target as HTMLElement).closest("button, a, input")) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    drag.current = { x: event.clientX, y: event.clientY, ox: offset.x, oy: offset.y };
  };
  const move = (event: ReactPointerEvent) => {
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const points = [...pointers.current.values()];
    if (points.length === 2) {
      const distance = Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
      if (drag.current?.distance) changeZoom(zoom * distance / drag.current.distance);
      if (drag.current) drag.current.distance = distance;
    } else if (drag.current && zoom > 1) {
      setOffset({ x: drag.current.ox + event.clientX - drag.current.x, y: drag.current.oy + event.clientY - drag.current.y });
    }
  };
  const up = (event: ReactPointerEvent) => {
    pointers.current.delete(event.pointerId);
    if (pointers.current.size || !drag.current) return;
    const distance = event.clientX - drag.current.x;
    if (zoom === 1 && Math.abs(distance) > 60) {
      if (distance < 0 && index < presentation.items.length - 1) setIndex((value) => value + 1);
      if (distance > 0 && index > 0) setIndex((value) => value - 1);
    }
    drag.current = null;
  };
  const wheel = (event: ReactWheelEvent) => { event.preventDefault(); changeZoom(zoom * (event.deltaY < 0 ? 1.15 : 0.87)); };
  const onKey = (event: ReactKeyboardEvent) => {
    if ((event.target as HTMLElement).closest("input.video-player-volume")) return;
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    // A video seeks. The arrows turn the page only for a still.
    if (video) {
      event.preventDefault();
      seekBy(root.current?.querySelector("video") ?? null, event.key === "ArrowRight" ? SEEK_STEP_SECONDS : -SEEK_STEP_SECONDS);
      return;
    }
    if (event.key === "ArrowRight" && index < presentation.items.length - 1) setIndex((value) => value + 1);
    if (event.key === "ArrowLeft" && index > 0) setIndex((value) => value - 1);
  };
  return <Overlay>
    <div ref={root} className="media-viewer lightbox" tabIndex={-1} role="dialog" aria-modal="true" aria-label={name} onKeyDown={onKey}>
      <div className={`media-viewer-stage ${video ? "video" : ""}`} onPointerDown={video ? undefined : down} onPointerMove={video ? undefined : move} onPointerUp={video ? undefined : up} onPointerCancel={video ? undefined : up} onWheel={video ? undefined : wheel} onDoubleClick={video ? undefined : () => changeZoom(zoom === 1 ? 2 : 1)}>
        {video ? <VideoPlayer key={item.id} src={src} /> : <img src={src} alt={name} draggable={false} style={{ transform: `translate(${offset.x}px, ${offset.y}px) scale(${zoom})` }} />}
      </div>
      <div className="lightbox-top">
        <span className="lightbox-title">{name}</span>
        <span className="lightbox-count">{index + 1}/{presentation.items.length}</span>
        {!video && zoom !== 1 && <button type="button" onClick={() => { setZoom(1); setOffset({ x: 0, y: 0 }); }} aria-label={t("preview.fit")}><span className="lightbox-zoom">{Math.round(zoom * 100)}%</span></button>}
        <a href={src} download={item.filename} aria-label={t("common.download")}><Icon name="download" /></a>
        <button type="button" onClick={onClose} aria-label={t("common.close")}><Icon name="close" /></button>
      </div>
      {index > 0 && <button type="button" className="lightbox-edge previous" onClick={() => setIndex((value) => value - 1)} aria-label={t("media.previous")}><Icon name="back" /></button>}
      {index < presentation.items.length - 1 && <button type="button" className="lightbox-edge next" onClick={() => setIndex((value) => value + 1)} aria-label={t("media.next")}><Icon name="forward" /></button>}
    </div>
  </Overlay>;
}

function albumRatio(items: MediaItem[]): string | undefined {
  const photo = items.find((item) => (item.kind === "image" || item.kind === "animation") && item.width && item.height);
  return photo?.width && photo.height ? `${photo.width} / ${photo.height}` : undefined;
}

export function InlineMedia({ sessionId, presentation }: { sessionId: string; presentation: MediaPresentation }) {
  const [open, setOpen] = useState<number | null>(null);
  const [access, setAccess] = useState<"loading" | "ready" | "error">("loading");
  const rail = useRef<HTMLDivElement>(null);
  const load = () => {
    setAccess("loading");
    requestMediaAccess().then(() => setAccess("ready")).catch(() => setAccess("error"));
  };
  useEffect(load, []); // eslint-disable-line react-hooks/exhaustive-deps -- one credential upgrade per mount
  if (access === "loading") return <div className="inline-media-loading" aria-label={t("media.loading")} />;
  if (access === "error") return <button type="button" className="inline-media-error" onClick={load}>{t("media.retry")}</button>;
  const ratio = presentation.layout === "album" ? albumRatio(presentation.items) : undefined;
  return <>
    <div className={`inline-media ${presentation.layout} items-${Math.min(4, presentation.items.length)}`} style={ratio ? { "--album-ratio": ratio } as CSSProperties : undefined}>
      <div className="inline-media-track" ref={rail}>
        {presentation.items.map((item, index) => <MediaElement key={item.id} sessionId={sessionId} presentation={presentation} item={item} onOpen={() => setOpen(index)} />)}
      </div>
      {presentation.layout === "album" && presentation.items.length > 3 && <>
        <button type="button" className="inline-media-nav previous" onClick={() => rail.current?.scrollBy({ left: -rail.current.clientWidth * .8, behavior: "smooth" })} aria-label={t("media.previous")}><Icon name="back" /></button>
        <button type="button" className="inline-media-nav next" onClick={() => rail.current?.scrollBy({ left: rail.current.clientWidth * .8, behavior: "smooth" })} aria-label={t("media.next")}><Icon name="forward" /></button>
      </>}
    </div>
    {open !== null && <Viewer sessionId={sessionId} presentation={presentation} start={open} onClose={() => setOpen(null)} />}
  </>;
}
