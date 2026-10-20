// The relay protocol between this extension and a `browserd` running `--backend attach`. Every frame
// is one JSON object with `t`; the daemon's side is in `browserd/internal/relay`, and both sides test
// against the same golden frames. A field added on one side and not the other is a mismatch the
// golden file catches; a change in meaning bumps `PROTO`, and both sides then show "update" instead
// of misbehaving.

export const PROTO = 1;

// The native-messaging host the desktop launcher registers for Chrome. It is the reverse-domain name
// Chrome requires (lowercase letters, digits, dots and underscores), and it names what it starts:
// `browserd bridge`, which carries these frames to the `user` daemon's socket.
export const NATIVE_HOST = "com.daedalus.browser";

export type ErrorBody = { code: string; message: string };

export type TabInfo = {
  id: number;
  window: number;
  url: string;
  title: string;
  opener?: number;
  attached: boolean;
};

export type GroupInfo = { id: string; title?: string; paused?: boolean };

export type TabsOp = "created" | "removed" | "updated" | "joined" | "left" | "group_closed";

export type HelloFrame = {
  t: "hello";
  proto: number;
  ext_version: string;
  chrome_version: string;
  platform: string;
  profile_id: string;
  profile_name: string;
  token?: string;
};

export type ExtensionFrame =
  | HelloFrame
  | { t: "result"; id: number; ok: true; value: unknown }
  | { t: "result"; id: number; ok: false; error: ErrorBody }
  | { t: "event"; session: string; method: string; params: unknown }
  | { t: "tabs"; op: TabsOp; tab?: TabInfo; group?: GroupInfo }
  | { t: "input"; tab: number }
  | { t: "pong"; n: number }
  | { t: "check"; n: number };

export type WelcomeFrame = {
  t: "welcome";
  proto: number;
  daemon_version: string;
  install_name: string;
  install_kind: "native" | "server";
};

export type CdpFrame = { t: "cdp"; id: number; session: string; method: string; params?: Record<string, unknown> };
export type CallFrame = { t: "call"; id: number; op: string; args?: Record<string, unknown> };

export type DaemonFrame =
  | WelcomeFrame
  | CdpFrame
  | CallFrame
  | { t: "ping"; n: number }
  | { t: "check_ok"; n: number; host_seen: boolean }
  | { t: "bye"; reason: string; name?: string; proto?: number };

// Why a daemon (or the host, on the server transport) ends a relay. `busy` carries the `name` of the
// Chrome that holds the relay, and `proto` the version the daemon speaks.
export const BYE_REASONS = ["shutdown", "replaced", "busy", "proto", "unauthorized", "unreachable"] as const;

// The `call` operations the extension performs with ordinary extension APIs.
export const CALL_OPS = [
  "window.ensure",
  "window.bounds",
  "tab.create",
  "tab.close",
  "tab.activate",
  "downloads.search",
  "cookies.names",
  "group.close",
  "notify",
] as const;

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isInt = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value);
const isString = (value: unknown): value is string => typeof value === "string";

/**
 * Reads one frame from the daemon, or null when it is not one this protocol knows. A frame that
 * fails here is dropped, never half-acted on: a `cdp` without an id cannot be answered, and a
 * `call` without an op would fall through to an operation nobody asked for.
 */
export function parseDaemonFrame(raw: unknown): DaemonFrame | null {
  if (!isObject(raw) || !isString(raw.t)) return null;
  switch (raw.t) {
    case "welcome":
      return isInt(raw.proto) && isString(raw.daemon_version) && isString(raw.install_name) &&
        (raw.install_kind === "native" || raw.install_kind === "server")
        ? (raw as WelcomeFrame)
        : null;
    case "cdp":
      return isInt(raw.id) && isString(raw.session) && isString(raw.method) &&
        (raw.params === undefined || isObject(raw.params))
        ? (raw as CdpFrame)
        : null;
    case "call":
      return isInt(raw.id) && isString(raw.op) && (raw.args === undefined || isObject(raw.args))
        ? (raw as CallFrame)
        : null;
    case "ping":
      return isInt(raw.n) ? { t: "ping", n: raw.n } : null;
    case "check_ok":
      return isInt(raw.n) ? { t: "check_ok", n: raw.n, host_seen: raw.host_seen === true } : null;
    case "bye":
      return isString(raw.reason) ? (raw as DaemonFrame) : null;
    default:
      return null;
  }
}

/** Whether an object is a frame this extension may send; used by the tests on the golden file. */
export function isExtensionFrame(raw: unknown): raw is ExtensionFrame {
  if (!isObject(raw) || !isString(raw.t)) return false;
  switch (raw.t) {
    case "hello":
      return isInt(raw.proto) && isString(raw.ext_version) && isString(raw.chrome_version) &&
        isString(raw.platform) && isString(raw.profile_id) && isString(raw.profile_name) &&
        (raw.token === undefined || isString(raw.token));
    case "result":
      if (!isInt(raw.id) || typeof raw.ok !== "boolean") return false;
      return raw.ok ? "value" in raw : isObject(raw.error) && isString(raw.error.code) && isString(raw.error.message);
    case "event":
      return isString(raw.session) && isString(raw.method) && "params" in raw;
    case "tabs":
      return ["created", "removed", "updated", "joined", "left", "group_closed"].includes(raw.op as string) &&
        (raw.tab === undefined || (isObject(raw.tab) && isInt(raw.tab.id))) &&
        (raw.group === undefined || (isObject(raw.group) && isString(raw.group.id)));
    case "input":
      return isInt(raw.tab);
    case "pong":
    case "check":
      return isInt(raw.n);
    default:
      return false;
  }
}

/**
 * A debuggee's session as the daemon names it: `x<tabId>` for the tab itself, `x<tabId>/<sid>` for
 * a flat child session inside it. The `x` keeps the name from ever being taken for a CDP session id,
 * which the daemon's own connection also multiplexes.
 */
export function sessionName(tabId: number, child?: string): string {
  return child ? `x${tabId}/${child}` : `x${tabId}`;
}

export function parseSession(session: string): { tabId: number; child?: string } | null {
  const match = /^x(\d+)(?:\/(.+))?$/.exec(session);
  if (!match) return null;
  const tabId = Number(match[1]);
  if (!Number.isSafeInteger(tabId)) return null;
  return match[2] ? { tabId, child: match[2] } : { tabId };
}
