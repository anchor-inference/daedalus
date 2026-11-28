// A live link's page: the app opened at /v/<token>/ on the services door, driving one session.
//
// The token is the whole credential and it lives in the address, so every request the app makes has to
// carry it in its path: a stream, a socket, a picture and a download cannot carry a header the browser
// would let us set. Rather than teach each of the app's many requests about it, the page's own fetch,
// XHR, EventSource and WebSocket put `/v/<token>` in front of every `/api/` and `/ws/` path once, here,
// before the app starts. A request this misses is refused by the host's door, never answered for the
// operator: the door, not this file, is what keeps a visitor to the one session.
//
// Nothing of the token is written to storage. The services origin also serves pages an agent wrote, and
// whatever this page kept there those pages could read. The visitor's random id and the name they gave
// are kept, because they are not secrets: they are only how the operator tells one visitor from another.

/** Fired when the host answers that the link was revoked or has expired: the page says so and stops. */
export const VISIT_ENDED = "daedalus-visit-ended";
/** Fired when the host answers 401: the link has a password and this page holds no pass (or an old one,
 *  after the operator changed it). The page shows the password screen. */
export const VISIT_LOCKED = "daedalus-visit-locked";
/** Fired when a send answers 402: the owner's limit for the link is reached. */
export const VISIT_SPENT = "daedalus-visit-spent";

/** Watches a door's answer for the three states the page reacts to as a whole. */
function heard(status: number): void {
  if (status === 410) window.dispatchEvent(new Event(VISIT_ENDED));
  else if (status === 401) window.dispatchEvent(new Event(VISIT_LOCKED));
  else if (status === 402) window.dispatchEvent(new Event(VISIT_SPENT));
}

export type AllowedPick = { preset: string; accounts: string[]; efforts: string[] };
export type RoleLimit = { preset: string; effort: string; account: string; allowed: AllowedPick[] };
export type VisitLimits = {
  roles: Record<string, RoleLimit>;
  budget: { usd: { limit: number; spent: number } | null; weekly_pct: { limit: number; spent: number } | null };
  terminals: "all" | "container" | "none";
};

let limits: VisitLimits | null = null;

/** What the link lets this visitor choose, as ``GET /api/share`` said; null in the operator's app. */
export function visitLimits(): VisitLimits | null {
  return limits;
}

export function setVisitLimits(next: VisitLimits | null): void {
  limits = next;
}

/** The efforts a visitor may use with ``preset`` for a role: null when the role is not narrowed (any). */
export function allowedEfforts(role: string, preset: string, from: VisitLimits | null = limits): string[] | null {
  const kept = from?.roles?.[role];
  if (!kept) return null;
  const entry = kept.allowed.find((item) => item.preset === preset);
  const own = preset === kept.preset ? [kept.effort] : [];
  if (entry) return entry.efforts.length ? [...new Set([...entry.efforts, ...own])] : null;
  return own;
}

/** Whether the visitor has no choice at all for a role: the link locks it to one value. */
export function roleLocked(role: string, from: VisitLimits | null = limits): boolean {
  const kept = from?.roles?.[role];
  return !!kept && kept.allowed.length === 0;
}

const LINK = /^\/v\/([A-Za-z0-9_-]{40,64})(?:\/|$)/;
const VISITOR_KEY = "daedalus.visitor.id";
const NAME_KEY = "daedalus.visitor.name";

/** The link's token when this page is a live link's, else null. */
export function shareToken(pathname: string = window.location.pathname): string | null {
  return LINK.exec(pathname)?.[1] ?? null;
}

export function inShare(): boolean {
  return shareToken() !== null;
}

/** An API or socket path as this page must ask for it: under the link's prefix on a live link's page,
 *  unchanged in the operator's app. Absolute addresses of this page's own origin are rewritten too. */
export function routed(path: string, token: string | null = shareToken()): string {
  if (!token) return path;
  let rest = path;
  let origin = "";
  if (/^(https?|wss?):\/\//.test(path)) {
    const url = new URL(path);
    const own = url.host === window.location.host;
    if (!own) return path;
    origin = `${url.protocol}//${url.host}`;
    rest = url.pathname + url.search + url.hash;
  }
  if (rest.startsWith("/api/") || rest.startsWith("/ws/")) return `${origin}/v/${token}${rest}`;
  return path;
}

function kept(key: string): string {
  try {
    return localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function keep(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* a browser without site data: the value lives for this page only */
  }
}

let memoryVisitor = "";

/** This browser's random id, made once: how the operator counts visitors, never who they are. */
export function visitorId(): string {
  let id = kept(VISITOR_KEY) || memoryVisitor;
  if (!/^[A-Za-z0-9_-]{8,64}$/.test(id)) {
    const bytes = new Uint8Array(12);
    crypto.getRandomValues(bytes);
    id = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    memoryVisitor = id;
    keep(VISITOR_KEY, id);
  }
  return id;
}

export function visitorName(): string {
  return kept(NAME_KEY);
}

export function setVisitorName(name: string): void {
  keep(NAME_KEY, name.trim().slice(0, 60));
}

function visitorHeaders(): Record<string, string> {
  const name = visitorName();
  return { "X-Share-Visitor": visitorId(), ...(name ? { "X-Share-Name": encodeURIComponent(name) } : {}) };
}

/** Puts every request of this page under the link. Called once, before the app renders. */
export function installShareTransport(token: string): void {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = routed(raw, token);
    if (path === raw) return nativeFetch(input, init);
    const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
    for (const [key, value] of Object.entries(visitorHeaders())) headers.set(key, value);
    // No credential of anyone else's: the link's token in the path, and for a link with a password the
    // pass the door set at unlocking, a cookie scoped to this link's own path. It was "omit" before
    // passwords existed, and with "omit" the browser both drops the pass it is given and never sends it,
    // so a visitor who typed the right password stayed locked out.
    headers.delete("X-Daedalus-Token");
    headers.delete("Authorization");
    const asked = nativeFetch(path, { ...(input instanceof Request ? { method: input.method, body: input.body } : {}), ...init, headers, credentials: "same-origin" });
    // 410 is the door's answer to a link that stopped, and the event stream's reconnect is what hears it
    // first once a revocation cut the stream: the page gives up there rather than retrying forever.
    void asked.then((response) => heard(response.status), () => undefined);
    return asked;
  };
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  const marked = new WeakSet<XMLHttpRequest>();
  XMLHttpRequest.prototype.open = function (this: XMLHttpRequest, method: string, url: string | URL, ...rest: unknown[]) {
    const raw = String(url);
    const path = routed(raw, token);
    if (path !== raw) marked.add(this);
    return (open as (...args: unknown[]) => void).call(this, method, path, ...(rest.length ? rest : [true]));
  } as typeof XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.send = function (this: XMLHttpRequest, body?: Document | XMLHttpRequestBodyInit | null) {
    if (marked.has(this)) {
      for (const [key, value] of Object.entries(visitorHeaders())) this.setRequestHeader(key, value);
      this.addEventListener("load", () => heard(this.status));
    }
    return send.call(this, body);
  };
  const NativeSource = window.EventSource;
  window.EventSource = class extends NativeSource {
    constructor(url: string | URL, init?: EventSourceInit) {
      super(routed(String(url), token), { ...init, withCredentials: false });
    }
  } as typeof EventSource;
  const NativeSocket = window.WebSocket;
  window.WebSocket = class extends NativeSocket {
    constructor(url: string | URL, protocols?: string | string[]) {
      super(routed(String(url), token), protocols);
    }
  } as typeof WebSocket;
}
