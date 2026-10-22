// The relay protocol between this extension and a `browserd` running `--backend attach`. Every frame
// is one JSON object with `t`; the daemon's side is in `browserd/internal/relay`, and both sides test
// against the same golden frames. A field added on one side and not the other is a mismatch the
// golden file catches; a change in meaning bumps `PROTO`, and both sides then show "update" instead
// of misbehaving. 2 names the conversation that owns each group (`session` on `window.ensure` and
// `tab.create`) and adds the side panel's two calls.

export const PROTO = 2;

// The native-messaging host the desktop launcher registers for Chrome. It is the reverse-domain name
// Chrome requires (lowercase letters, digits, dots and underscores), and it names what it starts:
// `browserd bridge`, which carries these frames to the `user` daemon's socket.
export const NATIVE_HOST = "com.daedalus.browser";

// The extension's id, fixed by the public key in the manifest. The bridge checks the caller's origin
// against it, and the launcher writes it into the host manifest's `allowed_origins`.
export const EXTENSION_ID = "lkhhklicnjlplokpadpccndipomikoaj";

export type ErrorBody = { code: string; message: string };

// A tab as a `tabs` frame or a call's answer names it. Empty fields are left out, as the daemon
// writes them: `removed` and `left` carry the id alone.
export type TabInfo = {
  id: number;
  group?: string;
  opener?: number;
  url?: string;
  title?: string;
  given?: boolean;
};

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
  | { t: "tabs"; op: TabsOp; tab?: TabInfo; group?: string }
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
  | { t: "bye"; reason: string; message?: string; proto?: number };

// Why a relay ends. The daemon sends the first six, the bridge the next two when it never reached a
// daemon that takes a relay, and the host the last on the server transport when the device token is
// unknown, expired or revoked. `message` is for a log; the side panel shows its own words for each reason.
// `proto` on a `proto` bye is the version the daemon speaks.
export const BYE_REASONS = [
  "shutdown",
  "replaced",
  "another_relay",
  "proto",
  "lost",
  "bad_frame",
  "not_running",
  "unavailable",
  "unauthorized",
] as const;

// The codes of a failed `result`, as the daemon reads them.
export const RESULT_ERRORS = {
  // The allowlist, or a tab outside the agent's groups.
  refused: "refused",
  // The tab is gone, or was never the agent's.
  noTab: "no_tab",
  // Chrome's own DevTools error; the message is its text.
  cdp: "cdp",
  // The debugger was detached from the tab and has not been attached again yet.
  detached: "detached",
  // Anything else.
  failed: "failed",
} as const;

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
  "panel.grant",
  "panel.event",
] as const;

// The side panel's credential, as `panel.grant` hands it over: the host app's address the panel
// frames, the token, and when it lapses if unused. Kept in `chrome.storage.session` only, so it
// dies with the browser and never reaches the disk; given to the panel's frame alone.
export type PanelGrant = { base: string; token: string; expires: string };

// A conversation using this Chrome needs the operator, or finished: a notification and the badge.
export type PanelEvent = { session: string; kind: "needs_you" | "done"; title: string; waiting: number; tab?: number };

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
        (raw.group === undefined || isString(raw.group));
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
