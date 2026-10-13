// The browser half of the voice page: hearing the operator and speaking the answer.
//
// Both ends have two implementations. Speech in is the browser's own streaming recognition where it
// exists (Chrome, Edge, Safari) and a recorder with a silence detector where it does not (Firefox),
// whose utterances the server transcribes. Speech out is the configured /audio/speech endpoint where
// there is one, and the browser's synthesiser otherwise. The page picks; the screen only sees words.

import { api } from "./api";
import { t } from "./i18n";
import { blobToWav } from "./wav";

type RecognitionEvent = { resultIndex: number; results: { isFinal: boolean; 0: { transcript: string } }[] };
type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start: () => void;
  stop: () => void;
  abort: () => void;
  onresult: ((e: RecognitionEvent) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  onspeechstart: (() => void) | null;
};

declare global {
  interface Window {
    SpeechRecognition?: new () => Recognition;
    webkitSpeechRecognition?: new () => Recognition;
    webkitAudioContext?: typeof AudioContext;
  }
}

/** The language the operator is spoken to in: what the app was opened in, or what the browser is set to. */
export function voiceLang(): string {
  const tg = window.Telegram?.WebApp?.initDataUnsafe?.user?.language_code;
  const raw = tg || navigator.language || "en";
  return raw.includes("-") ? raw : raw === "ru" ? "ru-RU" : raw === "en" ? "en-US" : raw;
}

export function recognitionSupported(): boolean {
  return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
}

export function recorderSupported(): boolean {
  return typeof MediaRecorder !== "undefined" && !!navigator.mediaDevices?.getUserMedia;
}

export type Listener = {
  start: () => Promise<void>;
  stop: () => void;
  kind: "recognition" | "recorder" | "local";
};

export type ListenerHandlers = {
  onInterim: (text: string) => void;
  onFinal: (text: string) => void;
  /** Speech has begun. The level is passed where the listener measures one; a recogniser's own
   *  voice activity detector reports nothing but the fact, and passes nothing. */
  onSpeechStart: (level?: number) => void;
  onError: (message: string) => void;
  /** The microphone's loudness, 0 to about 1, as often as the listener has it. Drives the orb. */
  onLevel?: (level: number) => void;
};

/** Streaming recognition: words arrive while they are being said, and a final result is one utterance. */
export function createRecognition(lang: string, h: ListenerHandlers): Listener {
  const Ctor = window.SpeechRecognition ?? window.webkitSpeechRecognition;
  let engine: Recognition | null = null;
  let wanted = false;
  const build = (): Recognition => {
    const r = new Ctor!();
    r.lang = lang;
    r.continuous = true;
    r.interimResults = true;
    r.onspeechstart = () => h.onSpeechStart();
    r.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const result = e.results[i];
        const text = result[0]?.transcript ?? "";
        if (result.isFinal) {
          const final = text.trim();
          if (final) h.onFinal(final);
        } else interim += text;
      }
      h.onInterim(interim.trim());
    };
    r.onerror = (e) => {
      // "no-speech" and "aborted" are the engine idling, not a failure worth telling the operator about.
      if (e.error !== "no-speech" && e.error !== "aborted") h.onError(e.error === "not-allowed" ? t("voice.error.mic") : e.error);
    };
    // Chrome ends the session on its own after a pause; while the operator wants the mic on, start it again.
    r.onend = () => {
      engine = null;
      if (wanted) window.setTimeout(() => wanted && void start(), 250);
    };
    return r;
  };
  const start = async () => {
    wanted = true;
    if (engine) return;
    engine = build();
    try {
      engine.start();
    } catch {
      /* already started: the onend restart raced with a tap */
    }
  };
  return {
    kind: "recognition",
    start,
    stop: () => {
      wanted = false;
      try {
        engine?.stop();
      } catch {
        /* never started */
      }
      engine = null;
    },
  };
}

const SILENCE_LEVEL = 0.012;
const SILENCE_MS = 800;
const MIN_UTTERANCE_MS = 400;
const MAX_UTTERANCE_MS = 60_000;

/**
 * No streaming recognition here: record, watch the level, and cut an utterance when the operator has
 * been quiet for a moment. Each cut is posted to the server, which transcribes it with the configured
 * endpoint. Coarser than recognition — there are no interim words — but it is the whole of Firefox.
 */
export function createRecorder(h: ListenerHandlers & { onUtterance: (blob: Blob) => void }): Listener {
  let stream: MediaStream | null = null;
  let recorder: MediaRecorder | null = null;
  let context: AudioContext | null = null;
  let timer: number | undefined;
  let chunks: Blob[] = [];
  let speaking = false;
  let quietSince = 0;
  let startedAt = 0;

  const cut = () => {
    if (!recorder || recorder.state !== "recording") return;
    recorder.stop();
  };

  const start = async () => {
    if (stream) return;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch {
      h.onError(t("voice.error.mic"));
      return;
    }
    const Ctx = window.AudioContext ?? window.webkitAudioContext!;
    context = new Ctx();
    const source = context.createMediaStreamSource(stream);
    const analyser = context.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    const samples = new Float32Array(analyser.fftSize);
    const begin = () => {
      if (!stream) return;
      chunks = [];
      speaking = false;
      startedAt = Date.now();
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      recorder.onstop = () => {
        const blob = new Blob(chunks, { type: recorder?.mimeType || "audio/webm" });
        const long = Date.now() - startedAt > MIN_UTTERANCE_MS;
        if (speaking && long && blob.size > 2000) h.onUtterance(blob);
        if (stream) begin();
      };
      recorder.start(250);
    };
    begin();
    timer = window.setInterval(() => {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const v of samples) sum += v * v;
      const level = Math.sqrt(sum / samples.length);
      h.onLevel?.(level);
      const now = Date.now();
      if (level > SILENCE_LEVEL) {
        if (!speaking) h.onSpeechStart(level);
        speaking = true;
        quietSince = 0;
        h.onInterim("…");
        if (now - startedAt > MAX_UTTERANCE_MS) cut();
        return;
      }
      if (!speaking) return;
      if (!quietSince) quietSince = now;
      else if (now - quietSince > SILENCE_MS) {
        h.onInterim("");
        cut();
      }
    }, 100);
  };

  return {
    kind: "recorder",
    start,
    stop: () => {
      window.clearInterval(timer);
      const live = stream;
      stream = null;
      try {
        recorder?.state === "recording" && recorder.stop();
      } catch {
        /* already stopped */
      }
      recorder = null;
      live?.getTracks().forEach((t) => t.stop());
      void context?.close();
      context = null;
    },
  };
}

