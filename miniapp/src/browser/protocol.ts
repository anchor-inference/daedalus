// The live view's wire format between the app and the host (and, byte for byte, the host and the
// browser daemon, which only relays): binary WebSocket frames whose first byte is the type, big-endian
// integers after it. `docs/architecture/browser.md` (Live views) is the specification; the golden frames
// in `testdata/frames.json` are what the daemon, the host and this file are all tested against.
//
// The types start at 0x21 so a view frame sent down a terminal's socket (0x01–0x13) is refused there
// rather than misread, and the other way round.
//
// JSON goes out compact with its keys in the order the specification lists them: the golden frames are
// bytes, and a codec that wrote `{"max_w":…,"tier":…}` would be right in meaning and wrong on the wire.

export const VIEW = {
  FRAME: 0x21,
  EVENT: 0x22,
  ATTACH: 0x30,
  ACK: 0x31,
  VIEW: 0x32,
  INPUT: 0x33,
} as const;

/** The largest INPUT the host relays; a longer text is sent in pieces. */
export const MAX_INPUT_BYTES = 4096;
/** The daemon's own ceiling on one `text` input, in characters. */
export const MAX_TEXT_CHARS = 1000;

import type { BrowserStep } from "../api";

export type Tier = "live" | "thumb";

/** What a frame shows: its pixels, the viewport's size in CSS pixels, and where the page was. */
export type FrameMeta = {
  tab: string;
  tier: Tier;
  /** The image, in pixels. */
  w: number;
  h: number;
  /** The viewport, in CSS pixels: the image is `w / vw` pixels per CSS pixel. */
  vw: number;
  vh: number;
  scroll_x: number;
  scroll_y: number;
  offset_top: number;
  page_scale: number;
  /** Capture time, milliseconds since the epoch. */
  ts: number;
};

export type ControlOwner = "agent" | "human" | "paused";

/** Who drives, as this client sees it: `holder` is "you", "other", or null while nobody holds it. */
export type ViewControl = { owner: ControlOwner; holder: "you" | "other" | null; until: number | null; reason: string };

export type ViewTab = { id: string; url: string; title: string; favicon_url: string; loading: boolean; active?: boolean };

export type Point = { x: number; y: number };
export type Box = { x: number; y: number; w: number; h: number };

export type ActionKind = "click" | "double_click" | "right_click" | "hover" | "type" | "press" | "select" | "check" | "uncheck" | "scroll" | "drag" | "upload";

export type ActionEvent = {
  type: "action";
  id: string;
  group: string;
  tab: string;
  actor: "agent" | "operator";
  kind: ActionKind | string;
  point?: Point | null;
  box?: Box | null;
  name: string;
  element: string;
  text_len?: number;
  keys?: string;
  at: number;
};

export type ViewEvent =
  | { type: "hello"; client_id: string; read_only: boolean; tier: Tier; group: { id: string; profile: string; viewport: { w: number; h: number } }; tab_id: string; control: ViewControl; fps_cap: number }
  | { type: "tabs"; tabs: ViewTab[]; active: string }
  | ({ type: "tab" } & ViewTab)
  | { type: "viewers"; count: number; others: { id: string; kind: string; label: string }[] }
  | ActionEvent
  | { type: "action_done"; id: string; ok: boolean; effects?: Record<string, unknown>; error?: string }
  | ({ type: "control"; group?: string } & ViewControl)
  | { type: "dialog"; state?: "opened" | "closed"; tab_id?: string; dialog_type?: string; message?: string; default_prompt?: string; [k: string]: unknown }
  | { type: "download"; download: { id: string; name: string; size: number; state: string } }
  | { type: "needs_you"; reason: string; what: string; url: string; tab_id?: string; by?: "daemon" | "agent" }
  | { type: "error"; code: string; message: string }
  | { type: "ping"; at: number }
  | WorkflowEvent
  | CopiedEvent;

/**
 * The group's recording of the operator's own steps (docs/architecture/browser.md, Recording a
 * person's steps): started, each step as it is taken (`replaces` names a step it takes the place of),
 * and stopped — without its steps, which the host keeps.
 */
export type WorkflowEvent = { type: "workflow"; state: "started" | "step" | "stopped"; id: string; recording?: boolean; steps?: number; step?: BrowserStep; replaces?: number; values?: string; reason?: string };

/**
 * The answer to a copy, to this client alone: the page's selected text, `truncated` when it was cut to
 * what one frame carries, `withheld` when the focus is on a password field, whose value never leaves
 * the page this way; `error` when the page could not be read (a dialog stops it).
 */
export type CopiedEvent = { type: "copied"; id: string; text: string; truncated?: boolean; withheld?: boolean; error?: string };

export type ServerFrame = { kind: "frame"; frameNo: number; meta: FrameMeta; image: Uint8Array } | { kind: "event"; event: ViewEvent };

export type Attach = { tier: Tier; tab?: string; max_w: number; max_h: number; dpr?: number; quality?: number };
export type ViewChange = { tier?: Tier; tab?: string; max_w?: number; max_h?: number; dpr?: number; quality?: number; hidden?: boolean };

