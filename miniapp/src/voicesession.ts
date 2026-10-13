// The voice session: the half of the companion's call that must not notice what is on the screen.
//
// A conversation held out loud has four long-lived things in it — a recogniser with the microphone
// open, a speaker with a queue of clips, the concierge's event stream, and the two watchers that
// report the engines finishing their loading — and not one of them has anything to do with what the
// operator is looking at. They used to live in the component that draws the orb, which was fine for
// as long as the orb was the only thing the page ever drew. The moment the middle of the page can
// show a transcript instead, that arrangement tears the conversation down to change a view: the
// answer stops mid-sentence, the recognised words are lost, and the stream reconnects.
//
// So they live here, in one object outside React, acquired by the page and released when the page
// goes away — never when it merely changes its mind about what to draw. The component subscribes to
// the state and reads the level; it owns no part of the session at all.
//
// The counts (`parts()`) are not decoration either. "Switching views does not recreate the speaker"
// is a claim about object lifetimes, which no screenshot can check and no assertion on the DOM can
// either; a counter incremented at each construction can be read from a test and from the page.

import { api } from "./api";
import { createLocalListener, localListenSupported } from "./stt";
import { sttFrame } from "./sttview";
import { errorText, haptic } from "./ui";
import type { Listener, Speaker, VoiceEvent, VoiceUi } from "./voice";
import {
  IDLE_VOICE,
  createMeter,
  createRecognition,
  createRecorder,
  createSpeaker,
  recognitionSupported,
  sendUtterance,
  shouldBargeIn,
  voiceLang,
  voiceReducer,
} from "./voice";

/** How long after the last spoken word the microphone stays deaf: a speaker's tail reaches it late. */
export const ECHO_TAIL_MS = 400;

/**
 * What `/api/companion/voice` has told the page about who listens and who speaks.
 *
 * The session is handed this rather than reading it: the reading is a query the component already
 * holds, it changes while the session runs, and the session's only interest in it is which engine
 * reads the next answer and which listener to build when the microphone is opened.
 */
export type VoiceReading = {
  /** Whether `/api/companion/voice` has answered at all. Before it has, a sentence waits rather than being spoken. */
  known: boolean;
  /** The server produces the audio: a voice on this machine and a speech endpoint both do. */
  serverTts: boolean;
  /** A voice on this machine that is still being built. The browser reads this one answer instead. */
  ttsLoading: boolean;
  /** Whether the local recognition kind is the one selected, and this browser can feed it. */
  localStt: boolean;
};

const UNREAD: VoiceReading = { known: false, serverTts: false, ttsLoading: false, localStt: false };

/** How many of each the session has built since it started. A view change adds nothing to any of them. */
export type VoiceParts = { speakers: number; listeners: number; streams: number };

export type VoiceSession = {
  state: () => VoiceUi;
  subscribe: (fn: () => void) => () => void;
  dispatch: (event: VoiceEvent) => void;
  /** The microphone's loudness as the listener last reported it, read by the orb's animation frame. */
  level: () => number;
  /** What `/api/companion/voice` says, folded in whenever the component's query answers again. */
  reading: (reading: VoiceReading) => void;
  startMic: () => Promise<void>;
  stopMic: () => void;
  /** A spoken utterance, already in words: answered by the voice model and read aloud. */
  send: (text: string) => Promise<void>;
  /** A typed message: a text turn, answered by the background model and read aloud only on a call. */
  type: (text: string) => Promise<void>;
  /** A new conversation: the microphone closes, the answer stops, the captions clear. */
  clear: () => Promise<void>;
  /** A tap. Buys the right to make a sound in a browser that wants one before it will speak. */
  unlock: () => void;
  parts: () => VoiceParts;
  /**
   * Open the concierge's stream. Idempotent, and deliberately not done when the session is built.
   *
   * A session nobody is holding has nothing to listen to, and the page holds it in an effect — after
   * the first frame is on the screen. Connecting a frame earlier, from inside a render, buys nothing
   * and costs the first sentence of the first answer a quarter of a second of mounting to wait
   * through before there is anywhere to draw it.
   */
  start: () => void;
  /** Told when an answer ends, so the page can re-read the server's own timing for it. */
  onAnswered: (fn: () => void) => () => void;
  /** Start watching whichever engines have something to load. Idempotent: twice is one watcher. */
  watchEngines: (stt: boolean, tts: boolean) => void;
  stop: () => void;
};