/** How long after the answer starts playing a speech start is taken to be the speaker, not a person.
 *
 *  A laptop plays the answer into its own microphone, and the first thing a recogniser hears after
 *  the audio begins is almost always that. */
export const ECHO_GUARD_MS = 400;

/** The loudness a listener that measures one must reach to count as somebody talking over the answer.
 *
 *  Well above what a laptop speaker comes back at through echo cancellation, and well below ordinary
 *  speech at arm's length. */
export const BARGE_LEVEL = 0.06;

/**
 * Whether speech starting right now is the operator talking over the answer, or the answer itself.
 *
 * The page used to answer this by closing its ears entirely while speaking, which is the one moment
 * barging in matters, so nothing was ever interrupted. What is kept from that is the reason it was
 * there — the speaker is heard by the microphone — and it is answered by the two things that actually
 * tell the two apart: the first moments after playback begins are the speaker, and after that a
 * person is louder at the microphone than a speaker is through echo cancellation. A listener that
 * measured no level has a voice activity detector of its own and is taken at its word.
 */
export function shouldBargeIn(state: { speaking: boolean; playingForMs: number; level?: number }): boolean {
  if (!state.speaking) return false;
  if (state.playingForMs < ECHO_GUARD_MS) return false;
  return state.level === undefined || state.level >= BARGE_LEVEL;
}

export type Speaker = {
  /**
   * One sentence of one answer, spoken after everything already queued *for that answer*.
   *
   * The turn is the run the sentence belongs to, as the server named it. A sentence of a newer turn
   * ends the older one where it stands rather than queueing behind it: what was still waiting is
   * marked unspoken and what was playing is stopped. Without that rule a voice that took fifteen
   * seconds to warm up read the previous answer and the current one back to back, both of them late,
   * which is exactly what was reported.
   */
  say: (text: string, turn?: string) => void;
  /** A new answer is coming. Ends the previous one now, before its first sentence exists. */
  beginTurn: (turn: string) => void;
  /** Barge-in: drop what is queued and stop what is playing. */
  cancel: () => void;
  /** A browser that plays nothing a tap did not start: call this from a tap. */
  unlock: () => void;
  stop: () => void;
};

/**
 * How the page knows a sentence is being said, when nothing tells it.
 *
 * The browser's own synthesiser is a queue with no clock on it. `speak()` returns nothing and may
 * decide, for reasons the page cannot see, to say nothing at all: the voices have not finished
 * loading, the tab is in the background, the engine wants a tap it has not had, or the queue is
 * holding an utterance from before that will never end. There is no error in any of those cases —
 * `onend` simply never arrives, and a queue that waits for it waits for good. This is what happened
 * here: an answer was heard, the one after it was not, and the page sat in "speaking" with the
 * microphone deafened until it was reloaded.
 *
 * So nothing on this page waits on a synthesiser's word any more. Every utterance is given a budget
 * made of these, and an utterance that outlives its budget is abandoned, the sentence is shown as
 * written rather than spoken, and the queue moves on. Late is not better than never here — a sentence
 * read out thirty seconds after the question is worse than one the operator simply reads.
 */
/** An unhurried reading pace, deliberately slower than any engine's, so a budget never cuts speech off. */
export const SPEECH_CHARS_PER_SECOND = 11;
/** How long `speak()` has to produce a sound before the page decides nothing is coming. */
export const SPEECH_START_MS = 1800;
/** Slack over the estimate, for an engine that pauses for breath or reads a number out in full. */
export const SPEECH_MARGIN_MS = 5000;
/** How long the first sentence waits for `getVoices()` to fill; past it, the engine's default voice reads. */
export const VOICES_WAIT_MS = 1500;
/** Chromium stops a synthesiser it thinks has run too long; a periodic `resume()` is the cure. */
export const RESUME_PULSE_MS = 10000;
/** How long a clip may play without its position moving before it counts as stalled rather than slow. */
export const CLIP_STALL_MS = 6000;

/** How long one utterance is allowed to take, from the moment it is handed over to the last word. */
export function speechBudgetMs(text: string): number {
  return SPEECH_START_MS + (text.length / SPEECH_CHARS_PER_SECOND) * 1000 + SPEECH_MARGIN_MS;
}

/** The little of `window.speechSynthesis` this page uses, so a test can supply one that misbehaves. */
export type Synthesiser = {
  getVoices: () => SpeechSynthesisVoice[];
  speak: (utterance: SpeechSynthesisUtterance) => void;
  cancel: () => void;
  resume: () => void;
  speaking?: boolean;
  pending?: boolean;
  paused?: boolean;
  addEventListener?: (name: string, fn: () => void) => void;
  removeEventListener?: (name: string, fn: () => void) => void;
  onvoiceschanged?: (() => void) | null;
};

/**
 * The voices, once there are any, or nothing after the wait.
 *
 * `getVoices()` is empty on the first call in every Chromium and in a fresh WKWebView: the list is
 * filled asynchronously and announced with `voiceschanged`. A page that reads it once at the first
 * sentence picks no voice for the whole conversation, and on the engines that treat a null voice as
 * "not ready" says nothing at all. Waiting is cheap and happens once; waiting forever is not an
 * option, because an engine with no voices to report never fires the event either.
 */
export function whenVoicesReady(synth: Synthesiser, waitMs: number): Promise<SpeechSynthesisVoice[]> {
  const have = synth.getVoices();
  if (have.length) return Promise.resolve(have);
  return new Promise((resolve) => {
    let settled = false;
    const done = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      synth.removeEventListener?.("voiceschanged", changed);
      if (synth.onvoiceschanged === changed) synth.onvoiceschanged = null;
      resolve(synth.getVoices());
    };
    const changed = () => {
      if (synth.getVoices().length) done();
    };
    const timer = setTimeout(done, waitMs);
    if (synth.addEventListener) synth.addEventListener("voiceschanged", changed);
    else synth.onvoiceschanged = changed;
  });
}

/** The voice to read a language in: the first one that speaks it, or none and the engine's default. */
export function voiceFor(voices: SpeechSynthesisVoice[], lang: string): SpeechSynthesisVoice | undefined {
  const want = lang.slice(0, 2).toLowerCase();
  return voices.find((v) => v.lang.toLowerCase().startsWith(want));
}

