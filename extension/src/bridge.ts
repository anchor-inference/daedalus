// The side panel's bridge to the app it frames. The panel page is an extension page and holds the
// privileges (tabs, screenshots, the page's text); the frame is the host's own app at `/app/panel`,
// which draws the conversation and asks for those privileges through here. Nothing else is shared:
// the frame cannot reach `chrome.*`, and the page it shows cannot reach the frame.
//
// The handshake:
// - The frame posts `DAEDALUS_PANEL_READY {proto, nonce}` to its parent, addressed to this
//   extension's origin. It is accepted only from the frame's own window and the host's origin, so a
//   page in another frame or tab cannot pose as the app.
// - The panel answers `DAEDALUS_PANEL_HELLO` with the nonce echoed, the credential and a
//   `MessageChannel` port, addressed to the host's origin alone, so the credential cannot land
//   anywhere else even if the frame navigated away in between.
// - Everything after travels on the port. Each READY (the frame reloaded: a sign-in, a restart of the
//   server) starts a new generation and closes the old port; a late request from a superseded load is
//   dropped instead of acting on a page the operator no longer sees.
//
// This file is pure: the panel page gives it the frame, the handlers and the gesture check, and the
// tests drive it with fakes.

export const PANEL_PROTO = 1;
export const READY = "DAEDALUS_PANEL_READY";
export const HELLO = "DAEDALUS_PANEL_HELLO";

/** What the frame may ask for. Each acts on the panel's own window: its active tab, or a tab of the
 *  agent's groups, never a tab the frame names freely. */
export const OPS = [
  "tab.current",
  "capture.visible",
  "page.text",
  "page.selection",
  "tab.grant",
  "tab.focus",
  "group.pause",
  "open.app",
  "token.refresh",
  "mic.grant",
] as const;
export type Op = (typeof OPS)[number];

/** Reads of the operator's page, and giving a tab away, need the operator's own click in the panel a
 *  moment before: a frame that was taken over still cannot photograph or read a page silently. */
export const GESTURE_OPS: ReadonlySet<Op> = new Set<Op>(["capture.visible", "page.text", "page.selection", "tab.grant", "mic.grant"]);

export type Hello = Record<string, unknown>;

export type BridgeDeps = {
  /** The frame's window as the panel holds it now; null while there is no frame. */
  frame: () => MessageEventSource | null;
  /** The host's origin: the only one a READY is accepted from and a HELLO is sent to. */
  origin: string;
  /** What HELLO carries beside the protocol fields: the credential, the language, the current tab. */
  hello: () => Hello | Promise<Hello>;
  handle: (op: Op, args: Record<string, unknown>) => Promise<unknown>;
  /** Whether the operator clicked or typed in the panel within the last few seconds. */
  gesture: () => boolean;
  channel?: () => MessageChannel;
  post?: (target: MessageEventSource, message: unknown, origin: string, transfer: Transferable[]) => void;
};

const NONCE = /^[A-Za-z0-9_-]{16,128}$/;

export class ShellBridge {
  generation = 0;
  private port: MessagePort | null = null;

  constructor(private readonly deps: BridgeDeps) {}

  /** A window message to the panel page. Returns whether it was taken as the frame's READY. */
  async onWindowMessage(event: Pick<MessageEvent, "source" | "origin" | "data">): Promise<boolean> {
    const frame = this.deps.frame();
    if (!frame || event.source !== frame || event.origin !== this.deps.origin) return false;
    const data = event.data as Record<string, unknown> | null;
    if (!data || data.t !== READY || data.proto !== PANEL_PROTO || typeof data.nonce !== "string" || !NONCE.test(data.nonce)) return false;
    const generation = ++this.generation;
    this.port?.close();
    const channel = this.deps.channel ? this.deps.channel() : new MessageChannel();
    this.port = channel.port1;
    channel.port1.onmessage = (message) => void this.onPortMessage(message.data, generation);
    const hello = await this.deps.hello();
    // A newer READY may have come in while the hello was put together.
    if (generation !== this.generation) {
      channel.port1.close();
      return false;
    }
    const message = { ...hello, t: HELLO, proto: PANEL_PROTO, nonce: data.nonce, generation };
    const post = this.deps.post ?? ((target, value, origin, transfer) => (target as Window).postMessage(value, origin, transfer));
    post(frame, message, this.deps.origin, [channel.port2]);
    return true;
  }

  /** Tell the frame of the current generation something happened: the active tab changed, a newer
   *  credential arrived, a notification asked for a conversation. */
  emit(event: string, value: unknown): void {
    this.port?.postMessage({ event, value });
  }

  close(): void {
    this.generation += 1;
    this.port?.close();
    this.port = null;
  }

  private async onPortMessage(data: unknown, generation: number): Promise<void> {
    if (generation !== this.generation || !this.port) return;
    const port = this.port;
    const request = data as Record<string, unknown> | null;
    if (!request || !Number.isInteger(request.id) || typeof request.op !== "string") return;
    const id = request.id as number;
    const op = request.op as Op;
    const reply = (body: Record<string, unknown>) => {
      if (generation === this.generation) port.postMessage({ id, ...body });
    };
    if (!(OPS as readonly string[]).includes(op)) return reply({ ok: false, error: "unknown" });
    const args = request.args && typeof request.args === "object" && !Array.isArray(request.args) ? (request.args as Record<string, unknown>) : {};
    if (GESTURE_OPS.has(op) && !this.deps.gesture()) return reply({ ok: false, error: "gesture" });
    try {
      reply({ ok: true, value: (await this.deps.handle(op, args)) ?? null });
    } catch (error) {
      reply({ ok: false, error: error instanceof Error ? error.message : String(error) });
    }
  }
}

/** A nonce-shaped random string; the frame makes its own, this is for the tests and the shell's ids. */
export function randomNonce(): string {
  const bytes = new Uint8Array(18);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
