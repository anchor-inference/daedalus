// The voice session outlives what is drawn over it.
//
// Every other claim this unit makes rests on this one: the operator can open a transcript in the
// middle of an answer and the answer goes on being read out, because the recogniser, the speaker and
// the stream are not owned by the thing that was on the screen a moment ago. That is a statement
// about object lifetimes, and the only honest way to check it is to count the objects — which is
// what `parts()` is for — and to keep handing the same session sentences across the change.

import { describe, expect, it, vi } from "vitest";

// The module reaches the browser API through ./api, which reads the address bar as it loads.
(globalThis as unknown as { window: unknown }).window = { location: { search: "", pathname: "/", hash: "" }, history: { replaceState: () => undefined } };

const { createVoiceSession, holdVoiceSession, resetVoiceSession, voiceSession } = await import("./voicesession");
type Session = ReturnType<typeof createVoiceSession>;

/** One event stream the test pushes frames into, and which never ends on its own. */
function pushableStream() {
  const waiting: ((value: { value?: Uint8Array; done: boolean }) => void)[] = [];
  const queued: { value?: Uint8Array; done: boolean }[] = [];
  const encoder = new TextEncoder();
  return {
    opened: 0,
    push(event: string, data: Record<string, unknown>) {
      const chunk = { value: encoder.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`), done: false };
      const next = waiting.shift();
      if (next) next(chunk);
      else queued.push(chunk);
    },
    response() {
      this.opened += 1;
      return {
        body: {
          getReader: () => ({
            read: () =>
              new Promise<{ value?: Uint8Array; done: boolean }>((resolve) => {
                const ready = queued.shift();
                if (ready) resolve(ready);
                else waiting.push(resolve);
              }),
          }),
        },
      } as unknown as Response;
    },
  };
}

/** A speaker that records what it was told, and counts how many of itself were ever built. */
function speakers() {
  const built: { said: [string, string | undefined][]; turns: string[]; cancels: number; unlocks: number }[] = [];
  const make = () => {
    const it = { said: [] as [string, string | undefined][], turns: [] as string[], cancels: 0, unlocks: 0 };
    built.push(it);
    return {
      spy: {
        say: (text: string, turn?: string) => it.said.push([text, turn]),
        beginTurn: (turn: string) => it.turns.push(turn),
        cancel: () => {
          it.cancels += 1;
        },
        unlock: () => {
          it.unlocks += 1;
        },
        stop: () => undefined,
      },
      it,
    };
  };
  return { built, make };
}

type Made = { session: Session; stream: ReturnType<typeof pushableStream>; built: ReturnType<typeof speakers>["built"]; listeners: number[]; posted: string[] };

function make(over: Record<string, unknown> = {}): Made {
  const stream = pushableStream();
  const loads = pushableStream();
  const kit = speakers();
  const listeners: number[] = [];
  const posted: string[] = [];
  const session = createVoiceSession({
    fetch: ((url: string) => Promise.resolve(url.includes("/api/companion/stream") ? stream.response() : loads.response())) as unknown as typeof fetch,
    authHeaders: () => ({}),
    post: (path: string) => {
      posted.push(path);
      return Promise.resolve({});
    },
    makeSpeaker: () => kit.make().spy,
    makeMeter: () => ({ start: () => Promise.resolve(), stop: () => undefined }),
    makeRecognition: () => {
      listeners.push(1);
      return { start: () => Promise.resolve(), stop: () => undefined, kind: "recognition" as const };
    },
    makeLocalListener: () => {
      listeners.push(1);
      return { start: () => Promise.resolve(), stop: () => undefined, kind: "local" as const };
    },
    makeRecorder: () => {
      listeners.push(1);
      return { start: () => Promise.resolve(), stop: () => undefined, kind: "recorder" as const };
    },
    localListenSupported: () => true,
    recognitionSupported: () => true,
    sendUtterance: () => Promise.resolve(""),
    lang: "en-GB",
    haptic: () => undefined,
    ...over,
  });
  session.reading({ known: true, serverTts: true, ttsLoading: false, localStt: true });
  session.start();
  session.start();
  return { session, stream, built: kit.built, listeners, posted };
}

/** Let the stream reader run: every frame pushed costs one turn of the microtask queue or two. */
const settle = () => new Promise((r) => setTimeout(r, 0));

describe("the voice session across a change of view", () => {
  it("builds one stream, one speaker and one listener, whatever is on the screen", async () => {
    const { session, stream } = make();
    await session.startMic();
    stream.push("say", { text: "The invoices went out.", turn: "run-1" });
    await settle();
    const before = session.parts();
    expect(before).toEqual({ speakers: 1, listeners: 1, streams: 1 });

    session.dispatch({ type: "center", center: { view: "transcript" } });
    session.dispatch({ type: "center", center: { view: "agent", id: "s-9", title: "Invoice run" } });
    session.dispatch({ type: "center", center: { view: "orb" } });
    await settle();

    expect(session.parts()).toEqual(before);
    expect(stream.opened).toBe(1);
    session.stop();
  });

  it("goes on speaking the answer that was being spoken when the view changed", async () => {
    const { session, stream, built } = make();
    await session.startMic();
    stream.push("status", { state: "thinking", turn: "run-1" });
    stream.push("say", { text: "All eleven went out.", turn: "run-1" });
    await settle();
    session.dispatch({ type: "center", center: { view: "transcript" } });
    stream.push("say", { text: "Two came back.", turn: "run-1" });
    await settle();

    expect(built).toHaveLength(1);
    expect(built[0].said.map(([text]) => text)).toEqual(["All eleven went out.", "Two came back."]);
    expect(built[0].cancels).toBe(0);
    expect(session.state().spoken).toHaveLength(2);
    session.stop();
  });

  it("keeps the half-said utterance and the microphone through the change", async () => {
    const { session } = make();
    await session.startMic();
    session.dispatch({ type: "heard", text: "read me the second" });
    session.dispatch({ type: "center", center: { view: "transcript" } });

    expect(session.state().heard).toBe("read me the second");
    expect(session.state().micOn).toBe(true);
    expect(session.parts().listeners).toBe(1);
    session.stop();
  });

  it("holds a sentence that arrives before the page knows what speaks, and speaks it after", async () => {
    const { session, stream, built } = make();
    await session.startMic();
    session.reading({ known: false, serverTts: false, ttsLoading: false, localStt: false });
    stream.push("say", { text: "Nobody knows who reads this yet.", turn: "run-1" });
    await settle();
    expect(built[0].said).toHaveLength(0);

    session.reading({ known: true, serverTts: true, ttsLoading: false, localStt: true });
    expect(built).toHaveLength(1);
    expect(built[0].said[0][0]).toBe("Nobody knows who reads this yet.");
    session.stop();
  });

  it("reads an answer aloud only on a call or on its stage, and tells the server when a call opens and closes", async () => {
    const { session, stream, built, posted } = make();
    session.dispatch({ type: "center", center: { view: "transcript" } });
    stream.push("say", { text: "An answer to something typed.", turn: "run-1" });
    await settle();
    expect(built).toHaveLength(0);
    expect(session.state().spoken).toEqual(["An answer to something typed."]);

    await session.startMic();
    stream.push("say", { text: "An answer on the call.", turn: "run-2" });
    await settle();
    expect(built[0].said.map(([text]) => text)).toEqual(["An answer on the call."]);
    session.stopMic();
    expect(posted.filter((p) => p === "/api/companion/call")).toHaveLength(2);
    session.stop();
  });

  it("sends a typed message as a text turn and an utterance as a spoken one", async () => {
    const { session, posted } = make();
    await session.type("what is on today");
    await session.send("and tomorrow");
    expect(posted).toEqual(["/api/companion/message", "/api/companion/say"]);
    session.stop();
  });

  it("ends the previous answer where it stands when a new run starts", async () => {
    const { session, stream, built } = make();
    stream.push("status", { state: "thinking", turn: "run-1" });
    stream.push("say", { text: "one.", turn: "run-1" });
    stream.push("status", { state: "thinking", turn: "run-2" });
    await settle();
    expect(built[0].turns).toEqual(["run-1", "run-2"]);
    session.stop();
  });

  it("tells the page an answer is over, so it can re-read the server's timing", async () => {
    const { session, stream } = make();
    const heard = vi.fn();
    const forget = session.onAnswered(heard);
    stream.push("done", {});
    await settle();
    expect(heard).toHaveBeenCalledTimes(1);
    forget();
    stream.push("done", {});
    await settle();
    expect(heard).toHaveBeenCalledTimes(1);
    session.stop();
  });

  it("opens each engine's progress stream once, however often it is asked", () => {
    const opened: string[] = [];
    const { session } = make({
      fetch: ((url: string) => {
        opened.push(String(url));
        return Promise.resolve({ body: { getReader: () => ({ read: () => new Promise(() => undefined) }) } } as unknown as Response);
      }) as unknown as typeof fetch,
    });
    session.watchEngines(true, true);
    session.watchEngines(true, true);
    session.watchEngines(true, false);
    expect(opened.filter((u) => u.includes("/api/stt/progress"))).toHaveLength(1);
    expect(opened.filter((u) => u.includes("/api/tts/progress"))).toHaveLength(1);
    session.stop();
  });
});

describe("the one session the page holds", () => {
  it("survives a component that leaves and comes straight back", async () => {
    resetVoiceSession();
    const first = voiceSession();
    const letGo = holdVoiceSession();
    letGo();
    const again = holdVoiceSession();
    await settle();
    expect(voiceSession()).toBe(first);
    again();
    await settle();
    expect(voiceSession()).not.toBe(first);
    resetVoiceSession();
  });

  it("is let go only when the last holder has", async () => {
    resetVoiceSession();
    const held = voiceSession();
    const one = holdVoiceSession();
    const two = holdVoiceSession();
    one();
    await settle();
    expect(voiceSession()).toBe(held);
    two();
    await settle();
    expect(voiceSession()).not.toBe(held);
    resetVoiceSession();
  });
});