/**
 * How loud the answer is while it is being spoken, for the orb to ripple with.
 *
 * Two sources, because there are two speakers. Server audio is a real signal and is measured: the
 * `<audio>` element is routed through an analyser, once — a media element can only be given to one
 * `MediaElementAudioSourceNode` ever, and a second attempt throws and takes the audio with it. The
 * browser's own synthesiser exposes nothing at all: no node, no level, not even a boundary event in
 * every engine. So that half is a cadence rather than a measurement, and it is written here as one
 * honestly: a slow wave with a faster one over it, which reads as speech without claiming to be it.
 */
function createSpeechLevel(audio: HTMLAudioElement, opts: { onLevel?: (level: number) => void }): { start: (measured: boolean) => void; stop: () => void } {
  let context: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  let samples = new Float32Array(0);
  let frame = 0;
  let began = 0;

  let measuring = false;

  const attach = () => {
    if (analyser) return;
    try {
      const Ctx = window.AudioContext ?? window.webkitAudioContext!;
      context = new Ctx();
      analyser = context.createAnalyser();
      analyser.fftSize = 512;
      samples = new Float32Array(analyser.fftSize);
      const source = context.createMediaElementSource(audio);
      source.connect(analyser);
      // The element is no longer heard directly once it has a source node, so the graph has to carry
      // it to the output itself. Without this line the page goes silent and nothing says why.
      analyser.connect(context.destination);
    } catch {
      analyser = null; // No analyser here: the cadence below stands in, and the audio is untouched.
    }
  };

  const tick = () => {
    if (measuring && analyser && samples.length) {
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const v of samples) sum += v * v;
      opts.onLevel?.(Math.sqrt(sum / samples.length));
    } else {
      const t = (performance.now() - began) / 1000;
      opts.onLevel?.(0.12 + 0.06 * Math.sin(t * 7.3) + 0.05 * Math.sin(t * 2.1));
    }
    frame = requestAnimationFrame(tick);
  };

  return {
    // Whether there is anything to measure is decided per answer rather than once: a turn the
    // browser's own synthesiser reads has no audio element in it at all, and the same page may read
    // the next one from a clip off the server. A media element can be given to exactly one source
    // node ever, so the node is made on the first answer that needs it and kept.
    start: (measured: boolean) => {
      measuring = measured;
      if (frame) return;
      if (measured) attach();
      void context?.resume().catch(() => undefined);
      began = performance.now();
      frame = requestAnimationFrame(tick);
    },
    stop: () => {
      cancelAnimationFrame(frame);
      frame = 0;
      opts.onLevel?.(0);
    },
  };
}

/** What a local voice answers with: a run of length-prefixed clips, one per sentence. */
const TURNS_REMEMBERED = 32;
/** How many finished answers are remembered as abandoned. Enough that no sentence of an answer still
 *  being fetched can outlive the memory of it, and small enough that a whole conversation is not
 *  kept: a run's clips are fetched within seconds of its sentences, and thirty-two answers is many
 *  minutes of talking. */

const SEQUENCE_TYPE = "application/x-speech-sequence";
const LENGTH_BYTES = 4;

/** Join two byte runs. Called once per network read, on buffers the size of a sentence of Opus. */
function joined(head: Uint8Array<ArrayBuffer>, tail: Uint8Array): Uint8Array<ArrayBuffer> {
  const out = new Uint8Array(head.length + tail.length);
  out.set(head, 0);
  out.set(tail, head.length);
  return out;
}

/**
 * One queue, one player, and an answer that starts before it has been made.
 *
 * A local voice answers `/api/companion/tts` with its sentences as they are synthesised rather than with
 * one finished file, so the clip for the first sentence is turned into audio and played while the
 * third is still being rendered on the server. That is the difference between hearing an answer begin
 * half a second after asking and waiting out the whole paragraph in silence. An endpoint answers with
 * one file as it always did, and that path is unchanged.
 *
 * Cancelling drops the lot: the queue, the clips already fetched, the clip playing, and the request in
 * flight — which is aborted rather than read to the end, so the server stops synthesising too.
 */
