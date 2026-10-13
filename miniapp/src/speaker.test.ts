// The speaker against a synthesiser that misbehaves in each of the three ways a real one does.
//
// None of these are hypothetical. Every browser's `speechSynthesis` reports an empty voice list on
// the first call and fills it later; Chromium abandons an utterance without firing `onend`; and a
// browser that has not been tapped accepts `speak()` and makes no sound and says nothing about it.
// The page used to wait on that engine's word, so one bad utterance held every sentence behind it —
// the answer was not spoken at all, and the page stayed in "speaking" with the microphone deaf.
//
// What is asserted here is the one property that makes the page recoverable: every sentence handed
// to the speaker is finished with, in bounded time, whatever the engine does.

import { afterEach, describe, expect, it, vi } from "vitest";

type Utterance = { text: string; lang: string; voice: unknown; volume: number; onstart: null | (() => void); onend: null | (() => void); onerror: null | (() => void) };

type SynthOptions = {
  /** Voices at the first call; an empty list is announced with `voiceschanged` when `arrive` is called. */
  voices?: boolean;
  /** How many utterances actually end. The ones after that are accepted and never finish. */
  ends?: number;
  /** Utterances, by their order, that this engine accepts and then swallows without a word. */
  swallow?: number[];
  /** A browser that wants a tap: `speak` is accepted, nothing starts, and nothing is reported. */
  silent?: boolean;
};

function fakeSynth(options: SynthOptions) {
  const ends = options.ends ?? Infinity;
  const swallow = options.swallow ?? [];
  const spoken: string[] = [];
  const listeners: (() => void)[] = [];
  let voices = options.voices === false ? [] : [{ lang: "en-US", name: "Alex" } as unknown as SpeechSynthesisVoice];
  let allowed = !options.silent;
  const synth = {
    getVoices: () => voices,
    speak: (u: Utterance) => {
      if (!u.text.trim()) return; // the silent primer an unlock speaks inside the tap
      spoken.push(u.text);
      if (!allowed) return; // accepted, and nothing is ever heard of it again
      setTimeout(() => {
        u.onstart?.();
        if (spoken.length <= ends && !swallow.includes(spoken.length - 1)) setTimeout(() => u.onend?.(), 1);
      }, 1);
    },
    cancel: () => undefined,
    resume: () => undefined,
    speaking: false,
    pending: false,
    paused: false,
    addEventListener: (name: string, fn: () => void) => {
      if (name === "voiceschanged") listeners.push(fn);
    },
    removeEventListener: () => undefined,
  };
  return {
    synth,
    spoken,
    /** The voices load, late, the way they do in every browser. */
    arrive: () => {
      voices = [{ lang: "en-US", name: "Alex" } as unknown as SpeechSynthesisVoice];
      for (const fn of listeners) fn();
    },
    /** The operator taps the page, and the browser starts allowing sound. */
    tap: () => {
      allowed = true;
    },
  };
}

function install(options: SynthOptions) {
  const fake = fakeSynth(options);
  class Utter {
    text: string;
    lang = "";
    voice: unknown = null;
    volume = 1;
    onstart: null | (() => void) = null;
    onend: null | (() => void) = null;
    onerror: null | (() => void) = null;
    constructor(text: string) {
      this.text = text;
    }
  }
  const g = globalThis as Record<string, unknown>;
  g.window = { location: { search: "", pathname: "/", hash: "" }, history: { replaceState: () => undefined }, speechSynthesis: fake.synth };
  g.SpeechSynthesisUtterance = Utter;
  g.requestAnimationFrame = () => 0;
  g.cancelAnimationFrame = () => undefined;
  g.Audio = class {
    preload = "";
    src = "";
    currentSrc = "";
    muted = false;
    onended: unknown = null;
    onerror: unknown = null;
    play() {
      return Promise.resolve();
    }
    pause() {}
    removeAttribute() {}
  };
  (URL as unknown as { createObjectURL: () => string }).createObjectURL = () => "blob:clip";
  (URL as unknown as { revokeObjectURL: () => void }).revokeObjectURL = () => undefined;
  return fake;
}

