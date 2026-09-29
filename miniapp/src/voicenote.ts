// A voice note in the composer, as a small machine: recording, transcribing, failed, and back to
// nothing. The rule it exists for is that a recording is never lost. The operator talked for several
// minutes, the endpoint answered 400, and the page threw the audio away with the error; everything
// here keeps the recording until its words are in the field or the operator has let it go himself.
//
// The machine is pure so the composer's behaviour can be read back without a microphone. The
// recording itself is kept in IndexedDB while it waits, so a reload, a crash or a closed tab finds it
// again; where IndexedDB is not there (a private window, a test) it is kept in memory for the page.

export type NotePhase = "idle" | "recording" | "transcribing" | "failed";

export type NoteState = {
  phase: NotePhase;
  /** The words go out as soon as they arrive: ↑ was pressed, or the autosend setting is on. */
  send: boolean;
  /** Why the last attempt failed, in the host's words. */
  error: string;
  /** The host's name for the recording it kept after a failure; a retry sends this instead of the audio. */
  kept: string;
};

export const IDLE: NoteState = { phase: "idle", send: false, error: "", kept: "" };

export type NoteEvent =
  | { type: "start" }
  | { type: "stop"; send: boolean }
  | { type: "cancel" }
  | { type: "heard" }
  | { type: "failed"; error: string; kept?: string }
  | { type: "retry"; send: boolean }
  | { type: "settled" }
  | { type: "restored"; error: string; kept: string };

/** One step. An event that makes no sense in the current phase changes nothing. */
export function noteStep(state: NoteState, event: NoteEvent): NoteState {
  switch (event.type) {
    case "start":
      // Not from "failed": a new recording started over an unsettled one would bury it.
      return state.phase === "idle" ? { ...IDLE, phase: "recording" } : state;
    case "stop":
      return state.phase === "recording" ? { ...state, phase: "transcribing", send: event.send } : state;
    case "cancel":
      return state.phase === "recording" || state.phase === "transcribing" ? IDLE : state;
    case "heard":
      return state.phase === "transcribing" ? IDLE : state;
    case "failed":
      return state.phase === "transcribing" ? { ...state, phase: "failed", error: event.error, kept: event.kept || state.kept } : state;
    case "retry":
      return state.phase === "failed" ? { ...state, phase: "transcribing", error: "", send: event.send } : state;
    case "settled":
      return state.phase === "failed" ? IDLE : state;
    case "restored":
      return state.phase === "idle" ? { phase: "failed", send: false, error: event.error, kept: event.kept } : state;
  }
}

/** The draft with the words after it. What was typed stays where it was; the note goes underneath. */
export function appendWords(draft: string, words: string): string {
  const said = words.trim();
  if (!said) return draft;
  if (!draft.trim()) return said;
  return `${draft.trimEnd()}\n\n${said}`;
}

/** What the composer does with the words: the text it now holds, and whether that text goes out. */
export function landWords(draft: string, words: string, send: boolean): { text: string; send: boolean } {
  return { text: appendWords(draft, words), send };
}

/** The elapsed time as the bar shows it: 0:07, 1:23, 12:04. */
export function clock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/** The host's refusal, as a message and the name of the recording it kept, whatever shape it came in. */
export function readRefusal(status: number, body: unknown): { error: string; kept: string } {
  const detail = body && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined;
  if (detail && typeof detail === "object") {
    const d = detail as { message?: unknown; recording?: unknown };
    return { error: String(d.message ?? "") || `HTTP ${status}`, kept: typeof d.recording === "string" ? d.recording : "" };
  }
  return { error: typeof detail === "string" && detail ? detail : `HTTP ${status}`, kept: "" };
}

/** The file a recording becomes when it is attached instead of transcribed. */
export function recordingName(type: string, at: Date): string {
  const ext = /wav/.test(type) ? "wav" : /mp4|aac|m4a/.test(type) ? "m4a" : /ogg/.test(type) ? "ogg" : "webm";
  const stamp = at.toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-");
  return `voice-note-${stamp}.${ext}`;
}

// ── keeping the recording while it waits ──

export type KeptNote = { blob: Blob; error: string; kept: string; at: number };

const DB = "daedalus.voicenotes";
const STORE = "notes";
const memory = new Map<string, KeptNote>();

function open(): Promise<IDBDatabase | null> {
  return new Promise((resolve) => {
    try {
      if (typeof indexedDB === "undefined") return resolve(null);
      const request = indexedDB.open(DB, 1);
      request.onupgradeneeded = () => request.result.createObjectStore(STORE);
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => resolve(null);
      request.onblocked = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
}

async function withStore<T>(mode: IDBTransactionMode, work: (store: IDBObjectStore) => IDBRequest | null, fallback: () => T): Promise<T> {
  const db = await open();
  if (!db) return fallback();
  return new Promise<T>((resolve) => {
    try {
      const tx = db.transaction(STORE, mode);
      const request = work(tx.objectStore(STORE));
      tx.oncomplete = () => {
        db.close();
        resolve((request?.result as T) ?? fallback());
      };
      tx.onerror = tx.onabort = () => {
        db.close();
        resolve(fallback());
      };
    } catch {
      db.close();
      resolve(fallback());
    }
  });
}

/** Keep a session's waiting recording. Memory always holds it too, so a failed write loses nothing this page has. */
export async function keepNote(sessionId: string, note: KeptNote): Promise<void> {
  memory.set(sessionId, note);
  await withStore("readwrite", (store) => store.put(note, sessionId), () => undefined);
}

/** The recording a session left waiting, if any. */
export async function keptNote(sessionId: string): Promise<KeptNote | null> {
  const here = memory.get(sessionId);
  if (here) return here;
  const stored = await withStore<KeptNote | null>("readonly", (store) => store.get(sessionId), () => null);
  return stored && stored.blob instanceof Blob ? stored : null;
}

/** Let the waiting recording go: its words are in the field, or it was attached or discarded. */
export async function forgetNote(sessionId: string): Promise<void> {
  memory.delete(sessionId);
  await withStore("readwrite", (store) => store.delete(sessionId), () => undefined);
}
