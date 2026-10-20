// The two ways a relay reaches its daemon. Both carry the same frames; they differ only in how they
// open, and in what their failures mean to the operator.
//
// - Native: Chrome starts `browserd bridge` through native messaging, and the bridge forwards frames
//   to the `user` daemon on this computer. An open port also keeps the service worker alive.
// - Server: a WebSocket to `wss://<server>/api/browser/relay`. The browser's WebSocket cannot set a
//   header, so the device token travels in the first `hello` instead.

import { NATIVE_HOST, type ExtensionFrame } from "./protocol.js";

/** Why a transport closed, in the terms the side panel explains: an error code from the dictionary. */
export type CloseReason =
  | "not_running"
  | "not_registered"
  | "server_unreachable"
  | "pairing_expired"
  | "busy"
  | "lost";

export interface Transport {
  send(frame: ExtensionFrame): void;
  close(): void;
}

export interface TransportHandlers {
  onFrame(frame: unknown): void;
  onClose(reason: CloseReason, detail?: string): void;
}

export type TransportConfig = { kind: "native" } | { kind: "server"; relayUrl: string };

export type TransportFactory = (config: TransportConfig, handlers: TransportHandlers) => Transport;

/**
 * What a native-messaging disconnect means. Chrome reports it only as `runtime.lastError`'s text,
 * which is stable English across versions: "Specified native messaging host not found." when no
 * manifest names the host, "Access to the specified native messaging host is forbidden." when the
 * manifest exists but does not list this extension's origin (both are a registration the launcher
 * must fix), and "Native host has exited." when the bridge ran and ended, which it does when the
 * daemon is not there to connect to.
 */
export function nativeCloseReason(message: string | undefined): CloseReason {
  const text = (message ?? "").toLowerCase();
  if (text.includes("not found") || text.includes("forbidden")) return "not_registered";
  return "not_running";
}

export function openNative(handlers: TransportHandlers): Transport {
  const port = chrome.runtime.connectNative(NATIVE_HOST);
  let closed = false;
  port.onMessage.addListener((message) => handlers.onFrame(message));
  port.onDisconnect.addListener(() => {
    if (closed) return;
    closed = true;
    handlers.onClose(nativeCloseReason(chrome.runtime.lastError?.message), chrome.runtime.lastError?.message);
  });
  return {
    send(frame) {
      if (!closed) port.postMessage(frame);
    },
    close() {
      if (closed) return;
      closed = true;
      port.disconnect();
    },
  };
}

// The close codes the host uses on the relay socket, beside the `bye` frame it sends first: the
// frame says why, and the code says it again for a client that never got the frame because the
// socket closed under it.
export const CLOSE_UNAUTHORIZED = 4401;
export const CLOSE_BUSY = 4409;

export function openServer(relayUrl: string, handlers: TransportHandlers): Transport {
  const socket = new WebSocket(relayUrl);
  let opened = false;
  let closed = false;
  const queue: string[] = [];
  socket.onopen = () => {
    opened = true;
    for (const text of queue.splice(0)) socket.send(text);
  };
  socket.onmessage = (event) => {
    let frame: unknown;
    try {
      frame = JSON.parse(String(event.data));
    } catch {
      return;
    }
    handlers.onFrame(frame);
  };
  socket.onclose = (event) => {
    if (closed) return;
    closed = true;
    if (event.code === CLOSE_UNAUTHORIZED) handlers.onClose("pairing_expired");
    else if (event.code === CLOSE_BUSY) handlers.onClose("busy", event.reason);
    else handlers.onClose(opened ? "lost" : "server_unreachable");
  };
  return {
    send(frame) {
      if (closed) return;
      const text = JSON.stringify(frame);
      if (opened) socket.send(text);
      else queue.push(text);
    },
    close() {
      if (closed) return;
      closed = true;
      socket.close(1000);
    },
  };
}

export const openTransport: TransportFactory = (config, handlers) =>
  config.kind === "native" ? openNative(handlers) : openServer(config.relayUrl, handlers);

/**
 * The server's origin from what the operator typed: a bare name, a name with a port, or a full
 * address. Only HTTPS is accepted, because the device token travels over it; plain HTTP is allowed
 * for a loopback address alone, where there is no wire to read it from.
 */
export function normaliseServer(input: string): string | null {
  let text = input.trim();
  if (!text) return null;
  if (!/^[a-z]+:\/\//i.test(text)) text = `https://${text}`;
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return null;
  }
  const loopback = url.hostname === "localhost" || url.hostname === "127.0.0.1" || url.hostname === "[::1]";
  if (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) return null;
  if (url.username || url.password) return null;
  return url.origin;
}

export function relayUrl(origin: string): string {
  return origin.replace(/^http/, "ws") + "/api/browser/relay";
}

export function pairUrl(origin: string): string {
  return origin + "/api/browser/pair";
}