// The module reaches the browser through ./api, which reads the address bar as it loads; `install`
// below replaces this with the window a particular test wants.
install({});
const { createSpeaker, speechBudgetMs, voiceFor, whenVoicesReady, SPEECH_START_MS, VOICES_WAIT_MS } = await import("./voice");
type Synthesiser = import("./voice").Synthesiser;

const settle = async (ms: number) => {
  await vi.advanceTimersByTimeAsync(ms);
};

afterEach(() => {
  vi.useRealTimers();
});

describe("the reading budget", () => {
  it("gives a sentence the time to be read at an unhurried pace, and no more", () => {
    const short = speechBudgetMs("Yes, all eleven went out.");
    const long = speechBudgetMs("Yes, all eleven went out. ".repeat(10));
    expect(short).toBeGreaterThan(SPEECH_START_MS);
    expect(long).toBeGreaterThan(short * 2);
    // Ten seconds of speech is not cut off at five.
    expect(speechBudgetMs("x".repeat(110))).toBeGreaterThan(10_000);
  });
});

describe("a synthesiser with no voices yet", () => {
  it("waits for the list to fill rather than speaking the first sentence into a null voice", async () => {
    vi.useFakeTimers();
    const fake = install({ voices: false });
    const speaking: boolean[] = [];
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: (on) => speaking.push(on) });
    speaker.say("Eleven invoices went out.");
    await settle(10);
    expect(fake.spoken).toEqual([]);
    fake.arrive();
    await settle(20);
    expect(fake.spoken).toEqual(["Eleven invoices went out."]);
    expect(speaking).toEqual([true, false]);
  });

  it("gives up waiting and speaks in whatever voice the engine has, rather than never speaking", async () => {
    vi.useFakeTimers();
    const fake = install({ voices: false });
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: () => undefined });
    speaker.say("Eleven invoices went out.");
    await settle(VOICES_WAIT_MS + 20);
    expect(fake.spoken).toEqual(["Eleven invoices went out."]);
  });

  it("picks a voice for the language and is content with none", () => {
    const voices = [{ lang: "ru-RU" }, { lang: "en-GB" }] as SpeechSynthesisVoice[];
    expect(voiceFor(voices, "en-US")?.lang).toBe("en-GB");
    expect(voiceFor(voices, "de-DE")).toBeUndefined();
    expect(voiceFor([], "en-US")).toBeUndefined();
  });

  it("returns at once when the list is already there", async () => {
    const fake = install({});
    await expect(whenVoicesReady(fake.synth as unknown as Synthesiser, VOICES_WAIT_MS)).resolves.toHaveLength(1);
  });
});

describe("a synthesiser that abandons an utterance without a word", () => {
  it("does not let it hold the sentences behind it", async () => {
    vi.useFakeTimers();
    const fake = install({ swallow: [1] });
    const unspoken: string[] = [];
    const speaking: boolean[] = [];
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: (on) => speaking.push(on), onUnspoken: (t) => unspoken.push(t) });
    speaker.say("Eleven invoices went out.");
    speaker.say("Two came back with the wrong VAT line.");
    speaker.say("Shall I redo them?");
    await settle(speechBudgetMs("Two came back with the wrong VAT line.") + 200);
    expect(fake.spoken).toEqual(["Eleven invoices went out.", "Two came back with the wrong VAT line.", "Shall I redo them?"]);
    // The one it swallowed is named, so the page can show it as read rather than heard.
    expect(unspoken).toEqual(["Two came back with the wrong VAT line."]);
    // And the page is told the speaker has stopped, which is what takes it out of "speaking".
    expect(speaking.at(-1)).toBe(false);
  });
});

describe("a browser that will not make a sound until it is tapped", () => {
  it("says so instead of going quiet, and speaks everything after the tap", async () => {
    vi.useFakeTimers();
    const fake = install({ silent: true });
    const blocked: boolean[] = [];
    const unspoken: string[] = [];
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: () => undefined, onBlocked: (on) => blocked.push(on), onUnspoken: (t) => unspoken.push(t) });
    speaker.say("Eleven invoices went out.");
    await settle(SPEECH_START_MS + 50);
    expect(blocked).toContain(true);
    expect(unspoken).toEqual(["Eleven invoices went out."]);

    fake.tap();
    speaker.unlock();
    speaker.say("Two came back with the wrong VAT line.");
    await settle(50);
    expect(fake.spoken.at(-1)).toBe("Two came back with the wrong VAT line.");
    expect(blocked.at(-1)).toBe(false);
  });
});

