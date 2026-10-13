// A widget an agent put into its answer (the Widget tool), as a document for a sandboxed frame.
//
// The frame has an opaque origin (`sandbox="allow-scripts"` and nothing else), so the widget cannot
// read the app's cookies, storage or DOM, and the policy inside the document refuses every
// connection, so it cannot call the API or any other site either. What it may do goes over
// postMessage, through the small bridge the document defines before the widget's own scripts run:
// its stored values (the app reads and writes them for it), a message for the composer (the
// operator sends it, never the widget), and the theme. Nothing in here touches the screen, so the
// document and the protocol are tested without a browser.

import type { Activity } from "./turns";

export type DrawnWidget = { callId: string; title: string; html: string; data: unknown };

/** The widgets a run of steps drew, in order: finished calls only. A refused call drew nothing, and a
 *  running one has not been checked by the host yet. */
export function drawnWidgets(items: readonly Activity[]): DrawnWidget[] {
  const out: DrawnWidget[] = [];
  for (const a of items) {
    if (a.kind !== "tool" || a.name !== "Widget" || a.running || a.error) continue;
    const html = typeof a.args.html === "string" ? a.args.html : "";
    if (!html.trim()) continue;
    const title = typeof a.args.title === "string" ? a.args.title.replace(/\s+/g, " ").trim() : "";
    out.push({ callId: a.id, title, html, data: a.args.data ?? null });
  }
  return out;
}

export const WIDGET_CSP =
  "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:; " +
  "connect-src 'none'; frame-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'";

export const MIN_HEIGHT = 40;
/** Past this the frame scrolls inside: a widget whose content grows with its frame (a `100vh`
 *  section) would otherwise grow the conversation without end. */
export const MAX_HEIGHT = 1400;

export function clampHeight(value: unknown): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  return Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.ceil(value)));
}

export const ASK_MAX = 4000;

export type WidgetTheme = { scheme: string; theme: string; tokens: Record<string, string> };
export type WidgetInit = { title: string; data: unknown; values: Record<string, unknown>; theme: WidgetTheme; lang: string };

/** Script text that cannot end its element early: `</script>` and `<!--` lose their angle brackets. */
export function scriptJson(value: unknown): string {
  return JSON.stringify(value ?? null).replace(/</g, "\\u003c").replace(/>/g, "\\u003e").replace(/\u2028/g, "\\u2028").replace(/\u2029/g, "\\u2029");
}

const BASE_STYLE = `*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;background:transparent}
body{padding:12px;font-family:var(--font,system-ui,sans-serif);font-size:var(--fs-14,14px);line-height:1.45;color:var(--fg);overflow-wrap:anywhere}
button,input,select,textarea{font:inherit;color:inherit}`;

/** The bridge, run first in the frame. Values are handed over whole when the frame opens, so a read
 *  answers at once; a write is the app's, and the widget's copy changes only once the app kept it. */
const BOOTSTRAP = `(function () {
  "use strict";
  var init = __INIT__;
  var values = init.values || {};
  var pending = {};
  var next = 1;
  var listeners = [];
  var current = null;
  function post(message) { message.widget = 1; parent.postMessage(message, "*"); }
  function call(kind, fields) {
    return new Promise(function (resolve, reject) {
      var id = next++;
      pending[id] = { resolve: resolve, reject: reject };
      fields.kind = kind;
      fields.id = id;
      post(fields);
      setTimeout(function () { if (pending[id]) { delete pending[id]; reject(new Error("the app did not answer")); } }, 15000);
    });
  }
  function applyTheme(theme) {
    if (!theme) return;
    var root = document.documentElement;
    var tokens = theme.tokens || {};
    Object.keys(tokens).forEach(function (name) { if (name.indexOf("--") === 0) root.style.setProperty(name, tokens[name]); });
    root.dataset.scheme = theme.scheme || "";
    root.style.colorScheme = theme.scheme === "light" ? "light" : "dark";
    current = { scheme: theme.scheme, theme: theme.theme, tokens: tokens };
  }
  applyTheme(init.theme);
  window.addEventListener("message", function (e) {
    if (e.source !== parent) return;
    var d = e.data;
    if (!d || d.widget !== 1) return;
    if (d.kind === "reply" && pending[d.id]) {
      var waiting = pending[d.id];
      delete pending[d.id];
      if (d.error) waiting.reject(new Error(String(d.error))); else waiting.resolve(d.result);
    } else if (d.kind === "theme") {
      applyTheme(d.theme);
      listeners.slice().forEach(function (fn) { try { fn(current); } catch (error) { console.error(error); } });
    }
  });
  function copy(value) { return value === undefined ? null : JSON.parse(JSON.stringify(value)); }
  var storage = Object.freeze({
    get: function (key) {
      key = String(key);
      return Promise.resolve(Object.prototype.hasOwnProperty.call(values, key) ? copy(values[key]) : null);
    },
    set: function (key, value) {
      key = String(key);
      var plain;
      try { plain = copy(value); } catch (error) { return Promise.reject(error); }
      return call("set", { key: key, value: plain }).then(function () { values[key] = plain; return true; });
    },
    delete: function (key) {
      key = String(key);
      return call("delete", { key: key }).then(function (found) { delete values[key]; return !!found; });
    },
    keys: function () { return Promise.resolve(Object.keys(values).sort()); }
  });
  window.widget = Object.freeze({
    title: init.title,
    data: init.data,
    page: false,
    lang: init.lang,
    get theme() { return current; },
    onTheme: function (fn) { listeners.push(fn); return function () { listeners = listeners.filter(function (f) { return f !== fn; }); }; },
    storage: storage,
    ask: function (text) { return call("ask", { text: String(text == null ? "" : text) }); }
  });
  var last = -1;
  function measure() {
    var root = document.documentElement.getBoundingClientRect().height;
    var body = document.body ? document.body.getBoundingClientRect().bottom + (parseFloat(getComputedStyle(document.body).marginBottom) || 0) : 0;
    var height = Math.ceil(Math.max(root, body));
    if (height !== last) { last = height; post({ kind: "height", height: height }); }
  }
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(measure).observe(document.documentElement);
  document.addEventListener("DOMContentLoaded", function () {
    measure();
    if (typeof ResizeObserver !== "undefined" && document.body) new ResizeObserver(measure).observe(document.body);
  });
  window.addEventListener("load", measure);
  // A link would take the frame to another site, which is the network the widget does not have.
  document.addEventListener("click", function (e) {
    var link = e.target && e.target.closest ? e.target.closest("a[href]") : null;
    if (link && (link.getAttribute("href") || "").charAt(0) !== "#") e.preventDefault();
  }, true);
  document.addEventListener("submit", function (e) { e.preventDefault(); }, true);
})();`;