/**
 * What the session needs from the world, so a test can hand it something else.
 *
 * Only the four things a jsdom has no answer for: the network, the speaker, the listeners and the
 * microphone meter. Everything else in here is arithmetic over the reducer.
 */
export type VoiceDeps = {
  fetch: typeof fetch;
  authHeaders: () => Record<string, string>;
  post: (path: string, body: unknown) => Promise<unknown>;
  makeSpeaker: typeof createSpeaker;
  makeMeter: typeof createMeter;
  makeLocalListener: typeof createLocalListener;
  makeRecognition: typeof createRecognition;
  makeRecorder: typeof createRecorder;
  localListenSupported: () => boolean;
  recognitionSupported: () => boolean;
  sendUtterance: (blob: Blob) => Promise<string>;
  lang: string;
  /** The short vibration a tap and an interruption get, where the device has one. */
  haptic: (kind: "light" | "medium") => void;
};

function liveDeps(): VoiceDeps {
  return {
    fetch: (...args: Parameters<typeof fetch>) => fetch(...args),
    authHeaders: () => api.authHeaders(),
    post: (path, body) => api.post(path, body),
    makeSpeaker: createSpeaker,
    makeMeter: createMeter,
    makeLocalListener: createLocalListener,
    makeRecognition: createRecognition,
    makeRecorder: createRecorder,
    localListenSupported,
    recognitionSupported,
    sendUtterance,
    lang: voiceLang(),
    haptic,
  };
}

/**
 * One conversation, held for as long as the page is open.
 *
 * Built by `voiceSession` and exported here for the tests, which want one they can end.
 */
