// The app's half of the side panel's bridge (the extension's half is `extension/src/bridge.ts`).
//
// The app is framed by the extension's panel page. It announces itself to its parent with a nonce of
// its own, addressed to the extension's origin so no other parent ever hears it, and takes the
// answer only from that parent, from that origin, with that nonce echoed: a page that frames the app
// cannot pose as the extension, and could not frame it anyway (the host's frame-ancestors names the
// extension alone). The answer carries the panel's credential and a port; everything after goes on
// the port.

export const EXTENSION_ID = "lkhhklicnjlplokpadpccndipomikoaj";
export const EXTENSION_ORIGIN = `chrome-extension://${EXTENSION_ID}`;
export const PANEL_PROTO = 1;
export const READY = "DAEDALUS_PANEL_READY";
export const HELLO = "DAEDALUS_PANEL_HELLO";

export type PanelTab = { id: number; url: string; title: string; readable: boolean };
export type TabOwner = { group: string; session?: string; title: string; paused: boolean };
export type Current = { tab: PanelTab | null; owner: TabOwner | null };
export type PanelEvent = { session: string; kind: "needs_you" | "done"; title: string; waiting: number };

export type Hello = {
  lang: string;
  token: string;
  expires: string;
  current: Current;
  /** The conversation a notification's click asked for, when the panel opened for one. */
  show: string | null;
  profile: string;
  connected: boolean;
  generation: number;
};

export type PageInfo = { tab: number; url: string; title: string };

export interface Shell {
  hello: Hello;
  request<T = unknown>(op: string, args?: Record<string, unknown>): Promise<T>;
  /** Hear the extension: `current` (the active tab changed), `token`, `show`, `event`. */
  on(event: string, listener: (value: any) => void): () => void;
}

export class ShellError extends Error {}

type WindowLike = {
  parent: { postMessage(message: unknown, origin: string): void };
  addEventListener(type: "message", listener: (event: MessageEvent) => void): void;
  removeEventListener(type: "message", listener: (event: MessageEvent) => void): void;
};

function nonce(): string {
  const bytes = new Uint8Array(18);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Announces the app to the extension and resolves with the shell once it answered; rejects after
 * `timeoutMs` when nobody did (the page was opened on its own, outside the panel).
 */
export function connectShell(win: WindowLike = window as unknown as WindowLike, timeoutMs = 4000): Promise<Shell> {
  const mine = nonce();
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      win.removeEventListener("message", onMessage);
      reject(new ShellError("no extension"));
    }, timeoutMs);
    function onMessage(event: MessageEvent) {
      if (event.source !== (win.parent as unknown) || event.origin !== EXTENSION_ORIGIN) return;
      const data = event.data as Record<string, unknown> | null;
      if (!data || data.t !== HELLO || data.proto !== PANEL_PROTO || data.nonce !== mine) return;
      const port = event.ports?.[0];
      if (!port || typeof data.token !== "string") return;
      clearTimeout(timer);
      win.removeEventListener("message", onMessage);
      resolve(shellOn(port, data as unknown as Hello));
    }
    win.addEventListener("message", onMessage);
    win.parent.postMessage({ t: READY, proto: PANEL_PROTO, nonce: mine }, EXTENSION_ORIGIN);
  });
}

/** The shell over its port: requests answered by id, events to whoever listens. */
export function shellOn(port: MessagePort, hello: Hello): Shell {
  let next = 0;
  const waiting = new Map<number, { resolve: (value: any) => void; reject: (error: Error) => void }>();
  const listeners = new Map<string, Set<(value: any) => void>>();
  port.onmessage = (message: MessageEvent) => {
    const data = message.data as Record<string, any> | null;
    if (!data) return;
    if (typeof data.event === "string") {
      for (const listener of listeners.get(data.event) ?? []) listener(data.value);
      return;
    }
    const pending = typeof data.id === "number" ? waiting.get(data.id) : undefined;
    if (!pending) return;
    waiting.delete(data.id);
    if (data.ok) pending.resolve(data.value);
    else pending.reject(new ShellError(String(data.error ?? "failed")));
  };
  port.start?.();
  return {
    hello,
    request(op, args = {}) {
      const id = ++next;
      return new Promise((resolve, reject) => {
        waiting.set(id, { resolve, reject });
        port.postMessage({ id, op, args });
      });
    },
    on(event, listener) {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event)!.add(listener);
      return () => listeners.get(event)?.delete(listener);
    },
  };
}