/** The frame's whole document: the policy, the base look and the bridge first in the head, then the
 *  widget as the agent wrote it, whether a whole document or a fragment. */
export function widgetDocument(html: string, init: WidgetInit): string {
  const parsed = new DOMParser().parseFromString(html, "text/html");
  // A base or a policy of the widget's own could loosen what the frame's policy says.
  parsed.querySelectorAll("base, meta[http-equiv]").forEach((el) => el.remove());
  const head = parsed.head;
  const bridge = parsed.createElement("script");
  bridge.textContent = BOOTSTRAP.replace("__INIT__", () => scriptJson(init));
  const style = parsed.createElement("style");
  style.textContent = BASE_STYLE;
  const viewport = parsed.createElement("meta");
  viewport.name = "viewport";
  viewport.content = "width=device-width, initial-scale=1";
  const csp = parsed.createElement("meta");
  csp.httpEquiv = "Content-Security-Policy";
  csp.content = WIDGET_CSP;
  head.prepend(csp, viewport, style, bridge);
  parsed.documentElement.dataset.scheme = init.theme.scheme;
  if (init.lang) parsed.documentElement.lang = init.lang;
  return "<!doctype html>" + parsed.documentElement.outerHTML;
}

export type WidgetMessage =
  | { kind: "height"; height: number }
  | { kind: "set"; id: number; key: string; value: unknown }
  | { kind: "delete"; id: number; key: string }
  | { kind: "ask"; id: number; text: string };

/** A message from a widget's frame, or null when it is not one the bridge speaks. */
export function widgetMessage(data: unknown): WidgetMessage | null {
  const d = data as Record<string, unknown> | null;
  if (!d || typeof d !== "object" || d.widget !== 1 || typeof d.kind !== "string") return null;
  if (d.kind === "height") return typeof d.height === "number" ? { kind: "height", height: d.height } : null;
  if (typeof d.id !== "number") return null;
  if ((d.kind === "set" || d.kind === "delete") && typeof d.key === "string" && d.key) {
    return d.kind === "set" ? { kind: "set", id: d.id, key: d.key, value: d.value ?? null } : { kind: "delete", id: d.id, key: d.key };
  }
  if (d.kind === "ask" && typeof d.text === "string") return { kind: "ask", id: d.id, text: d.text };
  return null;
}

/** The storage route of one key: each segment encoded, the slashes kept. */
export function storagePath(sessionId: string, callId: string, key?: string): string {
  const base = `/api/sessions/${encodeURIComponent(sessionId)}/widgets/${encodeURIComponent(callId)}/storage`;
  return key === undefined ? base : `${base}/${key.split("/").map(encodeURIComponent).join("/")}`;
}

/** The words a widget hands the composer: trimmed and capped, or null when there are none. */
export function askText(text: string): string | null {
  const words = text.trim();
  return words ? words.slice(0, ASK_MAX) : null;
}