export function createSpeaker(opts: {
  /**
   * Which engine reads the *next* answer: the server's audio, or this browser's own synthesiser.
   *
   * Asked once per answer rather than once per page, because the honest reply changes: a voice that
   * runs on this machine takes a second or two to become a synthesiser, and an answer that arrives
   * during that load has a choice between waiting for it and being read here. It is read here. The
   * answer after it, with the voice built, is read in the voice the operator chose. Whichever it is,
   * it holds for the whole of one answer — an answer that changes voice halfway is worse than either.
   */
  engine: () => "server" | "here";
  lang: string;
  onSpeaking: (on: boolean) => void;
  onLevel?: (level: number) => void;
  /** A sentence the speaker gave up on. The page shows it as written rather than pretending it was said. */
  onUnspoken?: (text: string) => void;
  /** Whether the browser is refusing to make a sound until it is tapped. The page offers the tap. */
  onBlocked?: (blocked: boolean) => void;
  /** How long this answer took between its first sentence arriving and its first sound. Once per answer. */
  onFirstAudio?: (turn: string, ms: number) => void;
}): Speaker {
  const audio = new Audio();
  audio.preload = "auto";
  let generation = 0;
  let queue: string[] = [];
  let pumping = false;
  let inflight: AbortController | null = null;
  /** Ends whatever `play` is waiting on, so cancelling never leaves the pump hanging on a clip. */
  let release: (() => void) | null = null;
  /** The answer being read out. Everything queued belongs to it and nothing older is ever played. */
  let turn = "";
  /** Whether an answer is under way at all, which is not the same as its name being non-empty. */
  let started = false;
  /** Every answer that was stopped. A sentence of one of them arriving afterwards is shown, never
   *  read. A set and not one name: with three answers in quick succession a single slot holds only
   *  the most recent, so a late sentence of the one before it was not recognised as abandoned — it
   *  opened its own turn, which stopped the answer that was live and read the stale one out, the
   *  exact failure this machinery exists to prevent. Bounded, oldest first, because a long
   *  conversation is a long list of finished turns. */
  const abandoned = new Set<string>();
  /** When this answer's first sentence was handed over, and whether its first sound has been reported.
   *  Negative until the first sentence arrives: a clock can legitimately read zero. */
  let turnBegan = -1;
  let heard = false;
  /** Which engine this answer is being read by, fixed for its whole length once it is chosen. */
  let here = false;
  /** Whether that choice has been made for this answer yet. */
  let chosen = false;
  const level = createSpeechLevel(audio, opts);
  /** The first sound of this answer exists. What the operator feels is this number, so it is measured. */
  const sounded = () => {
    if (heard) return;
    heard = true;
    if (turnBegan >= 0) opts.onFirstAudio?.(turn, Math.round(performance.now() - turnBegan));
  };
  const synth = (): Synthesiser | null => {
    try {
      return (window.speechSynthesis as unknown as Synthesiser) ?? null;
    } catch {
      return null;
    }
  };

  /**
   * Play one clip and resolve when it has finished, failed, been cancelled — or stalled.
   *
   * The stall is the point. A media element that never fires `ended` is the same bug as a
   * synthesiser that never fires `onend`, and it happens for the same kinds of reason: a decode that
   * went wrong, a tab the browser stopped giving time to, a `play()` the page was not allowed to
   * make. Progress is what is watched rather than the clock alone — a clip that is playing keeps
   * moving `currentTime`, and one that has stopped moving for `CLIP_STALL_MS` is not playing however
   * long it claims to be.
   */
  const play = (url: string, mine: number) =>
    new Promise<void>((resolve) => {
      if (mine !== generation) {
        URL.revokeObjectURL(url);
        return resolve();
      }
      let done = false;
      let moved = Date.now();
      let guard = 0;
      const finish = (spoken: boolean) => {
        if (done) return;
        done = true;
        clearInterval(guard);
        release = null;
        audio.onended = null;
        audio.onerror = null;
        audio.onplaying = null;
        audio.ontimeupdate = null;
        URL.revokeObjectURL(url);
        if (!spoken) audio.pause();
        resolve();
      };
      release = () => finish(true);
      audio.onended = () => finish(true);
      audio.onerror = () => finish(false);
      audio.onplaying = () => {
        moved = Date.now();
        sounded();
        opts.onBlocked?.(false);
      };
      audio.ontimeupdate = () => {
        moved = Date.now();
      };
      guard = setInterval(() => {
        if (Date.now() - moved < CLIP_STALL_MS) return;
        finish(false);
      }, CLIP_STALL_MS / 2) as unknown as number;
      audio.src = url;
      void audio.play().catch(() => {
        // Refused rather than broken: a browser that wants a tap first says so here and nowhere else.
        opts.onBlocked?.(true);
        finish(false);
      });
    });

  /**
   * The browser's own synthesiser, which takes the text and promises nothing.
   *
   * Everything here is a defence against one of its ways of going quiet. The voices are waited for
   * once, because an utterance handed over before they load is the one that is never spoken. A queue
   * left paused or holding an abandoned utterance is cleared before anything is put into it, because
   * otherwise it swallows every sentence after it. `resume()` is pulsed while it speaks, because
   * Chromium stops a synthesiser it decides has gone on too long and reports nothing. And the whole
   * utterance is under a budget: if no sound starts, or the end never comes, it is abandoned and the
   * sentence is marked as one the operator will have to read.
   */
  const speakHere = async (text: string, mine: number) => {
    const engine = synth();
    if (!engine) {
      opts.onUnspoken?.(text);
      return;
    }
    const voices = await whenVoicesReady(engine, VOICES_WAIT_MS);
    if (mine !== generation) return;
    try {
      if (engine.paused) engine.resume();
      if (engine.speaking || engine.pending) engine.cancel();
    } catch {
      /* an engine that will not be asked about itself is still worth speaking to */
    }
    await new Promise<void>((resolve) => {
      const utterance = new SpeechSynthesisUtterance(text);
      let started = false;
      let done = false;
      let guard = 0;
      let pulse = 0;
      const finish = (spoken: boolean) => {
        if (done) return;
        done = true;
        clearTimeout(guard);
        clearInterval(pulse);
        release = null;
        if (!spoken) {
          try {
            engine.cancel(); // the utterance is abandoned, and a queue still holding it is the next bug
          } catch {
            /* nothing to cancel */
          }
          opts.onUnspoken?.(text);
        }
        resolve();
      };
      release = () => finish(true);
      utterance.onstart = () => {
        started = true;
        sounded();
        opts.onBlocked?.(false);
        clearTimeout(guard);
        guard = setTimeout(() => finish(false), speechBudgetMs(text)) as unknown as number;
      };
      utterance.onend = () => finish(true);
      utterance.onerror = () => {
        if (!started) opts.onBlocked?.(true);
        finish(started);
      };
      // Before a word is heard the only budget that applies is "did anything start at all", and a
      // browser waiting for a tap fails exactly that one.
      guard = setTimeout(() => {
        if (!started) opts.onBlocked?.(true);
        finish(false);
      }, SPEECH_START_MS) as unknown as number;
      pulse = setInterval(() => {
        try {
          engine.resume();
        } catch {
          /* an engine with no resume needs none */
        }
      }, RESUME_PULSE_MS) as unknown as number;
      // Everything that touches the engine is in here, because every part of it throws somewhere:
      // assigning a voice the engine does not recognise, and `speak` itself on an engine that has
      // decided it cannot synthesise. A throw at this point used to take the whole queue with it —
      // the promise never settled, or settled by rejecting into a pump that then abandoned the rest
      // of the answer without a word. It is one more way for a sentence to go unspoken, no more.
      try {
        utterance.lang = opts.lang;
        const match = voiceFor(voices, opts.lang);
        if (match) utterance.voice = match;
        engine.speak(utterance);
      } catch {
        finish(false);
      }
    });
  };

  /** One piece of text from the server: either a sequence of sentences, or one whole file. */
  const speakThere = async (text: string, mine: number) => {
    const controller = new AbortController();
    inflight = controller;
    const forTurn = turn;
    let response: Response;
    try {
      response = await fetch("/api/companion/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json", ...api.authHeaders() },
        // The turn goes with the sentence so the server can time the wait the operator felt — from
        // the words being written to a sound — against the answer that waited, rather than against
        // whichever request happened to be last.
        body: JSON.stringify({ text, turn: forTurn }),
        signal: controller.signal,
      });
    } catch {
      return; // Aborted by a barge-in, or the connection went; either way there is nothing to play.
    }
    if (mine !== generation || !response.ok || !response.body) return;
    if (!(response.headers.get("content-type") ?? "").startsWith(SEQUENCE_TYPE)) {
      const url = URL.createObjectURL(await response.blob());
      return play(url, mine);
    }
    const type = response.headers.get("x-speech-media-type") || "audio/ogg";
    const reader = response.body.getReader();
    // The reader runs ahead of the player: a sentence is fetched while the one before it is being
    // heard, which is the whole reason the server sends them separately.
    const ready: string[] = [];
    let reading = true;
    let wake: (() => void) | null = null;
    const nudge = () => {
      const w = wake;
      wake = null;
      w?.();
    };
    void (async () => {
      let buffer = new Uint8Array(new ArrayBuffer(0));
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done || mine !== generation) break;
          buffer = joined(buffer, value);
          for (;;) {
            if (buffer.length < LENGTH_BYTES) break;
            const size = new DataView(buffer.buffer, buffer.byteOffset, LENGTH_BYTES).getUint32(0);
            if (buffer.length < LENGTH_BYTES + size) break;
            ready.push(URL.createObjectURL(new Blob([buffer.slice(LENGTH_BYTES, LENGTH_BYTES + size)], { type })));
            buffer = buffer.slice(LENGTH_BYTES + size);
            nudge();
          }
        }
      } catch {
        /* the fetch was aborted, or the stream dropped: what arrived is still playable */
      } finally {
        reading = false;
        nudge();
      }
    })();
    for (;;) {
      if (mine !== generation) break;
      const url = ready.shift();
      if (url === undefined) {
        if (!reading) break;
        await new Promise<void>((r) => {
          wake = r;
        });
        continue;
      }
      await play(url, mine);
    }
    controller.abort();
    for (const url of ready) URL.revokeObjectURL(url);
    ready.length = 0;
  };

  /** Work through the queue one piece of text at a time, and report speaking only around real audio. */
  const pump = async () => {
    if (pumping) return;
    pumping = true;
    const mine = generation;
    // Which engine reads this answer is settled at the moment it is about to be read, and then holds
    // for the whole of it. Not earlier: the page learns which of the three speaks from a reading
    // that lands after the concierge's stream is already open, and a choice made before that reading
    // would send the first answer of every conversation to the browser's own synthesiser on a
    // machine that has a voice of its own. Not later either: an answer that changes voice halfway
    // through is worse than either voice.
    if (!chosen) {
      here = opts.engine() === "here";
      chosen = true;
    }
    opts.onSpeaking(true);
    level.start(!here);
    try {
      while (queue.length && mine === generation) {
        const text = queue.shift()!;
        if (here) await speakHere(text, mine);
        else await speakThere(text, mine);
      }
    } finally {
      pumping = false;
      if (mine === generation) {
        level.stop();
        opts.onSpeaking(false);
      } else if (queue.length) {
        // The answer changed while this pump was waiting on a sentence. It has just let go of the
        // engine; the sentences of the new answer are already queued behind it and nothing else is
        // going to pick them up, because `say` found a pump that had not finished yet.
        void pump();
      }
    }
  };

  /**
   * Everything cancelling has to undo, in one place: `cancel`, `stop` and a new answer starting.
   *
   * `keep` decides what happens to the sentences still in the queue. A barge-in throws them away
   * without a word — the operator is talking and does not want the rest — while an answer superseded
   * by a newer one hands each of them to `onUnspoken`, so the page can mark what it showed and never
   * said. Either way nothing of the old answer is played afterwards: the queue is emptied, the
   * request fetching the rest of it is aborted (which is what stops the synthesiser at the far end),
   * the clip in the player is stopped, and the generation moves so that anything already in flight
   * finds itself out of date the moment it comes back.
   */
  const halt = (mark = false) => {
    generation += 1;
    if (started && turn) {
      abandoned.add(turn);
      while (abandoned.size > TURNS_REMEMBERED) abandoned.delete(abandoned.values().next().value as string);
    }
    started = false;
    if (mark) for (const text of queue) opts.onUnspoken?.(text);
    queue = [];
    inflight?.abort();
    inflight = null;
    try {
      synth()?.cancel();
    } catch {
      /* no synthesiser */
    }
    audio.pause();
    const url = audio.currentSrc || audio.src;
    audio.removeAttribute("src");
    audio.onended = null;
    audio.onerror = null;
    // The element is not reliable about firing anything after a pause, and the handler that would
    // have revoked this URL is the one just taken off it, so it is revoked here by hand.
    if (url.startsWith("blob:")) URL.revokeObjectURL(url);
    release?.();
    release = null;
    level.stop();
    opts.onSpeaking(false);
  };

  /**
   * Move to a new answer, ending the one before it wherever it had got to.
   *
   * This is the whole of the fix for hearing two answers at once. A local voice that is still
   * loading, or one that renders slower than speech, leaves the first answer's clips queued and half
   * fetched; the operator, hearing nothing, asks again; and the old clips arrive with the new ones
   * behind them. A clip belongs to an answer, an answer is over when the next one starts, and a clip
   * of an answer that is over is never played.
   */
  const begin = (next: string) => {
    if (started && next === turn) return;
    if (started) halt(true);
    started = true;
    turn = next;
    turnBegan = -1;
    heard = false;
    chosen = false;
  };

  return {
    say: (text: string, next?: string) => {
      const body = text.trim();
      if (!body) return;
      const id = next ?? turn;
      if (id && abandoned.has(id)) {
        // A sentence of an answer that was stopped — the operator talked over it, or asked something
        // else. It is on the screen and it is marked there; reading it out now would be the very
        // backlog this turn machinery exists to prevent.
        opts.onUnspoken?.(body);
        return;
      }
      begin(id);
      if (turnBegan < 0) turnBegan = performance.now();
      queue.push(body);
      void pump();
    },
    beginTurn: (next: string) => begin(next),
    cancel: () => halt(),
    unlock: () => {
      // Nothing is primed while something is being said.
      //
      // Priming is for an engine that has never made a sound: a muted play() inside the tap is what
      // buys the element the right to play later on iOS, and a silent utterance is what buys it from
      // the synthesiser. Doing either to an engine that is already speaking is destructive — the
      // element is *paused* by its own priming, and the synthesiser's queue is cancelled with the
      // answer in it — and it is pointless, because an engine that is making a sound has already
      // demonstrated the only thing the priming was for. The operator taps the microphone in the
      // middle of an answer often; it is how they interrupt.
      const playing = !audio.paused && !!(audio.currentSrc || audio.src);
      if (!playing) {
        audio.muted = true;
        void audio
          .play()
          .then(() => {
            audio.pause();
            audio.muted = false;
          })
          .catch(() => {
            audio.muted = false;
          });
      }
      // And the synthesiser is a second engine with a gesture rule of its own, which reading the
      // voice list does not satisfy. What does is speaking inside the tap — so a silent utterance
      // goes through here, and the queue is cleared first in case one from before the tap is still
      // sitting in it, unspoken and blocking everything behind it.
      try {
        const engine = synth();
        if (!engine) return;
        if (engine.speaking || engine.pending) {
          // It is saying something. Cancelling to prime it would throw the answer away.
          opts.onBlocked?.(false);
          return;
        }
        engine.cancel();
        engine.resume();
        void whenVoicesReady(engine, VOICES_WAIT_MS);
        const primer = new SpeechSynthesisUtterance(" ");
        primer.volume = 0;
        primer.lang = opts.lang;
        engine.speak(primer);
        opts.onBlocked?.(false);
      } catch {
        /* no synthesiser */
      }
    },
    // Unmount goes through here, so the phase must come back down with it: a remount that starts in
    // "speaking" never leaves it, because nothing is playing to end.
    stop: () => halt(),
  };
}

