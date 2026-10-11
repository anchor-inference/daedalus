// A custom page inside the app: the screen at /app/p/:slug frames the page the host serves at /pages/:slug/.
//
// The page is the operator's own and same-origin, so the frame is not sandboxed: the page calls the
// API with the session cookie like the app does. A browser signed in another way (a token, Telegram)
// holds no cookie, and a frame's navigation cannot carry a header, so the cookie is asked for once
// before the first frame is drawn. The bridge (pagebridge.ts) carries the theme, toasts and navigation.

import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, type PageDetail } from "../api";
import { Icon } from "../icons";
import { PageHeader, useMedia } from "../ui/index";
import { OverflowMenu, type MenuItem } from "../ui/dialogs";
import { useEvent } from "../events";
import { invalidate, useQuery } from "../store";
import { navigate, pathFor } from "../router";
import { shortDateTime } from "../format";
import { t, useLang } from "../i18n";
import { serveBridge, themeSnapshot } from "../pagebridge";
import "./pagehost.css";

let signedIn: Promise<void> | null = null;

/** The session cookie for this browser, asked for once per load; a failure is asked again next time. */
function ensureCookie(): Promise<void> {
  signedIn ??= api.post("/api/pages/_signin").then(
    () => undefined,
    (error) => {
      signedIn = null;
      throw error;
    },
  );
  return signedIn;
}

/** The title in the reader's language: one per language when the page has them, else the one it has. */
export function pageTitle(title: string | Record<string, string> | undefined, lang: string, slug: string): string {
  if (!title) return slug;
  if (typeof title === "string") return title;
  return title[lang] || title.en || Object.values(title)[0] || slug;
}

