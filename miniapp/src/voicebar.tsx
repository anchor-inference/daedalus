// The composer's voice note: the microphone, the bar that replaces the field while it records, and
// the strip that stands above the field when a recording could not be transcribed.
//
// While recording, the pill is one bar: ✕ throws the recording away, the waveform scrolls in from the
// right, ■ stops and puts the words in the field to be read over, and ↑ stops and sends at once. The
// words always land after whatever was already typed. See voicenote.ts for the machine and the rule.

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { api, AsrStatus } from "./api";
import { Icon } from "./icons";
import { t } from "./i18n";
import { errorText, haptic } from "./ui";
import { blobToWav } from "./wav";
import { IDLE, NoteState, clock, forgetNote, keepNote, keptNote, noteStep, readRefusal, recordingName } from "./voicenote";

/** How many level samples the bar remembers: more than the widest bar can draw. */
const LEVELS = 400;
/** How often the microphone's level is read, in milliseconds: one bar each time. */
const LEVEL_MS = 70;

export type VoiceNote = {
  state: NoteState;
  supported: boolean;
  startedAt: number;
  levels: React.MutableRefObject<number[]>;
  start: () => void;
  stop: (send: boolean) => void;
  cancel: () => void;
  retry: () => void;
  attach: () => void;
  discard: () => void;
};

type Options = {
  sessionId: string;
  asr: AsrStatus | null | undefined;
  /** The words, marked as a transcript, and whether they go out at once. */
  onWords: (text: string, send: boolean) => void;
  /** The recording as a file in the pill, for when the operator gives up on the words. */
  onAttach: (file: File) => void;
  toast: (text: string) => void;
};