/** Post one recorded utterance for the server to transcribe; returns what it heard. */
export async function sendUtterance(blob: Blob): Promise<string> {
  const wav = await blobToWav(blob);
  const form = new FormData();
  form.append("audio", wav, "utterance.wav");
  const response = await fetch("/api/companion/audio", { method: "POST", headers: api.authHeaders(), body: form });
  if (!response.ok) throw new Error(response.status === 413 ? t("voice.error.recording.long") : t("voice.error.recording", { status: response.status }));
  return String(((await response.json()) as { transcript?: string }).transcript ?? "");
}

// ── the agents panel ───────────────────────────────────────────────────────────────────────

/** One delegated agent as the voice endpoint reports it. */
export type AgentNews = {
  session_id: string;
  title: string;
  status: string;
  last_message_at: string;
  answer: string;
  /** The last thing it said mid-run: narration, or what it is waiting for. */
  progress?: string;
  progress_at?: string;
  /** "operator" — it asked a question; "approval" — the policy stopped a call; "" — it is getting on with it. */
  waiting?: string;
};

/** The one line the panel shows for an agent, and when that line is from. `waiting` is a table key. */
export type AgentNote = { line: string; when: string; waiting: string; live: boolean };

/** What an agent is stopped on, as a key in the table rather than as a word: the panel translates it. */
const WAITING_KEYS: Record<string, string> = {
  operator: "voice.agent.waiting.operator",
  approval: "voice.agent.waiting.approval",
};

