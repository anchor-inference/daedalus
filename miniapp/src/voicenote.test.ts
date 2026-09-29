// The composer's voice note without a microphone: what each control does to the machine, where the
// words land, and that a failed recording is still there afterwards.

import { describe, expect, it } from "vitest";
import { CLOUD, transcriberChoices } from "./asrchoices";
import { IDLE, NoteEvent, NoteState, appendWords, clock, forgetNote, keepNote, keptNote, landWords, noteStep, readRefusal, recordingName } from "./voicenote";

const run = (...events: NoteEvent[]): NoteState => events.reduce(noteStep, IDLE);

describe("the voice note machine", () => {
  it("records, stops and hears", () => {
    expect(run({ type: "start" }).phase).toBe("recording");
    const stopped = run({ type: "start" }, { type: "stop", send: false });
    expect(stopped).toMatchObject({ phase: "transcribing", send: false });
    expect(noteStep(stopped, { type: "heard" })).toEqual(IDLE);
  });

  it("↑ stops and marks the words to go out at once", () => {
    expect(run({ type: "start" }, { type: "stop", send: true })).toMatchObject({ phase: "transcribing", send: true });
  });

  it("✕ throws away a recording, and a transcription on its way", () => {
    expect(run({ type: "start" }, { type: "cancel" })).toEqual(IDLE);
    expect(run({ type: "start" }, { type: "stop", send: false }, { type: "cancel" })).toEqual(IDLE);
  });

  it("a failure keeps the recording's name and waits for the operator", () => {
    const failed = run({ type: "start" }, { type: "stop", send: true }, { type: "failed", error: "HTTP 400: Provider returned 400", kept: "a1b2c3d4e5f6.wav" });
    expect(failed).toMatchObject({ phase: "failed", error: "HTTP 400: Provider returned 400", kept: "a1b2c3d4e5f6.wav" });
    // Nothing but the operator's own choice leaves this state: not a new recording, not a cancel.
    expect(noteStep(failed, { type: "start" })).toBe(failed);
    expect(noteStep(failed, { type: "cancel" })).toBe(failed);
    expect(noteStep(failed, { type: "heard" })).toBe(failed);
  });

  it("a retry keeps the kept name and a second failure without one does not forget it", () => {
    const failed = run({ type: "start" }, { type: "stop", send: false }, { type: "failed", error: "x", kept: "a1b2c3d4e5f6.wav" });
    const again = noteStep(failed, { type: "retry", send: false });
    expect(again).toMatchObject({ phase: "transcribing", error: "", kept: "a1b2c3d4e5f6.wav" });
    expect(noteStep(again, { type: "failed", error: "offline" })).toMatchObject({ phase: "failed", kept: "a1b2c3d4e5f6.wav" });
    expect(noteStep(again, { type: "heard" })).toEqual(IDLE);
  });

  it("attaching or discarding settles a failure", () => {
    const failed = run({ type: "start" }, { type: "stop", send: false }, { type: "failed", error: "x" });
    expect(noteStep(failed, { type: "settled" })).toEqual(IDLE);
  });

  it("a recording found after a reload comes back as a failure to retry", () => {
    expect(run({ type: "restored", error: "waiting", kept: "" })).toMatchObject({ phase: "failed", error: "waiting" });
    expect(noteStep(run({ type: "start" }), { type: "restored", error: "w", kept: "" }).phase).toBe("recording");
  });
});

describe("where the words land", () => {
  const words = "🎙 Voice note, transcribed automatically (wording may be imperfect):\nbuy milk";

  it("an empty field takes the words as they are", () => {
    expect(appendWords("", words)).toBe(words);
    expect(appendWords("   \n", words)).toBe(words);
  });

  it("typed text stays and the words go after it, a blank line apart", () => {
    expect(appendWords("Look at the logs first.  \n", words)).toBe(`Look at the logs first.\n\n${words}`);
  });

  it("nothing heard changes nothing", () => {
    expect(appendWords("draft", "  ")).toBe("draft");
  });

  it("stop leaves the draft and the words in the field; send sends both", () => {
    expect(landWords("typed", "said", false)).toEqual({ text: "typed\n\nsaid", send: false });
    expect(landWords("typed", "said", true)).toEqual({ text: "typed\n\nsaid", send: true });
  });
});

describe("the pieces around it", () => {
  it("shows the time as minutes and seconds", () => {
    expect(clock(0)).toBe("0:00");
    expect(clock(7.9)).toBe("0:07");
    expect(clock(83)).toBe("1:23");
    expect(clock(724)).toBe("12:04");
  });

  it("reads the host's refusal in either shape", () => {
    expect(readRefusal(502, { detail: { message: "HTTP 400: Provider returned 400", recording: "a1b2c3d4e5f6.wav" } })).toEqual({ error: "HTTP 400: Provider returned 400", kept: "a1b2c3d4e5f6.wav" });
    expect(readRefusal(409, { detail: "speech-to-text is not set up" })).toEqual({ error: "speech-to-text is not set up", kept: "" });
    expect(readRefusal(500, null)).toEqual({ error: "HTTP 500", kept: "" });
  });

  it("names an attached recording by its type and time", () => {
    const at = new Date("2026-09-29T10:11:12Z");
    expect(recordingName("audio/webm;codecs=opus", at)).toBe("voice-note-20260929-101112.webm");
    expect(recordingName("audio/mp4", at)).toBe("voice-note-20260929-101112.m4a");
  });

  it("keeps a recording for its session until it is let go", async () => {
    const blob = new Blob(["abc"], { type: "audio/webm" });
    await keepNote("s1", { blob, error: "HTTP 400", kept: "", at: 1 });
    expect(await keptNote("s2")).toBeNull();
    expect((await keptNote("s1"))?.blob).toBe(blob);
    await forgetNote("s1");
    expect(await keptNote("s1")).toBeNull();
  });
});

describe("the transcriber choices in Settings", () => {
  const models = [
    { id: "gigaam-ru", label: "GigaAM", installed: true },
    { id: "whisper-small", label: "Whisper", installed: false },
    { id: "t-one-ru", label: "T-one", installed: true },
  ];

  it("offers the endpoint and every installed model, and nothing that is not installed", () => {
    expect(transcriberChoices(models, CLOUD).map((o) => o.value)).toEqual([CLOUD, "gigaam-ru", "t-one-ru"]);
  });

  it("the fallback never offers the transcriber itself", () => {
    expect(transcriberChoices(models, "", "gigaam-ru").map((o) => o.value)).toEqual([CLOUD, "t-one-ru"]);
    expect(transcriberChoices(models, "", CLOUD).map((o) => o.value)).toEqual(["gigaam-ru", "t-one-ru"]);
  });

  it("a configured model that was deleted stays visible, marked", () => {
    const choices = transcriberChoices(models, "whisper-small");
    expect(choices.at(-1)).toMatchObject({ value: "whisper-small", missing: true });
  });
});