export function createVoiceSession(given?: Partial<VoiceDeps>): VoiceSession {
  const deps: VoiceDeps = { ...liveDeps(), ...given };
  let ui: VoiceUi = IDLE_VOICE;
  const watchers = new Set<() => void>();
  const counts: VoiceParts = { speakers: 0, listeners: 0, streams: 0 };

  let read: VoiceReading = UNREAD;
  let speaker: Speaker | null = null;
  let listener: Listener | null = null;
  let meter: ReturnType<typeof createMeter> | null = null;
  let stopped = false;

  // The page is heard by its own microphone: a laptop speaker plays the answer straight back into
  // the recogniser. While anything is playing, everything the listener hears is dropped.
  let speaking = false;
  let deafUntil = 0;
  let speakingSince = 0;
  let rawLevel = 0;
  // Sentences that arrived before `/api/companion/voice` had said which of the three speaks. Committing an
  // answer to the browser's synthesiser on a machine that has a voice of its own is a worse mistake
  // than holding its first sentence for the tenth of a second the reading takes.
  let held: [string, string][] = [];
  /** Told when an answer ends. The page re-reads `/api/companion/voice` for the server's half of the timing. */
  const answered = new Set<() => void>();

  const emit = () => {
    for (const fn of [...watchers]) fn();
  };
  const dispatch = (event: VoiceEvent) => {
    const next = voiceReducer(ui, event);
    if (next === ui) return;
    ui = next;
    emit();
  };

  const earsOpen = () => !speaking && Date.now() >= deafUntil;

  // ── the speaker ──────────────────────────────────────────────────────────────────────────
  //
  // Built once, on the first sentence that needs it, and never rebuilt: which engine reads an answer
  // is a question asked of `read` at the start of each answer rather than baked into the object, so
  // a new reading of `/api/companion/voice` changes the next answer without disturbing the one being spoken.
  const ensureSpeaker = (): Speaker => {
    if (speaker) return speaker;
    counts.speakers += 1;
    speaker = deps.makeSpeaker({
      engine: () => {
        if (!read.serverTts) return "here";
        if (read.ttsLoading && typeof window !== "undefined" && "speechSynthesis" in window) return "here";
        return "server";
      },
      lang: deps.lang,
      onSpeaking: (on: boolean) => {
        speaking = on;
        if (on) speakingSince = Date.now();
        else deafUntil = Date.now() + ECHO_TAIL_MS;
        dispatch({ type: "speaking", on });
      },
      onLevel: (level: number) => {
        rawLevel = level;
      },
      onUnspoken: (text: string) => dispatch({ type: "unspoken", text }),
      onBlocked: (on: boolean) => dispatch({ type: "blocked", on }),
      onFirstAudio: (turn: string, ms: number) => dispatch({ type: "audio", turn, ms }),
    });
    return speaker;
  };

  const say = (text: string, turn: string) => {
    if (!text.trim()) return;
    if (!read.known) {
      held.push([text, turn]);
      return;
    }
    ensureSpeaker().say(text, turn);
  };
  const release = () => {
    if (!read.known || !held.length) return;
    const waiting = held;
    held = [];
    for (const [text, turn] of waiting) ensureSpeaker().say(text, turn);
  };

  // ── the concierge's half of the conversation ────────────────────────────────────────────
  const stream = new AbortController();
  let opened = false;
  const openStream = () => {
    if (opened) return;
    opened = true;
    counts.streams += 1;
    void (async () => {
      let backoff = 1000;
      while (!stopped) {
        try {
          const response = await deps.fetch("/api/companion/stream", { headers: deps.authHeaders(), signal: stream.signal });
          // One bodiless answer from a proxy is a dropped connection, not the end of the session:
          // fall through to the backoff below rather than leaving the loop for good.
          if (!response.body) throw new Error("no stream");
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          backoff = 1000;
          while (!stopped) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const frames = buffer.split("\n\n");
            buffer = frames.pop() ?? "";
            for (const frame of frames) {
              const event = /^event: (.*)$/m.exec(frame)?.[1];
              const data = /^data: (.*)$/m.exec(frame)?.[1];
              if (!event || !data) continue;
              try {
                handle(event, JSON.parse(data));
              } catch {
                /* one malformed frame must not end the stream */
              }
            }
          }
        } catch {
          /* aborted or dropped: reconnect below */
        }
        if (stopped) return;
        await new Promise((r) => setTimeout(r, backoff));
        backoff = Math.min(backoff * 2, 15000);
      }
    })();
  };

  function handle(event: string, p: Record<string, unknown>) {
    if (event === "partial") dispatch({ type: "partial", text: String(p.text ?? "") });
    else if (event === "say") {
      dispatch({ type: "say", text: String(p.text ?? "") });
      // Read aloud only on a call: the microphone open, or the orb in the middle of the page, which is
      // the call's own view. The companion is a conversation in text as much as in voice, and an
      // answer to a typed message read out while its transcript is being read is a page that talks
      // at somebody unasked.
      if (ui.micOn || ui.center.view === "orb") say(String(p.text ?? ""), String(p.turn ?? ""));
    } else if (event === "agents") dispatch({ type: "agents", agents: (p.agents ?? []) as VoiceUi["agents"] });
    else if (event === "error") dispatch({ type: "problem", message: String(p.message ?? "the concierge stopped") });
    else if (event === "done") {
      dispatch({ type: "done" });
      for (const fn of [...answered]) fn();
    } else if (event === "status") {
      // A run starting is the earliest the page knows the previous answer is over — earlier than its
      // first sentence, which is the moment that used to arrive behind a queue of clips from the
      // answer before it.
      if (String(p.state ?? "") === "thinking" && p.turn) ensureSpeaker().beginTurn(String(p.turn));
      dispatch({ type: "status", state: String(p.state ?? "idle"), title: String(p.title ?? "") });
    }
  }

  // ── the two engines loading into memory ─────────────────────────────────────────────────
  //
  // Both are opened at most once and closed only when the session ends. Opening a page's second view
  // used to mean closing and reopening these, which on a slow load is how the page misses the one
  // frame that says the voice is ready and reads the whole conversation in the browser's.
  const loads = new AbortController();
  const watched = { stt: false, tts: false };
  const watchLoad = async (path: string, fold: (frame: Record<string, unknown>) => void) => {
    try {
      const response = await deps.fetch(path, { headers: deps.authHeaders(), signal: loads.signal });
      if (!response.body) return;
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) return;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const chunk of frames) {
          const data = /^data: (.*)$/m.exec(chunk)?.[1];
          if (!data) continue;
          try {
            fold(JSON.parse(data) as Record<string, unknown>);
          } catch {
            /* one malformed frame must not end the stream */
          }
        }
      }
    } catch {
      /* the session ended, or the stream dropped */
    }
  };

  const session: VoiceSession = {
    state: () => ui,
    subscribe: (fn) => {
      watchers.add(fn);
      return () => watchers.delete(fn);
    },
    dispatch,
    level: () => rawLevel,
    reading: (next: VoiceReading) => {
      read = next;
      release();
    },
    parts: () => ({ ...counts }),
    start: openStream,
    onAnswered: (fn: () => void) => {
      answered.add(fn);
      return () => {
        answered.delete(fn);
      };
    },
    watchEngines: (stt: boolean, tts: boolean) => {
      if (stt && !watched.stt) {
        watched.stt = true;
        void watchLoad("/api/stt/progress", (raw) => {
          const frame = sttFrame(raw);
          if (frame?.kind !== "engine") return;
          dispatch({ type: "engine", engine: { state: frame.load.state, model: frame.load.model, loadedInMs: frame.load.loaded_in_ms, error: frame.load.error } });
        });
      }
      if (tts && !watched.tts) {
        watched.tts = true;
        void watchLoad("/api/tts/progress", (frame) => {
          if (String(frame.kind ?? "") !== "engine") return;
          dispatch({
            type: "voice",
            engine: { state: String(frame.state ?? "idle"), model: String(frame.voice ?? ""), loadedInMs: Number(frame.loaded_in_ms ?? 0), error: String(frame.error ?? "") },
          });
        });
      }
    },
    startMic: async () => {
      if (listener) return;
      ensureSpeaker().unlock();
      const onSpeechStart = (level?: number) => {
        // The browser's own recognition reports that somebody started talking and never says how
        // loudly; on that path the page does measure, through the meter it opened for the orb, and
        // not using that reading meant the page barging in on itself.
        const heard = level ?? (meter ? rawLevel : undefined);
        if (!shouldBargeIn({ speaking, playingForMs: Date.now() - speakingSince, level: heard })) return;
        deps.haptic("light");
        bargeIn();
      };
      const handlers = {
        onInterim: (text: string) => {
          // Words while the answer is being read out are the operator talking over it, and this is
          // the second chance to notice: a listener announces speech once, and a moment the page
          // could not act on used to be the last one.
          if (!earsOpen()) {
            onSpeechStart();
            return;
          }
          dispatch({ type: "heard", text });
        },
        onFinal: (text: string) => {
          if (earsOpen()) void session.send(text);
        },
        onSpeechStart,
        onError: (message: string) => dispatch({ type: "problem", message }),
        onLevel: (level: number) => {
          rawLevel = level;
        },
      };
      const l = read.localStt && deps.localListenSupported()
        ? deps.makeLocalListener(handlers)
        : deps.recognitionSupported()
          ? deps.makeRecognition(deps.lang, handlers)
          : deps.makeRecorder({
              ...handlers,
              onUtterance: async (blob: Blob) => {
                if (!earsOpen()) return;
                try {
                  const text = await deps.sendUtterance(blob);
                  if (text.trim()) dispatch({ type: "asked", text: text.trim() });
                } catch (e) {
                  dispatch({ type: "problem", message: errorText(e) });
                }
              },
            });
      counts.listeners += 1;
      listener = l;
      await l.start();
      // The browser's own recognition hands over words and no audio at all, so on that path the
      // level the orb reacts to comes from a second, read-only tap on the microphone.
      if (l.kind === "recognition") {
        meter = deps.makeMeter((level: number) => {
          rawLevel = level;
        });
        void meter.start();
      }
      dispatch({ type: "mic", on: true });
      deps.haptic("medium");
      // The server answers agents' reports with the voice model while a call is open.
      void deps.post("/api/companion/call", { on: true }).catch(() => undefined);
    },
    stopMic: () => {
      if (listener) void deps.post("/api/companion/call", { on: false }).catch(() => undefined);
      listener?.stop();
      listener = null;
      meter?.stop();
      meter = null;
      rawLevel = 0;
      dispatch({ type: "mic", on: false });
    },
    send: async (text: string) => {
      const body = text.trim();
      if (!body) return;
      // The previous answer is over the moment another question is asked. Anything of it still
      // queued would otherwise be read out over the answer to this one.
      speaker?.cancel();
      dispatch({ type: "asked", text: body });
      try {
        await deps.post("/api/companion/say", { text: body });
      } catch (e) {
        dispatch({ type: "problem", message: errorText(e) });
      }
    },
    type: async (text: string) => {
      const body = text.trim();
      if (!body) return;
      speaker?.cancel();
      dispatch({ type: "asked", text: body });
      try {
        await deps.post("/api/companion/message", { text: body, source: "app" });
      } catch (e) {
        dispatch({ type: "problem", message: errorText(e) });
      }
    },
    clear: async () => {
      session.stopMic();
      speaker?.cancel();
      dispatch({ type: "cleared" });
      try {
        await deps.post("/api/companion/new", {});
      } catch (e) {
        dispatch({ type: "problem", message: errorText(e) });
      }
    },
    unlock: () => ensureSpeaker().unlock(),
    stop: () => {
      if (listener) void deps.post("/api/companion/call", { on: false }).catch(() => undefined);
      stopped = true;
      stream.abort();
      loads.abort();
      listener?.stop();
      listener = null;
      meter?.stop();
      meter = null;
      speaker?.stop();
      speaker = null;
      watchers.clear();
      answered.clear();
    },
  };

  function bargeIn() {
    // Three things stop, and all three have to: the clip that is playing, the request fetching the
    // rest of the answer, and the run that is producing the sentences.
    speaker?.cancel();
    dispatch({ type: "barge" });
    // The tail the speaker leaves behind is for its own echo. The operator is talking *now*.
    deafUntil = 0;
    void deps.post("/api/companion/interrupt", {}).catch(() => undefined);
  }

  return session;
}