export function useVoiceNote({ sessionId, asr, onWords, onAttach, toast }: Options): VoiceNote {
  const [state, dispatch] = useReducer(noteStep, IDLE);
  const [startedAt, setStartedAt] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const audio = useRef<Blob | null>(null);
  const discardOnStop = useRef(false);
  const abort = useRef<AbortController | null>(null);
  const levels = useRef<number[]>([]);
  // Whether ↑ rather than ■ ended the recording; read when the recorder has delivered its last bytes.
  const sendOnStop = useRef(false);
  const meter = useRef<{ ctx: AudioContext; timer: number } | null>(null);
  const live = useRef({ sessionId, onWords, onAttach, toast, autosend: !!asr?.autosend, maxSeconds: asr?.max_seconds ?? 0 });
  live.current = { sessionId, onWords, onAttach, toast, autosend: !!asr?.autosend, maxSeconds: asr?.max_seconds ?? 0 };
  const supported = typeof MediaRecorder !== "undefined" && !!navigator.mediaDevices?.getUserMedia;

  const stopMeter = useCallback(() => {
    if (!meter.current) return;
    window.clearInterval(meter.current.timer);
    void meter.current.ctx.close().catch(() => undefined);
    meter.current = null;
  }, []);

  const release = useCallback(() => {
    const r = recorder.current;
    recorder.current = null;
    stopMeter();
    if (r) {
      r.stream.getTracks().forEach((track) => track.stop());
      if (r.state !== "inactive") {
        discardOnStop.current = true;
        r.stop();
      }
    }
    abort.current?.abort();
    abort.current = null;
  }, [stopMeter]);

  // A recording belongs to its session: another session starts clean, and finds its own waiting one.
  useEffect(() => {
    let alive = true;
    audio.current = null;
    void keptNote(sessionId).then((note) => {
      if (!alive || !note) return;
      audio.current = note.blob;
      dispatch({ type: "restored", error: note.error || t("voicenote.restored"), kept: note.kept });
    });
    return () => {
      alive = false;
      release();
      dispatch({ type: "cancel" });
      dispatch({ type: "settled" });
    };
  }, [sessionId, release]);

  const transcribe = useCallback(async (blob: Blob, kept: string, send: boolean) => {
    const session = live.current.sessionId;
    const controller = new AbortController();
    abort.current = controller;
    const post = async (name: string) => {
      const form = new FormData();
      if (name) form.append("recording", name);
      else form.append("audio", await blobToWav(blob), "recording.wav");
      return fetch(`/api/sessions/${session}/transcribe`, { method: "POST", headers: api.authHeaders(), body: form, signal: controller.signal });
    };
    try {
      let res = await post(kept);
      // The host lets a kept recording go after a week; this page still has the audio itself.
      if (res.status === 404 && kept) res = await post("");
      if (!res.ok) {
        const refusal = readRefusal(res.status, await res.json().catch(() => null));
        await keepNote(session, { blob, error: refusal.error, kept: refusal.kept, at: Date.now() });
        dispatch({ type: "failed", error: refusal.error, kept: refusal.kept });
        haptic("error");
        return;
      }
      const body = (await res.json()) as { text: string };
      await forgetNote(session);
      audio.current = null;
      dispatch({ type: "heard" });
      live.current.onWords(body.text, send || live.current.autosend);
      haptic("success");
    } catch (e) {
      if (controller.signal.aborted) return;
      const error = errorText(e);
      await keepNote(session, { blob, error, kept, at: Date.now() });
      dispatch({ type: "failed", error });
    } finally {
      if (abort.current === controller) abort.current = null;
    }
  }, []);

  const start = useCallback(async () => {
    if (state.phase !== "idle" || recorder.current) return;
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      live.current.toast(t("voicenote.denied"));
      return;
    }
    const type = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"].find((x) => MediaRecorder.isTypeSupported?.(x));
    const r = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
    const parts: Blob[] = [];
    discardOnStop.current = false;
    r.ondataavailable = (e) => { if (e.data.size) parts.push(e.data); };
    r.onstop = () => {
      stream.getTracks().forEach((track) => track.stop());
      stopMeter();
      if (discardOnStop.current) return;
      const blob = new Blob(parts, { type: r.mimeType || type || "audio/webm" });
      audio.current = blob;
      // Kept before anything is sent: whatever happens to the request, the recording outlives it.
      void keepNote(live.current.sessionId, { blob, error: "", kept: "", at: Date.now() });
      void transcribe(blob, "", sendOnStop.current);
    };
    levels.current = [];
    try {
      const Ctx = window.AudioContext ?? window.webkitAudioContext;
      const ctx = new Ctx();
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const frame = new Float32Array(analyser.fftSize);
      const timer = window.setInterval(() => {
        analyser.getFloatTimeDomainData(frame);
        let sum = 0;
        for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
        const next = levels.current.length >= LEVELS ? levels.current.slice(1) : levels.current;
        next.push(Math.sqrt(sum / frame.length));
        levels.current = next;
      }, LEVEL_MS);
      meter.current = { ctx, timer };
    } catch {
      // No meter is a flat line, not a broken recording.
    }
    recorder.current = r;
    r.start(250);
    setStartedAt(Date.now());
    dispatch({ type: "start" });
    haptic("light");
  }, [state.phase, stopMeter, transcribe]);

  const stop = useCallback((send: boolean) => {
    const r = recorder.current;
    if (!r) return;
    recorder.current = null;
    sendOnStop.current = send;
    dispatch({ type: "stop", send: send || live.current.autosend });
    if (r.state !== "inactive") r.stop();
  }, []);

  // The configured ceiling stops the recording and transcribes it; it used to throw it away.
  useEffect(() => {
    if (state.phase !== "recording" || !live.current.maxSeconds) return;
    const timer = window.setTimeout(() => stop(false), Math.max(0, startedAt + live.current.maxSeconds * 1000 - Date.now()));
    return () => window.clearTimeout(timer);
  }, [state.phase, startedAt, stop]);

  const cancel = useCallback(() => {
    release();
    audio.current = null;
    void forgetNote(live.current.sessionId);
    dispatch({ type: "cancel" });
  }, [release]);

  const retry = useCallback(() => {
    const blob = audio.current;
    if (!blob) return;
    dispatch({ type: "retry", send: false });
    void transcribe(blob, state.kept, false);
  }, [state.kept, transcribe]);

  const letGo = useCallback((kept: string) => {
    audio.current = null;
    void forgetNote(live.current.sessionId);
    if (kept) void api.delete(`/api/sessions/${live.current.sessionId}/transcribe/${encodeURIComponent(kept)}`).catch(() => undefined);
    dispatch({ type: "settled" });
  }, []);

  const attach = useCallback(() => {
    const blob = audio.current;
    if (!blob) return;
    live.current.onAttach(new File([blob], recordingName(blob.type, new Date()), { type: blob.type || "audio/webm" }));
    letGo(state.kept);
  }, [letGo, state.kept]);

  const discard = useCallback(() => letGo(state.kept), [letGo, state.kept]);

  return { state, supported, startedAt, levels, start: () => void start(), stop, cancel, retry, attach, discard };
}

/** The microphone in the composer's row. It is there whether or not something is typed. */
export function MicButton({ note }: { note: VoiceNote }) {
  const busy = note.state.phase === "transcribing";
  return (
    <button type="button" className="iconbtn flat mic" onClick={note.start} disabled={!note.supported || note.state.phase !== "idle"} title={t(!note.supported ? "session.mic.none" : busy ? "session.mic.busy" : "session.mic.title")} aria-label={t("session.mic")}>
      <Icon name="mic" />
    </button>
  );
}