/**
 * What to show under an agent's title: its latest words and the moment they are from.
 *
 * Mid-run words win over the last answer while they exist, because they are newer and they are what the
 * operator is waiting to hear about; the panel says so with the timestamp of the line itself, not of the
 * session, so "2 min ago" on a running agent means it spoke two minutes ago rather than started then.
 */
export function agentNote(a: AgentNews): AgentNote {
  const progress = (a.progress ?? "").trim();
  const waiting = WAITING_KEYS[a.waiting ?? ""] ?? "";
  if (progress) return { line: progress, when: a.progress_at || a.last_message_at, waiting, live: true };
  return { line: (a.answer ?? "").trim(), when: a.last_message_at, waiting, live: false };
}

// ── which model the concierge answers with ─────────────────────────────────────────────────

/** One model preset as `/api/companion/voice` lists it; `fast` is the server's judgement, not the page's. */
export type VoicePreset = {
  id: string;
  label: string;
  provider: string;
  model: string;
  thinking: boolean;
  max_output_tokens: number;
  fast: boolean;
};

/** One line of the Model select: what it is called, and what it is, in the parentheses after it. */
export type ModelChoice = { id: string; label: string; detail: string; slow: boolean };

/**
 * The Model row, worked out once from what `/api/companion/voice` said.
 *
 * `slow` on a choice, and `warn` on the row, are the same question asked of a candidate and of the
 * model actually in use: a thinking model, or one allowed to write at length, answers a spoken
 * question long after the operator stopped waiting for it. `addFast` is the harder case — an
 * installation where every model is like that has nothing to pick, so the row says where to get one
 * instead of pretending the choice exists.
 */
export type ModelRow = {
  value: string;
  choices: ModelChoice[];
  /** Why the model in use is the wrong kind for a conversation, as a table key; "" when it is right. */
  warn: string;
  /** The model an empty choice comes out as, for the first option to name; "" when one is chosen. */
  fallback: string;
  addFast: boolean;
};

export function modelRow(state: { preset?: string; using?: string; presets?: VoicePreset[] } | null | undefined): ModelRow {
  const presets = state?.presets ?? [];
  const chosen = state?.preset ?? "";
  const using = presets.find((p) => p.id === (state?.using || chosen));
  return {
    value: chosen,
    choices: presets.map((p) => ({ id: p.id, label: p.label || p.id, detail: `${p.provider} · ${p.model}`, slow: !p.fast })),
    warn: !using || using.fast ? "" : using.thinking ? "voice.card.model.slow" : "voice.card.model.long",
    fallback: !chosen && using ? using.label || using.id : "",
    addFast: presets.length === 0 || !presets.some((p) => p.fast),
  };
}

// ── what the page is doing, as one value ───────────────────────────────────────────────────
//
// The page has six things it can be doing and eleven things that can change which — the operator's
// microphone, the engine loading, the recogniser's words, the concierge's status, its sentences, the
// speaker starting and stopping. Held as a dozen separate pieces of component state, the rules
// between them (a finished answer goes back to listening only if the microphone is still on; a model
// still loading outranks everything) live in four different callbacks and disagree. So they are one
// value and one function, which is also the only way any of it can be tested without a browser.

/** What the operator sees the page doing. Every one of these is drawn differently. */
/** The phases, as a list first: the screen names each of them through the dictionary, and a phase
 *  that exists only in a type is a phase no test can check has a word. */
export const VOICE_PHASES = ["idle", "loading", "listening", "thinking", "delegating", "speaking"] as const;

export type VoicePhase = (typeof VOICE_PHASES)[number];