describe("an engine that throws instead of answering", () => {
  it("counts as one sentence unspoken and not as the end of the answer", async () => {
    vi.useFakeTimers();
    const fake = install({});
    (fake.synth as unknown as { speak: (u: unknown) => void }).speak = (u: unknown) => {
      const utterance = u as { text: string; onend: null | (() => void) };
      if (utterance.text === "Two came back.") throw new Error("synthesis-unavailable");
      spokenByHand.push(utterance.text);
      setTimeout(() => utterance.onend?.(), 1);
    };
    const spokenByHand: string[] = [];
    const unspoken: string[] = [];
    let speaking = true;
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: (on) => (speaking = on), onUnspoken: (t) => unspoken.push(t) });
    speaker.say("All eleven went out.");
    speaker.say("Two came back.");
    speaker.say("Shall I redo them?");
    await settle(200);
    expect(spokenByHand).toEqual(["All eleven went out.", "Shall I redo them?"]);
    expect(unspoken).toEqual(["Two came back."]);
    expect(speaking).toBe(false);
  });
});

describe("whatever the engine does", () => {
  it("never leaves the page in speaking with nothing being spoken", async () => {
    vi.useFakeTimers();
    install({ ends: 0, silent: false });
    let speaking = false;
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: (on) => (speaking = on) });
    speaker.say("One.");
    speaker.say("Two.");
    await settle(10);
    expect(speaking).toBe(true);
    await settle(speechBudgetMs("One.") * 2 + 500);
    expect(speaking).toBe(false);
  });

  it("stops everything at a barge-in without waiting for the engine to agree", async () => {
    vi.useFakeTimers();
    const fake = install({ ends: 0 });
    let speaking = false;
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: (on) => (speaking = on) });
    speaker.say("One.");
    speaker.say("Two.");
    await settle(10);
    speaker.cancel();
    expect(speaking).toBe(false);
    await settle(speechBudgetMs("One.") + 500);
    expect(fake.spoken).toEqual(["One."]);
  });
});

// ── one answer at a time ──────────────────────────────────────────────────────────────────────
//
// The complaint these answer, in the owner's words: the first answer was spoken about fifteen
// seconds late, usually only the second one was read properly, and when the second answer came the
// page played the previous one and the current one. That is one bug with three faces. A voice that
// has to be built, or that renders slower than speech, leaves the first answer's sentences queued
// and its clips half fetched; the operator hears nothing and asks again; and the old audio arrives
// with the new audio behind it. A clip belongs to an answer, an answer ends when the next one
// begins, and a clip of an answer that has ended is never played.
describe("a second answer while the first is still being read", () => {
  it("ends the first one where it stands rather than reading both", async () => {
    vi.useFakeTimers();
    const fake = install({ ends: 0 }); // nothing ever finishes: the slow first answer, exactly
    const unspoken: string[] = [];
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: () => undefined, onUnspoken: (t) => unspoken.push(t) });
    speaker.say("The first answer, first sentence.", "run-1");
    speaker.say("The first answer, second sentence.", "run-1");
    await settle(10);
    expect(fake.spoken).toEqual(["The first answer, first sentence."]);

    speaker.say("The second answer.", "run-2");
    await settle(10);
    expect(fake.spoken).toEqual(["The first answer, first sentence.", "The second answer."]);
    // What was queued behind the abandoned answer is marked, not read out later.
    expect(unspoken).toContain("The first answer, second sentence.");
    // And no amount of waiting brings the rest of the first answer back.
    await settle(120_000);
    expect(fake.spoken.filter((said) => said.startsWith("The first answer"))).toEqual(["The first answer, first sentence."]);
  });

  it("marks a straggling sentence of a stopped answer rather than reading it late", async () => {
    vi.useFakeTimers();
    const fake = install({});
    const unspoken: string[] = [];
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: () => undefined, onUnspoken: (t) => unspoken.push(t) });
    speaker.say("The answer being read.", "run-1");
    await settle(10);
    speaker.cancel();
    speaker.say("A sentence the run wrote as it was stopped.", "run-1");
    await settle(200);
    expect(fake.spoken).toEqual(["The answer being read."]);
    expect(unspoken).toEqual(["A sentence the run wrote as it was stopped."]);
  });

  it("does not let a sentence from two answers back take the live one over", async () => {
    // One slot held only the most recently superseded answer, so with A -> B -> C a late sentence
    // of A was not recognised as abandoned: it opened its own turn, which stopped C and read A out.
    vi.useFakeTimers();
    const fake = install({});
    const unspoken: string[] = [];
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: () => undefined, onUnspoken: (t) => unspoken.push(t) });
    speaker.say("The first answer.", "run-1");
    await settle(10);
    speaker.say("The second answer.", "run-2");
    await settle(10);
    speaker.say("The third answer.", "run-3");
    await settle(10);
    expect(fake.spoken).toEqual(["The first answer.", "The second answer.", "The third answer."]);

    speaker.say("A straggling sentence of the first answer.", "run-1");
    await settle(200);
    expect(fake.spoken).toEqual(["The first answer.", "The second answer.", "The third answer."]);
    expect(unspoken).toEqual(["A straggling sentence of the first answer."]);
    // The live answer is still the live answer: it goes on being read.
    speaker.say("Still the third answer.", "run-3");
    await settle(200);
    expect(fake.spoken).toContain("Still the third answer.");
  });

  it("is moved on by the run starting, before that run has written a word", async () => {
    vi.useFakeTimers();
    const fake = install({ ends: 0 });
    const unspoken: string[] = [];
    const speaker = createSpeaker({ engine: () => "here", lang: "en-US", onSpeaking: () => undefined, onUnspoken: (t) => unspoken.push(t) });
    speaker.say("One of the first answer.", "run-1");
    speaker.say("Two of the first answer.", "run-1");
    await settle(10);
    speaker.beginTurn("run-2");
    expect(unspoken).toEqual(["Two of the first answer."]);
    await settle(120_000);
    expect(fake.spoken).toEqual(["One of the first answer."]);
  });
});

