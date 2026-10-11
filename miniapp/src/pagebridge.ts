// The app's side of a custom page's bridge (`/pages/_kit/daedalus.js` is the page's side).
//
// A page is same-origin and signed in with the session cookie, so it can call the API itself; the
// bridge carries only what the app alone knows or does: the live theme, toasts, navigation, and the
// events the app forwards. Every message is checked against the frame it must come from — the frame
// is same-origin, so the origin alone would also let any other window of the site speak for it.
//
// The events forwarded are the page's own: what its agent pushed with PageEmit (`pages.emit` for its
// slug), and the conversation of the session bound to it — its replies, its run state, and every event
// of that session. The app holds one event stream for everything; each open page is handed only what
// is about it, and only through its own frame.

/// <reference types="vite/client" />
import type { AppEvent, MessageView } from "./api";
import tokensCss from "./ui/tokens.css?raw";

export const PROTOCOL = 1;

/** Every custom property the app's stylesheet declares, read from the stylesheet itself so a token
 *  added there reaches the pages without a list here to keep in step. */
export const TOKEN_NAMES: string[] = [...new Set([...tokensCss.matchAll(/(--[a-z0-9-]+)\s*:/g)].map((m) => m[1]))];

/** What appearance.ts sets that the stylesheet does not declare. */
const APPEARANCE_ONLY = ["--wash", "--term-fg", "--font-prose"];

export type ThemeMessage = { tokens: Record<string, string>; scheme: string; theme: string; lang: string; phone: boolean };

/** The theme as the app paints it now: the computed value of every token on the app's root. A theme
 *  is applied as inline values there (appearance.ts), so only the computed style has it all. */
export function themeSnapshot(root: HTMLElement = document.documentElement, phone = false, lang = "en"): ThemeMessage {
  const style = getComputedStyle(root);
  const tokens: Record<string, string> = {};
  for (const name of [...TOKEN_NAMES, ...APPEARANCE_ONLY]) {
    const value = style.getPropertyValue(name).trim();
    if (value) tokens[name] = value;
  }
  return { tokens, scheme: root.dataset.scheme || "dark", theme: root.dataset.theme || "", lang, phone };
}

export type BridgeRequest = { daedalus: number; id: number; method: string; params?: Record<string, unknown> };

export type BridgeDeps = {
  theme: () => ThemeMessage;
  toast: (text: string) => void;
  navigate: (path: string) => void;
  openFile: (sessionId: string, path: string) => void;
  /** Show the page's agent beside it (a sheet on a phone); false when the page has none. */
  openAgent?: () => boolean;
};

export function isRequest(data: unknown): data is BridgeRequest {
  const d = data as BridgeRequest | null;
  return !!d && typeof d === "object" && d.daedalus === PROTOCOL && typeof d.id === "number" && typeof d.method === "string";
}

/** An address inside the app: a path under /app as it is, any other absolute path put under it.
 *  Anything else (another site, a scheme) is refused: navigation is the app's own. */
export function appPath(path: unknown): string | null {
  if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//")) return null;
  return path === "/app" || path.startsWith("/app/") || path.startsWith("/app?") ? path : `/app${path}`;
}

/** The answer to one request: `{ result }` or `{ error }`. Pure, so the protocol is tested without a frame. */
export function answer(request: BridgeRequest, deps: BridgeDeps): { result?: unknown; error?: string } {
  const p = request.params ?? {};
  switch (request.method) {
    case "ready":
    case "theme":
      return { result: { theme: deps.theme(), protocol: PROTOCOL } };
    case "toast": {
      const text = typeof p.text === "string" ? p.text.trim().slice(0, 500) : "";
      if (!text) return { error: "a toast needs text" };
      deps.toast(text);
      return { result: true };
    }
    case "navigate": {
      const path = appPath(p.path);
      if (!path) return { error: "navigate takes a path inside the app" };
      deps.navigate(path);
      return { result: true };
    }
    case "openFile": {
      if (typeof p.sessionId !== "string" || !p.sessionId || typeof p.path !== "string" || !p.path) return { error: "openFile takes a session id and a path" };
      deps.openFile(p.sessionId, p.path);
      return { result: true };
    }
    case "agent.open": {
      if (!deps.openAgent?.()) return { error: "this page has no agent" };
      return { result: true };
    }
    default:
      return { error: `unknown method ${request.method}` };
  }
}

/**
 * Serve the bridge for one frame until the returned function is called. The theme is pushed again
 * whenever the app's look changes — a theme picked in Appearance, the system turning dark, the window
 * crossing the phone width — so the page follows without asking.
 */
