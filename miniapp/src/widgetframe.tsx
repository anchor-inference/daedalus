// A widget in the conversation: its title, "Open as page", and the widget itself in a sandboxed
// frame that fits its height to what it draws. The frame cannot reach the API, so the app answers its
// bridge (widget.ts): it keeps the widget's values on the host, hands a follow-up to the composer
// when the operator's own click asked for it, and pushes the theme whenever the app's look changes.

import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { Icon } from "./icons";
import { t, useLang } from "./i18n";
import { themeSnapshot } from "./pagebridge";
import { invalidate } from "./store";
import { navigate } from "./router";
import { askText, clampHeight, storagePath, widgetDocument, widgetMessage, type DrawnWidget, type WidgetTheme } from "./widget";
import "./widget.css";

function currentTheme(): WidgetTheme {
  const snapshot = themeSnapshot(document.documentElement);
  return { scheme: snapshot.scheme, theme: snapshot.theme, tokens: snapshot.tokens };
}

/** Whether the operator just touched the page: a click inside the frame counts, since activation
 *  reaches every frame above the one clicked. A browser without the API is taken at its word. */
function userActive(): boolean {
  const activation = (navigator as Navigator & { userActivation?: { isActive: boolean } }).userActivation;
  return activation ? activation.isActive : true;
}

export type WidgetFrameProps = {
  sessionId: string;
  widget: DrawnWidget;
  toast: (text: string) => void;
  /** Put text into the conversation's composer; false where there is none (a subagent's transcript). */
  compose?: (text: string) => boolean;
};

export function WidgetFrame({ sessionId, widget, toast, compose }: WidgetFrameProps) {
  const [lang] = useLang();
  const frame = useRef<HTMLIFrameElement>(null);
  const [doc, setDoc] = useState("");
  const [height, setHeight] = useState(120);
  const [promoting, setPromoting] = useState(false);
  const title = widget.title || t("widget.untitled");
  const composeRef = useRef(compose);
  composeRef.current = compose;
  const opening = useRef({ title, lang });
  opening.current = { title, lang };

  // The document is built once, with the values as they are when it opens: rebuilding it on every
  // write would reload the frame under the operator's hand. Later theme changes travel as messages.
  useEffect(() => {
    let stopped = false;
    const open = (values: Record<string, unknown>) => {
      if (stopped) return;
      setDoc(widgetDocument(widget.html, { ...opening.current, data: widget.data, values, theme: currentTheme() }));
    };
    api.get<{ values: Record<string, unknown> }>(storagePath(sessionId, widget.callId)).then(
      (found) => open(found.values ?? {}),
      // Unreadable values still leave a widget to use; what it writes now is kept as usual.
      () => open({}),
    );
    return () => { stopped = true; };
  }, [sessionId, widget.callId, widget.html]);

  useEffect(() => {
    const element = frame.current;
    if (!element || !doc) return;
    const reply = (id: number, answer: { result?: unknown; error?: string }) => {
      element.contentWindow?.postMessage({ widget: 1, kind: "reply", id, ...answer }, "*");
    };
    const receive = (e: MessageEvent) => {
      if (!element.contentWindow || e.source !== element.contentWindow) return;
      const message = widgetMessage(e.data);
      if (!message) return;
      if (message.kind === "height") {
        const next = clampHeight(message.height);
        if (next !== null) setHeight(next);
        return;
      }
      if (message.kind === "set") {
        api.put(storagePath(sessionId, widget.callId, message.key), { value: message.value }).then(
          () => reply(message.id, { result: true }),
          (error) => reply(message.id, { error: error instanceof ApiError ? error.message : String(error) }),
        );
        return;
      }
      if (message.kind === "delete") {
        api.delete(storagePath(sessionId, widget.callId, message.key)).then(
          () => reply(message.id, { result: true }),
          (error) => error instanceof ApiError && error.status === 404 ? reply(message.id, { result: false }) : reply(message.id, { error: String(error) }),
        );
        return;
      }
      const text = askText(message.text);
      if (!text) return reply(message.id, { error: "ask needs text" });
      // A widget may not fill the composer on a timer while the operator is typing elsewhere.
      if (!userActive()) return reply(message.id, { error: "ask answers a click of the operator's, not a timer" });
      if (!composeRef.current?.(text)) return reply(message.id, { error: "there is no message field here" });
      toast(t("widget.asked"));
      reply(message.id, { result: true });
    };
    let last = "";
    const pushTheme = () => {
      const theme = currentTheme();
      const key = JSON.stringify(theme);
      if (key === last) return;
      last = key;
      element.contentWindow?.postMessage({ widget: 1, kind: "theme", theme }, "*");
    };
    const loaded = () => {
      last = JSON.stringify(currentTheme());
    };
    window.addEventListener("message", receive);
    window.addEventListener("daedalus-appearance", pushTheme);
    element.addEventListener("load", loaded);
    const observer = new MutationObserver(pushTheme);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["style", "data-scheme", "data-theme"] });
    const dark = window.matchMedia?.("(prefers-color-scheme: dark)");
    dark?.addEventListener?.("change", pushTheme);
    return () => {
      window.removeEventListener("message", receive);
      window.removeEventListener("daedalus-appearance", pushTheme);
      element.removeEventListener("load", loaded);
      observer.disconnect();
      dark?.removeEventListener?.("change", pushTheme);
    };
  }, [doc, sessionId, widget.callId, toast]);

  const promote = async () => {
    setPromoting(true);
    try {
      const made = await api.post<{ slug: string; path: string }>(`/api/sessions/${encodeURIComponent(sessionId)}/widgets/${encodeURIComponent(widget.callId)}/page`, {
        title, html: widget.html, data: widget.data ?? null,
      });
      invalidate("/api/pages");
      toast(t("widget.page.done", { title }));
      navigate(made.path);
    } catch (error) {
      toast(t("widget.page.failed", { error: error instanceof ApiError ? error.message : String(error) }));
    } finally {
      setPromoting(false);
    }
  };

  return (
    <div className="widget-card" data-widget={widget.callId}>
      <div className="widget-head">
        <span className="widget-glyph" aria-hidden><Icon name="sliders" size={15} /></span>
        <span className="widget-title truncate">{title}</span>
        <button type="button" className="iconbtn small widget-page" onClick={promote} disabled={promoting} title={t("widget.page")} aria-label={t("widget.page")}>
          <Icon name="external" size={15} />
        </button>
      </div>
      {doc ? (
        <iframe ref={frame} className="widget-frame" sandbox="allow-scripts" srcDoc={doc} title={title} style={{ height }} />
      ) : (
        <div className="widget-frame widget-loading" style={{ height }} aria-busy="true" />
      )}
    </div>
  );
}

/** The widgets one run of steps drew, each in its frame, in the order they were drawn. */
export function WidgetFrames({ sessionId, widgets, toast, compose }: { sessionId: string; widgets: DrawnWidget[]; toast: (text: string) => void; compose?: (text: string) => boolean }) {
  if (!widgets.length) return null;
  return (
    <div className="widgets" aria-label={t("widget.list")}>
      {widgets.map((widget) => <WidgetFrame key={widget.callId} sessionId={sessionId} widget={widget} toast={toast} compose={compose} />)}
    </div>
  );
}