describe("which engine reads an answer", () => {
  it("is asked once per answer, so a voice that is still loading only costs the answer it loads under", async () => {
    vi.useFakeTimers();
    const fake = install({});
    const asked: number[] = [];
    let loading = true;
    const posted: { url: string; body: unknown }[] = [];
    (globalThis as Record<string, unknown>).fetch = (url: string, init: { body: string }) => {
      posted.push({ url, body: JSON.parse(init.body) });
      // Not ok, so the speaker gives up on this sentence at once: what is asserted here is which
      // engine was asked, not what came back.
      return Promise.resolve({ ok: false, body: null, headers: { get: () => "" } });
    };
    const speaker = createSpeaker({
      engine: () => {
        asked.push(1);
        return loading ? "here" : "server";
      },
      lang: "en-US",
      onSpeaking: () => undefined,
    });
    speaker.say("The first answer, read by the browser.", "run-1");
    speaker.say("Still the first answer, still the browser.", "run-1");
    await settle(200);
    expect(asked.length).toBe(1);
    expect(fake.spoken.length).toBe(2);
    expect(posted).toEqual([]);

    loading = false; // the voice finished loading between the two answers
    speaker.say("The second answer, read by the voice.", "run-2");
    await settle(200);
    expect(asked.length).toBe(2);
    expect(fake.spoken.length).toBe(2);
    expect(posted.length).toBe(1);
    expect(posted[0].url).toContain("/api/companion/tts");
    expect(posted[0].body).toEqual({ text: "The second answer, read by the voice.", turn: "run-2" });
  });

  it("reports how long the answer waited for its first sound, once", async () => {
    vi.useFakeTimers();
    install({});
    const heard: [string, number][] = [];
    const speaker = createSpeaker({
      engine: () => "here",
      lang: "en-US",
      onSpeaking: () => undefined,
      onFirstAudio: (turn, ms) => heard.push([turn, ms]),
    });
    speaker.say("One.", "run-1");
    speaker.say("Two.", "run-1");
    await settle(200);
    expect(heard.length).toBe(1);
    expect(heard[0][0]).toBe("run-1");
    speaker.say("The next answer.", "run-2");
    await settle(200);
    expect(heard.map(([turn]) => turn)).toEqual(["run-1", "run-2"]);
  });
});