/**
 * What is in the middle of the page: the orb, the concierge's own transcript, or an agent's.
 *
 * This is a fact about the conversation, not about a component, which is why it is in the reducer
 * with the rest of them. The voice session does not notice it at all — the recogniser, the speaker
 * and the stream are the same three objects whichever of these is drawn — and that is the whole
 * point of keeping it here rather than in the component that swaps the views.
 */
export type VoiceCenter = { view: "orb" } | { view: "transcript" } | { view: "agent"; id: string; title: string };

/** The session whose transcript the middle of the page is showing, or "" for the orb. */
export function centerSession(center: VoiceCenter, voiceSessionId: string): string {
  if (center.view === "agent") return center.id;
  if (center.view === "transcript") return voiceSessionId;
  return "";
}

/** Where the local recogniser's weights are, as `/api/companion/voice` and the progress stream report them. */
export type EngineState = { state: string; model: string; loadedInMs: number; error: string };

export type VoiceUi = {
  phase: VoicePhase;
  micOn: boolean;
  /** The title of the agent being set up, while one is. */
  delegating: string;
  /** The last thing the operator said, as it was understood. */
  asked: string;
  /** What the recogniser has heard of the sentence being said now. */
  heard: string;
  /** The concierge's answer, a spoken sentence at a time, in the order it was said. */
  spoken: string[];
  /** The answer as it is being written, for the part that has not become a sentence yet. */
  partial: string;
  /** The sentences of this answer the speaker gave up on, so the page can mark them as read, not heard. */
  unspoken: string[];
  /** Whether the browser is refusing to make a sound until it is tapped. */
  blocked: boolean;
  problem: string;
  agents: AgentNews[];
  engine: EngineState;
  /** Where the chosen local voice is in its loading, as `/api/companion/voice` and `/api/tts/progress` report it. */
  voice: EngineState;
  /** How long the last answer took between its first sentence arriving and its first sound, in this page. */
  firstAudioMs: number;
  /** What is drawn in the middle of the page. Changing it changes nothing about the voice session. */
  center: VoiceCenter;
};

export type VoiceEvent =
  | { type: "mic"; on: boolean }
  | { type: "heard"; text: string }
  | { type: "asked"; text: string }
  | { type: "partial"; text: string }
  | { type: "say"; text: string }
  | { type: "status"; state: string; title?: string }
  | { type: "done" }
  | { type: "agents"; agents: AgentNews[] }
  | { type: "speaking"; on: boolean }
  | { type: "unspoken"; text: string }
  | { type: "blocked"; on: boolean }
  | { type: "barge" }
  | { type: "engine"; engine: Partial<EngineState> }
  | { type: "voice"; engine: Partial<EngineState> }
  | { type: "audio"; turn: string; ms: number }
  | { type: "problem"; message: string }
  | { type: "center"; center: VoiceCenter }
  | { type: "cleared" };

export const IDLE_VOICE: VoiceUi = {
  phase: "idle",
  micOn: false,
  delegating: "",
  asked: "",
  heard: "",
  spoken: [],
  partial: "",
  unspoken: [],
  blocked: false,
  problem: "",
  agents: [],
  engine: { state: "ready", model: "", loadedInMs: 0, error: "" },
  voice: { state: "ready", model: "", loadedInMs: 0, error: "" },
  firstAudioMs: 0,
  center: { view: "orb" },
};

/** Whether the microphone may be opened at all: a model still loading cannot hear anything. */
export function micReady(state: VoiceUi): boolean {
  return state.engine.state !== "loading";
}

/**
 * What the page falls back to when nothing is happening: still loading, still listening, or idle.
 *
 * Every transition that ends — the answer finished, the speaker stopped, the error shown — comes back
 * through here rather than guessing "idle", which is what used to leave the page saying "Ready" while
 * the microphone was still open.
 */
function resting(state: VoiceUi): VoicePhase {
  if (state.engine.state === "loading") return "loading";
  return state.micOn ? "listening" : "idle";
}