// ── one session per page, and it outlives the component ─────────────────────────────────────
//
// The counter is not a nicety. React mounts a component twice in development, and a session torn
// down between the two mounts is a stream reconnected, a microphone closed and an answer cut off
// every time the page is opened. So a release is deferred to the end of the task: a component that
// comes straight back takes the same session, and only a page genuinely left behind ends it.

let current: VoiceSession | null = null;
let holders = 0;
let ending: ReturnType<typeof setTimeout> | null = null;

/** The session this page is having, built on first sight and kept until the page is left. */
export function voiceSession(): VoiceSession {
  if (ending) {
    clearTimeout(ending);
    ending = null;
  }
  if (!current) current = createVoiceSession();
  return current;
}

/**
 * Keep the session alive while a component is mounted. The returned function lets it go.
 *
 * Holding is done in an effect rather than in a render, because a render that never becomes a mount
 * would hold forever, and React runs one of those on purpose in development.
 */
export function holdVoiceSession(): () => void {
  voiceSession().start();
  holders += 1;
  let let_go = false;
  return () => {
    if (let_go) return;
    let_go = true;
    holders = Math.max(0, holders - 1);
    if (holders > 0 || !current) return;
    if (ending) clearTimeout(ending);
    ending = setTimeout(() => {
      ending = null;
      if (holders > 0 || !current) return;
      current.stop();
      current = null;
    }, 0);
  };
}

/** For the tests: forget whatever is held, without waiting for a task to run. */
export function resetVoiceSession(): void {
  if (ending) clearTimeout(ending);
  ending = null;
  holders = 0;
  current?.stop();
  current = null;
}