/** The pill while a note records or is being transcribed: ✕, the waveform and the time, ■ and ↑. */
export function VoiceBar({ note }: { note: VoiceNote }) {
  const recording = note.state.phase === "recording";
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!recording) return;
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [recording]);
  const seconds = recording ? (now - note.startedAt) / 1000 : 0;
  return (
    <div className="voicebar" role="group" aria-label={t(recording ? "voicenote.recording" : "voicenote.transcribing")} data-phase={note.state.phase}>
      <button type="button" className="iconbtn flat voicebar-cancel" onClick={note.cancel} aria-label={t("voicenote.cancel")} title={t("voicenote.cancel")}>
        <Icon name="close" />
      </button>
      {recording ? <Waveform levels={note.levels} /> : (
        <div className="voicebar-busy" role="status"><span className="voicebar-spinner" aria-hidden />{t("voicenote.transcribing")}</div>
      )}
      {recording && <span className="voicebar-time" aria-label={t("voicenote.elapsed", { time: clock(seconds) })}>{clock(seconds)}</span>}
      <button type="button" className="roundbtn voicebar-stop" onClick={() => note.stop(false)} disabled={!recording} aria-label={t("voicenote.stop")} title={t("voicenote.stop")}>
        <Icon name="stop" size={16} />
      </button>
      <button type="button" className="roundbtn primary voicebar-send" onClick={() => note.stop(true)} disabled={!recording} aria-label={t("voicenote.send")} title={t("voicenote.send")}>
        <Icon name="up" />
      </button>
    </div>
  );
}

/** Above the field when the words did not come: why, and the three ways on. The recording is kept meanwhile. */
export function VoiceNoteFailed({ note }: { note: VoiceNote }) {
  if (note.state.phase !== "failed") return null;
  return (
    <div className="voicenote-failed" role="alert">
      <Icon name="alert" size={16} />
      <span className="voicenote-failed-text">
        <span className="voicenote-failed-title">{t("voicenote.failed")}</span>
        {note.state.error && <span className="sub voicenote-failed-reason">{note.state.error}</span>}
      </span>
      <span className="voicenote-failed-actions">
        <button type="button" className="btn small primary" onClick={note.retry}>{t("voicenote.retry")}</button>
        <button type="button" className="btn small" onClick={note.attach}>{t("voicenote.attach")}</button>
        <button type="button" className="iconbtn flat small" onClick={note.discard} aria-label={t("voicenote.discard")} title={t("voicenote.discard")}>
          <Icon name="trash" size={15} />
        </button>
      </span>
    </div>
  );
}

/** The microphone's level over the last seconds, newest at the right, quiet as a dotted baseline. */
function Waveform({ levels }: { levels: React.MutableRefObject<number[]> }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const ink = getComputedStyle(el).color;
    let frame = 0;
    const draw = () => {
      const ratio = window.devicePixelRatio || 1;
      const width = el.clientWidth;
      const height = el.clientHeight;
      if (el.width !== Math.round(width * ratio) || el.height !== Math.round(height * ratio)) {
        el.width = Math.round(width * ratio);
        el.height = Math.round(height * ratio);
      }
      const g = el.getContext("2d");
      if (!g) return;
      g.setTransform(ratio, 0, 0, ratio, 0, 0);
      g.clearRect(0, 0, width, height);
      g.fillStyle = ink;
      const step = 4;
      const mid = height / 2;
      const values = levels.current;
      const count = Math.floor(width / step);
      for (let i = 0; i < count; i++) {
        // With reduced motion nothing scrolls: the line stays a quiet baseline and the clock moves.
        const level = still ? 0 : values[values.length - count + i] ?? -1;
        const x = i * step;
        if (level < 0.01) {
          if (level >= 0 || i % 2 === 0) g.fillRect(x, mid - 1, 2, 2);
          continue;
        }
        const bar = Math.max(2, Math.min(height, Math.sqrt(level) * height * 1.6));
        g.fillRect(x, mid - bar / 2, 2, bar);
      }
      if (!still) frame = requestAnimationFrame(draw);
    };
    draw();
    const redraw = still ? window.setInterval(draw, 1000) : 0;
    return () => {
      cancelAnimationFrame(frame);
      window.clearInterval(redraw);
    };
  }, [levels]);
  return <canvas ref={canvas} className="voicebar-wave" aria-hidden />;
}