export function voiceReducer(state: VoiceUi, event: VoiceEvent): VoiceUi {
  switch (event.type) {
    case "mic": {
      const next = { ...state, micOn: event.on, heard: event.on ? state.heard : "" };
      // Opening the microphone does not stop the concierge either. The page had it the other way
      // round: a tap in the middle of an answer put the page in "listening" while the answer was
      // still being read out, and nothing dispatched "speaking" again — so the chip and the orb said
      // the page was listening for the whole of it, and the barge-in, which will not interrupt a page
      // that is not speaking, could not happen at all. Whether the microphone is open and what the
      // page is doing are two different facts, and only the second one is the phase.
      if (event.on) {
        const busy = state.phase === "speaking" || state.phase === "thinking" || state.phase === "delegating";
        return { ...next, problem: "", phase: busy ? state.phase : resting(next) };
      }
      // Stopping the microphone does not stop the concierge: an answer being written or spoken keeps
      // its phase, and only a page that was merely listening goes quiet.
      return { ...next, phase: state.phase === "listening" || state.phase === "loading" ? resting(next) : state.phase };
    }
    case "heard":
      return { ...state, heard: event.text };
    case "asked":
      // A new utterance clears the last answer rather than appending to it: the captions are a
      // conversation, and the previous reply is on its way off the screen the moment this one starts.
      return { ...state, asked: event.text, heard: "", spoken: [], partial: "", unspoken: [], problem: "", phase: "thinking" };
    case "partial":
      return { ...state, partial: event.text };
    case "say": {
      // A sentence handed to the speaker is the answer being read out, and the page says so without
      // waiting for audio to arrive: fetching a sentence from a speech endpoint takes long enough
      // that the page would otherwise still be saying "Thinking" while the answer is on the screen.
      // A speaker that then fails reports it has stopped, and the phase comes back on its own.
      if (!event.text.trim()) return state;
      const spoken = [...state.spoken, event.text.trim()];
      return { ...state, spoken, phase: state.engine.state === "loading" ? state.phase : "speaking" };
    }
    case "status": {
      if (event.state === "idle") return { ...state, delegating: "", phase: state.phase === "speaking" ? state.phase : resting(state) };
      const phase = event.state === "delegating" ? "delegating" : event.state === "thinking" ? "thinking" : state.phase;
      return { ...state, delegating: event.state === "delegating" ? event.title ?? "" : "", phase };
    }
    case "done":
      return state.phase === "thinking" || state.phase === "delegating" ? { ...state, delegating: "", phase: resting(state) } : { ...state, delegating: "" };
    case "agents":
      return { ...state, agents: event.agents };
    case "speaking":
      if (event.on) return { ...state, phase: "speaking" };
      return state.phase === "speaking" ? { ...state, phase: resting(state) } : state;
    case "unspoken": {
      // A sentence the speaker abandoned. It stays on the screen where it was — it is part of the
      // answer — and it is marked, because "you were told this out loud" and "this is on the screen
      // and nobody said it" are different things to the person who is not looking at the screen.
      const line = event.text.trim();
      if (!line || state.unspoken.includes(line)) return state;
      const unspoken = [...state.unspoken, line];
      // And where not one sentence of this answer was read out, the page is not speaking, whatever
      // the speaker's queue believes. "Speaking" with nothing audible is the state the operator
      // reported: the chip said the answer was being read out, and the page was silent.
      const silent = state.phase === "speaking" && state.spoken.every((said) => unspoken.includes(said));
      return { ...state, unspoken, phase: silent ? resting(state) : state.phase };
    }
    case "blocked":
      // Losing the block is not news worth a render when there was none: the speaker says so at the
      // start of every sentence it manages to say.
      return state.blocked === event.on ? state : { ...state, blocked: event.on };
    case "barge":
      // The operator talked over the answer. What was being read out is abandoned where it stopped —
      // the sentences already said stay on the screen, because they were said — and the page goes
      // back to listening in the same breath rather than waiting for the speaker to report itself.
      return state.phase === "speaking" ? { ...state, partial: "", phase: resting(state) } : state;
    case "voice":
      // The synthesiser loading changes what the page says, not what it lets the operator do: the
      // microphone is about recognition, and an answer that arrives while the voice is still being
      // built is read by the browser rather than held back.
      return { ...state, voice: { ...state.voice, ...event.engine } };
    case "audio":
      // What the operator felt, measured on the page that made them feel it: the moment the first
      // sentence of this answer arrived, to the moment a sound came out of it.
      return { ...state, firstAudioMs: event.ms };
    case "engine": {
      const engine = { ...state.engine, ...event.engine };
      const next = { ...state, engine };
      // A model that has finished loading releases the page: whatever it was waiting to be, it is now.
      if (state.phase === "loading" || engine.state === "loading") return { ...next, phase: resting(next) };
      return next;
    }
    case "problem":
      return { ...state, problem: event.message, phase: resting(state) };
    case "center": {
      // Swapping what is in the middle of the page is the one event here that changes nothing else.
      // Not the phase, not the microphone, not a caption: the operator is reading while the same
      // conversation goes on being had, and the page must not so much as blink at it.
      const now = state.center;
      const next = event.center;
      const same = now.view === next.view && (now.view !== "agent" || next.view !== "agent" || now.id === next.id);
      return same ? state : { ...state, center: next };
    }
    case "cleared":
      // A new conversation has no transcript to read, so the page comes back to the orb with it.
      return { ...IDLE_VOICE, micOn: state.micOn, engine: state.engine, voice: state.voice, blocked: state.blocked, phase: resting(state) };
  }
}

// ── the orb ────────────────────────────────────────────────────────────────────────────────

/**
 * One microphone level, smoothed the way an ear is: quick to rise, slow to fall.
 *
 * A raw RMS read sixty times a second makes the orb flicker on every consonant and collapse in every
 * gap between two words, which reads as a fault rather than as speech. Rising fast keeps the reaction
 * immediate; falling slowly keeps a sentence looking like one thing.
 */
export function smoothLevel(previous: number, next: number): number {
  const target = Math.max(0, Math.min(1, next));
  const rate = target > previous ? 0.55 : 0.12;
  return previous + (target - previous) * rate;
}

/** How the orb looks right now: how big, how bright, and how fast its light travels around it. */
export type OrbVisual = { scale: number; glow: number; spin: number };

/**
 * The level-to-look mapping, in one pure function so it can be checked without a browser.
 *
 * The curve is deliberately not linear. Speech at a normal distance from a laptop microphone sits
 * around an RMS of 0.05–0.2, so a linear mapping spends nine tenths of its range on volumes nobody
 * produces and the orb barely moves while someone is talking normally.
 */
export function orbVisual(phase: VoicePhase, level: number): OrbVisual {
  const heard = Math.pow(Math.max(0, Math.min(1, level)) * 3.2, 0.55);
  const loud = Math.max(0, Math.min(1, heard));
  switch (phase) {
    case "listening":
      return { scale: 1 + loud * 0.24, glow: 0.35 + loud * 0.65, spin: 1 };
    case "speaking":
      return { scale: 1 + loud * 0.16, glow: 0.45 + loud * 0.55, spin: 1.6 };
    case "thinking":
    case "delegating":
      return { scale: 1.05, glow: 0.7, spin: 2.4 };
    case "loading":
      return { scale: 1.02, glow: 0.5, spin: 2 };
    default:
      return { scale: 1, glow: 0.3, spin: 0.7 };
  }
}

/**
 * The microphone's loudness, for a listener that has no audio of its own.
 *
 * The local model's listener taps the samples it is already sending, and the recorder already has an
 * analyser; the browser's own `SpeechRecognition` hands over words and nothing else, so on that path
 * the page opens its own read-only tap on the same microphone. The permission has already been
 * granted by then, so nothing is asked of the operator twice.
 */
export function createMeter(onLevel: (level: number) => void): { start: () => Promise<void>; stop: () => void } {
  let stream: MediaStream | null = null;
  let context: AudioContext | null = null;
  let frame = 0;
  return {
    start: async () => {
      if (stream) return;
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      } catch {
        return; // The listener itself reports a refused microphone; a meter that cannot run is silent.
      }
      const Ctx = window.AudioContext ?? window.webkitAudioContext!;
      context = new Ctx();
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      context.createMediaStreamSource(stream).connect(analyser);
      const samples = new Float32Array(analyser.fftSize);
      const tick = () => {
        analyser.getFloatTimeDomainData(samples);
        let sum = 0;
        for (const v of samples) sum += v * v;
        onLevel(Math.sqrt(sum / samples.length));
        frame = requestAnimationFrame(tick);
      };
      frame = requestAnimationFrame(tick);
    },
    stop: () => {
      cancelAnimationFrame(frame);
      stream?.getTracks().forEach((t) => t.stop());
      stream = null;
      void context?.close();
      context = null;
    },
  };
}