export function serveBridge(frame: HTMLIFrameElement, deps: BridgeDeps, win: Window = window): { stop: () => void; send: (event: string, data: unknown) => void } {
  const send = (event: string, data: unknown) => {
    try {
      frame.contentWindow?.postMessage({ daedalus: PROTOCOL, event, data }, win.location.origin);
    } catch {
      /* the frame is between documents */
    }
  };
  const receive = (e: MessageEvent) => {
    if (!frame.contentWindow || e.source !== frame.contentWindow || e.origin !== win.location.origin || !isRequest(e.data)) return;
    const reply = answer(e.data, deps);
    frame.contentWindow.postMessage({ daedalus: PROTOCOL, id: e.data.id, ...reply }, win.location.origin);
  };
  let last = "";
  const pushTheme = () => {
    const theme = deps.theme();
    const key = JSON.stringify(theme);
    if (key === last) return;
    last = key;
    send("theme", theme);
  };
  win.addEventListener("message", receive);
  win.addEventListener("daedalus-appearance", pushTheme);
  win.addEventListener("resize", pushTheme);
  const observer = typeof MutationObserver !== "undefined" ? new MutationObserver(pushTheme) : null;
  observer?.observe(win.document.documentElement, { attributes: true, attributeFilter: ["style", "data-scheme", "data-theme", "lang"] });
  const dark = win.matchMedia?.("(prefers-color-scheme: dark)");
  dark?.addEventListener?.("change", pushTheme);
  // A reload of the frame is a new page that has heard nothing yet.
  const loaded = () => {
    last = "";
  };
  frame.addEventListener("load", loaded);
  return {
    send,
    stop: () => {
      win.removeEventListener("message", receive);
      win.removeEventListener("daedalus-appearance", pushTheme);
      win.removeEventListener("resize", pushTheme);
      observer?.disconnect();
      dark?.removeEventListener?.("change", pushTheme);
      frame.removeEventListener("load", loaded);
    },
  };
}

// ── what the app forwards to an open page ──────────────────────────────────────────────────

/** Which page a frame shows and which session is its agent, read afresh for every event. */
export type PageContext = { slug: string; sessionId: string | null };
export type PageEvent = { event: string; data: unknown };
export type AgentRun = { run_id: string | null; status: string; origin?: string; summary?: string; error?: string };
export type AgentReply = { role: "assistant"; text: string; run_id: string | null; seq: number | null; at: string };

/** A run's state as the page's `agent.on("run")` hears it, from the three events that move it. */
export function runState(event: AppEvent): AgentRun | null {
  const p = event.payload ?? {};
  if (event.type === "run.started") return { run_id: p.run_id ?? null, status: "running", origin: p.origin };
  if (event.type === "session.status") return { run_id: p.run_id ?? null, status: String(p.status ?? "") };
  if (event.type === "run.finished") {
    const run: AgentRun = { run_id: p.run_id ?? null, status: String(p.status ?? ""), origin: p.origin };
    if (typeof p.summary === "string") run.summary = p.summary;
    if (typeof p.error === "string") run.error = p.error;
    return run;
  }
  return null;
}

/**
 * What one event of the app's stream means to one open page: nothing, unless it is a PageEmit for
 * this page's slug (handed over under the agent's own event name) or an event of this page's agent
 * session (`agent.event` with the whole event, and `agent.run` when it moves a run). Pure, so the
 * filtering is tested without a frame or a stream.
 */
export function pageEvents(event: AppEvent, page: PageContext): PageEvent[] {
  if (event.type === "pages.emit") {
    const p = event.payload ?? {};
    return p.slug === page.slug && typeof p.event === "string" ? [{ event: p.event, data: p.data ?? null }] : [];
  }
  if (!page.sessionId || event.session_id !== page.sessionId) return [];
  const out: PageEvent[] = [{ event: "agent.event", data: { type: event.type, payload: event.payload, at: event.at } }];
  const run = runState(event);
  if (run) out.push({ event: "agent.run", data: run });
  return out;
}

/** The agent's answers of one run, after the last one already handed over: the assistant's text only,
 *  never a step without words, a summary or a note of the host's. */
export function runReplies(messages: MessageView[], runId: string | null, afterSeq: number): AgentReply[] {
  return messages
    .filter((m) => m.role === "assistant" && !m.internal && !m.summary && (m.text ?? "").trim() !== "" && (runId === null || m.run_id === runId) && (m.seq ?? Infinity) > afterSeq)
    .map((m) => ({ role: "assistant" as const, text: m.text, run_id: m.run_id ?? null, seq: m.seq ?? null, at: m.created_at }));
}

export type ForwardDeps = {
  /** Subscribe to the app's event stream (events.ts `onEvent`). */
  on: (types: readonly string[], handler: (event: AppEvent) => void) => () => void;
  /** The newest messages of a session. */
  messages: (sessionId: string) => Promise<MessageView[]>;
};

/**
 * Forward to one frame (`send` is that frame's) everything the app hears that is about its page,
 * until the returned function is called. When the agent's run ends, its answers are read once and
 * handed over as `agent.message`, each at most once.
 */
export function forwardToPage(send: (event: string, data: unknown) => void, page: () => PageContext, deps: ForwardDeps): () => void {
  let lastSeq = -1;
  let stopped = false;
  const off = deps.on([], (event) => {
    const now = page();
    for (const item of pageEvents(event, now)) send(item.event, item.data);
    if (event.type !== "run.finished" || !now.sessionId || event.session_id !== now.sessionId) return;
    const sessionId = now.sessionId;
    const runId = typeof event.payload?.run_id === "string" ? event.payload.run_id : null;
    deps.messages(sessionId).then(
      (messages) => {
        // Rebound while the answers were being read: they belong to an agent this page no longer has.
        if (stopped || page().sessionId !== sessionId) return;
        for (const reply of runReplies(messages, runId, lastSeq)) {
          if (reply.seq !== null) lastSeq = Math.max(lastSeq, reply.seq);
          send("agent.message", reply);
        }
      },
      () => undefined,
    );
  });
  return () => {
    stopped = true;
    off();
  };
}
