// HTML runs with an opaque origin: scripts may draw their report, but cannot read the app's
// credentials or DOM. Navigation is relayed as a path, checked here, and fetched with the app's
// auth header. The frame never receives that header or a URL containing it.

import { useEffect, useMemo, useRef, useState } from "react";
import { localHtmlPath } from "./htmlpath";
import { api } from "./api";
import { errorText } from "./ui";
import { t } from "./i18n";

export type HtmlNavigation = { back: (() => void) | null; forward: (() => void) | null; reload: () => void; path: string; busy: boolean };

export function HtmlPreview({ text, base, path, onNavigation }: { text: string; base?: string; path: string; onNavigation?: (nav: HtmlNavigation | null) => void }) {
  const frame = useRef<HTMLIFrameElement>(null);
  const [history, setHistory] = useState({ paths: [path], at: 0 });
  const [gen, setGen] = useState(0);
  const [body, setBody] = useState({ text, path, gen: 0 });
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const current = history.paths[history.at];
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true);
    setError("");
    const file = current.split("#")[0];
    if (file === path && gen === 0) setBody({ text, path: current, gen });
    else if (base) {
      api.fetch(`${base}/download?path=${encodeURIComponent(file)}`, { signal: controller.signal }).then(async (r) => { if (!r.ok) throw new Error(`${r.status} ${r.statusText}`); return r.text(); }).then((s) => { if (!controller.signal.aborted) setBody({ text: s, path: current, gen }); }).catch((e) => { if (!controller.signal.aborted) { setError(errorText(e)); setBusy(false); } });
    } else { setBody({ text, path: current, gen }); setBusy(false); }
    return () => controller.abort();
  }, [base, current, path, text, gen]);
  useEffect(() => {
    onNavigation?.({ back: history.at > 0 ? () => setHistory((h) => ({ ...h, at: h.at - 1 })) : null, forward: history.at < history.paths.length - 1 ? () => setHistory((h) => ({ ...h, at: h.at + 1 })) : null, reload: () => setGen((n) => n + 1), path: current, busy });
  }, [history, current, busy, onNavigation]);
  useEffect(() => () => onNavigation?.(null), [onNavigation]);
  useEffect(() => {
    const receive = (e: MessageEvent) => {
      if (e.source !== frame.current?.contentWindow || e.data?.type !== "preview-link" || typeof e.data.href !== "string") return;
      const next = localHtmlPath(e.data.href, current.split("#")[0]);
      if (next === null) { setError(t("preview.html.external")); return; }
      if (!base && next.split("#")[0] !== path) { setError(t("preview.html.local")); return; }
      setHistory((h) => ({ paths: [...h.paths.slice(0, h.at + 1), next], at: h.at + 1 }));
    };
    window.addEventListener("message", receive);
    return () => window.removeEventListener("message", receive);
  }, [base, current, path]);
  const doc = useMemo(() => {
    const parsed = new DOMParser().parseFromString(body.text, "text/html");
    // Inline assets work without handing a document any authenticated URLs. Blocking connections
    // also prevents a report from posting
    // to a cookie-authenticated API even though its scripts cannot read the response.
    parsed.querySelectorAll("base, meta[http-equiv]").forEach((el) => el.remove());
    const csp = parsed.createElement("meta");
    csp.httpEquiv = "Content-Security-Policy";
    csp.content = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; media-src data: blob:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'";
    parsed.head.prepend(csp);
    const script = parsed.createElement("script");
    script.textContent = `document.addEventListener('click', function(e) { const a = e.target.closest('a[href]'); if (!a) return; e.preventDefault(); e.stopImmediatePropagation(); parent.postMessage({type:'preview-link',href:a.getAttribute('href')}, '*'); }, true); document.addEventListener('submit', function(e) { e.preventDefault(); }, true);`;
    parsed.head.append(script);
    const hash = body.path.split("#")[1];
    if (hash) {
      const jump = parsed.createElement("script");
      jump.textContent = `document.getElementById(${JSON.stringify(hash).replace(/</g, "\\u003c")})?.scrollIntoView();`;
      parsed.body.append(jump);
    }
    return "<!doctype html>" + parsed.documentElement.outerHTML;
  }, [body]);
  return <>{error && <div className="explorer-note" role="status">{error}</div>}<iframe key={`${body.path}:${body.gen}`} ref={frame} className="preview-frame html-frame" sandbox="allow-scripts" srcDoc={doc} title={path} onLoad={() => setBusy(false)} /></>;
}