export function PageHostScreen({ slug, toast }: { slug: string; toast: (text: string) => void }) {
  const key = `/api/pages/${encodeURIComponent(slug)}`;
  const { data, error, refresh } = useQuery<PageDetail>(key, { staleMs: 2000 });
  const [lang] = useLang();
  const phone = useMedia("(max-width: 1023px)");
  const [draft, setDraft] = useState(false);
  const [reloads, setReloads] = useState(0);
  const [cookie, setCookie] = useState<"waiting" | "ready" | "failed">("waiting");
  const [loaded, setLoaded] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  // Read by the bridge on every push, so it never holds a stale language or width.
  const live = useRef({ phone, lang });
  live.current = { phone, lang };

  useEffect(() => {
    let gone = false;
    ensureCookie().then(
      () => !gone && setCookie("ready"),
      () => !gone && setCookie("failed"),
    );
    return () => {
      gone = true;
    };
  }, []);
  // Another page is another document: nothing of the last one's state carries over.
  useEffect(() => {
    setDraft(false);
    setLoaded(false);
  }, [slug]);
  // A publish or a rollback (from an agent, another window) redraws the page at its new version.
  useEvent(["pages.changed"], (event) => {
    if (event.payload.slug !== slug) return;
    invalidate(key);
    invalidate("/api/pages");
  }, [slug, key]);

  const missing = error !== null && !data && /404|no page/i.test(error);
  const nothingLive = !!data && data.version === null;
  const showDraft = draft || nothingLive;
  const src = data ? `${data.url}${showDraft ? "?draft=1" : ""}` : null;
  const frameKey = `${slug}:${data?.version ?? "-"}:${showDraft ? "d" : "l"}:${reloads}`;

  useEffect(() => {
    setLoaded(false);
  }, [frameKey]);

  useEffect(() => {
    const element = frame.current;
    if (!element || cookie !== "ready" || !src) return;
    const bridge = serveBridge(element, {
      theme: () => themeSnapshot(document.documentElement, live.current.phone, live.current.lang),
      toast,
      navigate: (path) => navigate(path),
      openFile: (sessionId, path) => navigate(pathFor("agents", sessionId, { panel: "preview", path })),
    });
    return bridge.stop;
  }, [frameKey, cookie, src, toast]);

  const title = pageTitle(data?.title, lang, slug);
  const external = data ? (data.kind === "link" && typeof data.target === "string" ? data.target : src) : null;

  const rollback = async (n: number) => {
    try {
      await api.post(`${key}/rollback`, { version: n });
      setDraft(false);
      invalidate("/api/pages");
      await refresh();
      toast(t("page.rollback.done", { n }));
    } catch (e) {
      toast(t("page.rollback.failed", { error: e instanceof ApiError ? e.message : String(e) }));
    }
  };

  const menu = useMemo<MenuItem[]>(() => {
    if (!data) return [];
    // A manifest without versions (an older host, a stub) is a page never published, not a crash.
    const items: MenuItem[] = (data.versions ?? []).map((v) => ({
      label: `${t("page.version", { n: v.n })} · ${shortDateTime(v.at)}`,
      hint: v.note || undefined,
      checked: !showDraft && v.n === data.version,
      onSelect: () => {
        if (v.n === data.version) setDraft(false);
        else void rollback(v.n);
      },
    }));
    if (!nothingLive) {
      if (items.length) items.push("-");
      items.push({ label: t(showDraft ? "page.showLive" : "page.showDraft"), icon: "eye", onSelect: () => setDraft((d) => !d) });
    }
    return items;
  }, [data, showDraft, nothingLive, lang]);

  const actions = data ? (
    <>
      {showDraft && <span className="page-host-badge" data-page-draft>{t("page.draftBadge")}</span>}
      <button className="iconbtn" onClick={() => setReloads((n) => n + 1)} aria-label={t("page.reload")} title={t("page.reload")} data-page-reload>
        <Icon name="reload" />
      </button>
      {external && (
        <a className="iconbtn" href={external} target="_blank" rel="noopener" aria-label={t("page.newTab")} title={t("page.newTab")} data-page-newtab>
          <Icon name="external" />
        </a>
      )}
      {menu.length > 0 && <OverflowMenu items={menu} label={t("page.versions")} icon="clock" className="page-host-versions" />}
    </>
  ) : undefined;

  let body;
  if (missing) {
    body = (
      <div className="screen"><div className="empty" data-page-state="missing">
        <b>{t("page.missing")}</b>
        <div>{t("page.missing.sub")}</div>
        <button className="btn primary" onClick={() => navigate(pathFor("agents"))}>{t("page.home")}</button>
      </div></div>
    );
  } else if (error && !data) {
    body = (
      <div className="screen"><div className="empty" data-page-state="error">
        <b>{t("page.error")}</b>
        <div>{error}</div>
        <button className="btn primary" onClick={() => void refresh()}>{t("common.retry")}</button>
      </div></div>
    );
  } else if (cookie === "failed") {
    body = (
      <div className="screen"><div className="empty" data-page-state="error">
        <b>{t("page.error")}</b>
        <div>{t("page.signin.failed")}</div>
        <button className="btn primary" onClick={() => { setCookie("waiting"); ensureCookie().then(() => setCookie("ready"), () => setCookie("failed")); }}>{t("common.retry")}</button>
      </div></div>
    );
  } else if (!data || cookie !== "ready" || !src) {
    body = <div className="page-host-loading" data-page-state="loading">{t("page.loading")}</div>;
  } else {
    body = (
      <div className="page-host-stage">
        {nothingLive && <div className="page-host-note" data-page-state="unpublished">{t("page.unpublished")}</div>}
        <iframe
          key={frameKey}
          ref={frame}
          className="page-host-frame"
          src={src}
          title={t("page.frame", { title })}
          allow="clipboard-read; clipboard-write; fullscreen; geolocation; camera; microphone; display-capture; web-share; autoplay; xr-spatial-tracking"
          allowFullScreen
          onLoad={() => setLoaded(true)}
          data-page-frame={slug}
        />
        {!loaded && <div className="page-host-loading over" aria-hidden>{t("page.loading")}</div>}
      </div>
    );
  }

  return (
    <div className={`page-host ${phone ? "phone" : ""}`} data-page={slug}>
      <PageHeader title={data || !missing ? title : t("nav.p")} actions={actions} />
      {body}
    </div>
  );
}