export type Mods = number;
export type InputMessage =
  | { t: "mouse"; type: "down" | "up" | "move"; x: number; y: number; button: "left" | "middle" | "right" | "none"; clicks: number; mods: Mods }
  | { t: "wheel"; x: number; y: number; dx: number; dy: number; mods: Mods }
  | { t: "key"; type: "down" | "up"; key: string; code: string; key_code: number; text?: string; mods: Mods }
  | { t: "text"; text: string }
  | { t: "touch"; type: "start" | "move" | "end" | "cancel"; points: { x: number; y: number; id: number }[] }
  | { t: "nav"; action: "url" | "back" | "forward" | "reload"; url?: string }
  | { t: "copy"; id: string };

export class ProtocolError extends Error {}

const encoder = new TextEncoder();
const decoder = new TextDecoder("utf-8", { fatal: true });

function bytesOf(data: ArrayBuffer | Uint8Array): Uint8Array {
  return data instanceof Uint8Array ? data : new Uint8Array(data);
}

function json(bytes: Uint8Array): unknown {
  try {
    return JSON.parse(decoder.decode(bytes));
  } catch {
    throw new ProtocolError("not JSON");
  }
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** One frame from the host. Anything that is not exactly a FRAME or an EVENT is refused. */
export function decodeServerFrame(data: ArrayBuffer | Uint8Array): ServerFrame {
  const bytes = bytesOf(data);
  if (bytes.length < 1) throw new ProtocolError("empty frame");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (bytes[0] === VIEW.FRAME) {
    if (bytes.length < 7) throw new ProtocolError("short frame header");
    const frameNo = view.getUint32(1);
    if (frameNo === 0) throw new ProtocolError("frame 0");
    const metaLen = view.getUint16(5);
    if (7 + metaLen > bytes.length) throw new ProtocolError("meta longer than the frame");
    const meta = json(bytes.subarray(7, 7 + metaLen));
    if (!isObject(meta)) throw new ProtocolError("meta is not an object");
    const image = bytes.subarray(7 + metaLen);
    if (image.length === 0) throw new ProtocolError("frame without an image");
    return { kind: "frame", frameNo, meta: meta as FrameMeta, image };
  }
  if (bytes[0] === VIEW.EVENT) {
    const event = json(bytes.subarray(1));
    if (!isObject(event) || typeof event.type !== "string") throw new ProtocolError("event without a type");
    return { kind: "event", event: event as ViewEvent };
  }
  throw new ProtocolError(`unknown frame type 0x${bytes[0].toString(16)}`);
}

/** A JSON body with its keys in `order` (the specification's), leaving out what is undefined. */
function ordered(value: Record<string, unknown>, order: readonly string[]): string {
  const out: Record<string, unknown> = {};
  for (const k of order) if (value[k] !== undefined) out[k] = value[k];
  return JSON.stringify(out);
}

function typed(type: number, body: string): Uint8Array {
  const payload = encoder.encode(body);
  const frame = new Uint8Array(1 + payload.length);
  frame[0] = type;
  frame.set(payload, 1);
  return frame;
}

const VIEW_KEYS = ["tier", "tab", "max_w", "max_h", "dpr", "quality", "hidden"] as const;

export function encodeAttach(a: Attach): Uint8Array {
  return typed(VIEW.ATTACH, ordered(a, VIEW_KEYS));
}

export function encodeView(v: ViewChange): Uint8Array {
  return typed(VIEW.VIEW, ordered(v, VIEW_KEYS));
}

export function encodeAck(frameNo: number): Uint8Array {
  if (!Number.isInteger(frameNo) || frameNo < 1 || frameNo > 0xffffffff) throw new ProtocolError("frame number out of range");
  const frame = new Uint8Array(5);
  frame[0] = VIEW.ACK;
  new DataView(frame.buffer).setUint32(1, frameNo);
  return frame;
}

const INPUT_KEYS: Record<InputMessage["t"], readonly string[]> = {
  mouse: ["t", "type", "x", "y", "button", "clicks", "mods"],
  wheel: ["t", "x", "y", "dx", "dy", "mods"],
  key: ["t", "type", "key", "code", "key_code", "text", "mods"],
  text: ["t", "text"],
  touch: ["t", "type", "points"],
  nav: ["t", "action", "url"],
  copy: ["t", "id"],
};

/** One INPUT frame. A text too long for one is the caller's to split (`splitText`). */
export function encodeInput(i: InputMessage): Uint8Array {
  const frame = typed(VIEW.INPUT, ordered(i as Record<string, unknown>, INPUT_KEYS[i.t]));
  if (frame.length > MAX_INPUT_BYTES + 1) throw new ProtocolError("input too long");
  return frame;
}

/**
 * Text cut into pieces the daemon takes whole: at most 1000 characters and well under 4 KiB of JSON
 * each, never between the two halves of a surrogate pair — a pasted emoji split in two would reach
 * the page as two broken characters.
 */
export function splitText(text: string, max = MAX_TEXT_CHARS): string[] {
  const chars = [...text];
  const out: string[] = [];
  let piece = "";
  let count = 0;
  let bytes = 0;
  for (const ch of chars) {
    // JSON escapes can grow a character to six bytes (\u0001); a conservative budget keeps every
    // piece inside the frame limit whatever it holds.
    const cost = ch.length > 1 ? 12 : ch < " " || ch === '"' || ch === "\\" ? 6 : encoder.encode(ch).length;
    if (piece && (count >= max || bytes + cost > MAX_INPUT_BYTES - 32)) {
      out.push(piece);
      piece = "";
      count = 0;
      bytes = 0;
    }
    piece += ch;
    count += 1;
    bytes += cost;
  }
  if (piece) out.push(piece);
  return out;
}
