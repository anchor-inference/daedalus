// A custom page inside the app: the screen at /app/p/:slug frames the page the host serves at /pages/:slug/.
//
// The page is the operator's own and same-origin, so the frame is not sandboxed: the page calls the
// API with the session cookie like the app does. A browser signed in another way (a token, Telegram)
// holds no cookie, and a frame's navigation cannot carry a header, so the cookie is asked for once
// before the first frame is drawn. The bridge (pagebridge.ts) carries the theme, toasts and navigation.
//
// A page may have an agent of its own, the session bound to it. Its conversation opens beside the page
// on a desktop (a column the operator drags wider, remembered open or closed per page) and in place
// of the page on a phone, where the page stays loaded underneath so switching back loses nothing.
// Everything the app hears about the page — what the agent pushes with PageEmit, the agent's replies
// and runs — is forwarded into the frame.
//
// A new version never reloads the page under the operator: the frame stays on the version it opened
// with and a chip offers the new one, so a half-filled form or a turned model is not lost because the
// agent published while the operator was using the page.

import { Component, Suspense, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { ApiError, api, type PageDetail, type SessionDetail } from "../api";
import { Icon } from "../icons";
import { PageHeader, useMedia } from "../ui/index";
import { OverflowMenu, type MenuItem } from "../ui/dialogs";
import { onEvent, useEvent } from "../events";
import { invalidate, useQuery } from "../store";
import { navigate, pathFor, sessionPath } from "../router";
import { shortDateTime } from "../format";
import { t, useLang } from "../i18n";
import { forwardToPage, serveBridge, themeSnapshot } from "../pagebridge";
import { pageTitle } from "../pagecard";
import { SessionScreen } from "../chunks";
import { SessionPicker } from "../sessionpicker";
import { PaneHandle, clampWidth, pixelDrag, readLayout, writeLayout } from "../layout";
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

/** The agent column's width: the operator's drag, kept per browser; the stylesheet's own until then. */
const AGENT_W_KEY = "page-agent-w";
const AGENT_W_MIN = 320;
/** What the page keeps beside a dragged column, so the page never shrinks to nothing. */
const PAGE_W_MIN = 360;
const AGENT_STEP = 24;

/** Whether the agent is shown beside one page: remembered per page, closed until asked for. */
export function agentShown(slug: string): boolean {
  return readLayout(`page-agent.${slug}`) === "open";
}

function rememberAgentShown(slug: string, open: boolean): void {
  writeLayout(`page-agent.${slug}`, open ? "open" : "closed");
}

/** The answers of the bound session, newest last, for the replies the page is handed. */
async function latestMessages(sessionId: string) {
  const detail = await api.get<SessionDetail>(`/api/sessions/${encodeURIComponent(sessionId)}?tail=12`);
  return detail.messages ?? [];
}

class AgentBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return <div className="empty" role="alert">
      <b>{t("page.agent.crashed")}</b>
      <button type="button" className="btn" onClick={() => this.setState({ failed: false })}>{t("common.retry")}</button>
    </div>;
  }
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
  // The version and the draft the frame opened with; a newer one waits behind the chip.
  const [shownVersion, setShownVersion] = useState<number | null | undefined>(undefined);
  const [shownDraft, setShownDraft] = useState<string | undefined>(undefined);
  const [agentOpen, setAgentOpen] = useState(() => agentShown(slug));
  const [picking, setPicking] = useState(false);
  const [agentWidth, setAgentWidth] = useState<number | null>(() => {
    const kept = Number(readLayout(AGENT_W_KEY));
    return kept >= AGENT_W_MIN ? kept : null;
  });
  const frame = useRef<HTMLIFrameElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const aside = useRef<HTMLElement>(null);
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
  // An address that asks for the draft (an agent showing its work before publishing) opens on it.
  useEffect(() => {
    setDraft(new URLSearchParams(window.location.search).get("draft") === "1");
    setLoaded(false);
    setShownVersion(undefined);
    setShownDraft(undefined);
    setAgentOpen(agentShown(slug));
  }, [slug]);
  // A publish, a rollback, a binding (from an agent, another window): the manifest is read again. The
  // frame does not follow by itself; the chip below offers what changed.
  useEvent(["pages.changed"], (event) => {
    if (event.payload.slug !== slug) return;
    invalidate(key);
    invalidate("/api/pages");
  }, [slug, key]);

  const missing = error !== null && !data && /404|no page/i.test(error);
  const nothingLive = !!data && data.version === null;
  const showDraft = draft || nothingLive;
  const draftStamp = data?.draft?.updated_at ?? "";
  // The first manifest decides what the frame shows; later ones only light the chip.
  useEffect(() => {
    if (!data) return;
    if (shownVersion === undefined) setShownVersion(data.version);
    if (shownDraft === undefined) setShownDraft(draftStamp);
  }, [data, shownVersion, shownDraft, draftStamp]);
  const newerLive = !!data && !showDraft && shownVersion !== undefined && data.version !== null && data.version !== shownVersion;
  const newerDraft = !!data && showDraft && shownDraft !== undefined && draftStamp !== shownDraft;
  const src = data ? `${data.url}${showDraft ? "?draft=1" : ""}` : null;
  const frameKey = `${slug}:${showDraft ? `d:${shownDraft ?? ""}` : `l:${shownVersion ?? "-"}`}:${reloads}`;

  /** The frame on what is current now: the reload button, the chip, a version picked in the menu. */
  const catchUp = useCallback((version?: number | null) => {
    if (data) {
      setShownVersion(version === undefined ? data.version : version);
      setShownDraft(data.draft?.updated_at ?? "");
    }
    setReloads((n) => n + 1);
  }, [data]);

  useEffect(() => {
    setLoaded(false);
  }, [frameKey]);

  const agent = data?.agent ?? null;
  const agentId = agent && !agent.gone ? agent.id : null;
  const showAgent = agentOpen && !!agentId;
  const setShown = useCallback((open: boolean) => {
    setAgentOpen(open);
    rememberAgentShown(slug, open);
  }, [slug]);
  // Read by the forwarding on every event, so a rebinding takes effect without restarting it.
  const pageNow = useRef({ slug, sessionId: agentId });
  pageNow.current = { slug, sessionId: agentId };
  const openAgent = useRef<() => boolean>(() => false);
  openAgent.current = () => {
    if (!agentId) return false;
    setShown(true);
    return true;
  };

  useEffect(() => {
    const element = frame.current;
    if (!element || cookie !== "ready" || !src) return;
    const bridge = serveBridge(element, {
      theme: () => themeSnapshot(document.documentElement, live.current.phone, live.current.lang),
      toast,
      navigate: (path) => navigate(path),
      openFile: (sessionId, path) => navigate(pathFor("agents", sessionId, { panel: "preview", path })),
      openAgent: () => openAgent.current(),
    });
    const forwarding = forwardToPage(bridge.send, () => pageNow.current, { on: onEvent, messages: latestMessages });
    return () => {
      forwarding();
      bridge.stop();
    };
  }, [frameKey, cookie, src, toast]);

  const title = pageTitle(data?.title, lang, slug);
  const external = data ? (data.kind === "link" && typeof data.target === "string" ? data.target : src) : null;

  const rollback = async (n: number) => {
    try {
      await api.post(`${key}/rollback`, { version: n });
      setDraft(false);
      invalidate("/api/pages");
      await refresh();
      catchUp(n);
      toast(t("page.rollback.done", { n }));
    } catch (e) {
      toast(t("page.rollback.failed", { error: e instanceof ApiError ? e.message : String(e) }));
    }
  };

  const bind = async (sessionId: string | null) => {
    try {
      const page = await api.patch<PageDetail>(key, { session_id: sessionId });
      await refresh();
      if (sessionId) {
        setShown(true);
        toast(t("page.agent.bound", { title: page.agent?.title || sessionId }));
      } else {
        toast(t("page.agent.unbound"));
      }
    } catch (e) {
      toast(t("page.agent.failed", { error: e instanceof ApiError ? e.message : String(e) }));
    }
  };

  const newAgent = async () => {
    try {
      const made = await api.post<{ session_id: string; title: string }>(`${key}/agent`, { title: t("page.agent.sessionTitle", { title }) });
      await refresh();
      setShown(true);
      toast(t("page.agent.bound", { title: made.title }));
    } catch (e) {
      toast(t("page.agent.failed", { error: e instanceof ApiError ? e.message : String(e) }));
    }
  };

  const menu = useMemo<MenuItem[]>(() => {
    if (!data) return [];
    // A manifest without versions (an older host, a stub) is a page never published, not a crash.
    const items: MenuItem[] = (data.versions ?? []).map((v) => ({
      label: `${t("page.version", { n: v.n })} · ${shortDateTime(v.at)}`,
      hint: v.note || undefined,
      checked: !showDraft && v.n === shownVersion,
      onSelect: () => {
        if (v.n === data.version) {
          setDraft(false);
          if (v.n !== shownVersion) catchUp(v.n);
        } else void rollback(v.n);
      },
    }));
    if (!nothingLive) {
      if (items.length) items.push("-");
      items.push({ label: t(showDraft ? "page.showLive" : "page.showDraft"), icon: "eye", onSelect: () => { setDraft((d) => !d); catchUp(); } });
    }
    return items;
  }, [data, showDraft, nothingLive, shownVersion, lang, catchUp]);

  const agentItems: MenuItem[] = [
    ...(agentId && !phone ? [{ label: t(showAgent ? "page.agent.hide" : "page.agent.show"), icon: "panel" as const, onSelect: () => setShown(!showAgent) }] : []),
    ...(agentId ? [{ label: t("page.agent.openFull"), icon: "external" as const, onSelect: () => navigate(sessionPath(agentId)) }, "-" as const] : []),
    { label: t(agent ? "page.agent.change" : "page.agent.choose"), icon: "bots", onSelect: () => setPicking(true) },
    { label: t("page.agent.new"), icon: "plus", onSelect: () => void newAgent() },
    ...(agent ? ["-" as const, { label: t("page.agent.unbind"), icon: "unlink" as const, danger: true, onSelect: () => void bind(null) }] : []),
  ];
  const agentWord = agent ? (agent.gone ? t("page.agent.gone") : t("page.agent.chip", { title: agent.title || agent.id })) : t("page.agent.none");

  const actions = data ? (
    <>
      {showDraft && <span className="page-host-badge" data-page-draft>{t("page.draftBadge")}</span>}
      {phone ? (
        agentId ? (
          <button className={`iconbtn ${showAgent ? "on" : ""}`} onClick={() => setShown(!showAgent)} aria-pressed={showAgent} aria-label={t("page.agent.toggle")} title={t("page.agent.toggle")} data-page-agent-toggle>
            <Icon name="bots" />
          </button>
        ) : null
      ) : (
        <>
          <OverflowMenu
            items={agentItems}
            label={t("page.agent.menu")}
            className={`chip page-agent-chip ${agent?.gone ? "warn" : ""}`}
            trigger={<><Icon name="bots" size={14} /><span className="truncate" data-page-agent-name>{agentWord}</span><Icon name="chevron" size={12} /></>}
          />
          {agentId && (
            <button className={`iconbtn ${showAgent ? "on" : ""}`} onClick={() => setShown(!showAgent)} aria-pressed={showAgent} aria-label={t("page.agent.toggle")} title={t("page.agent.toggle")} data-page-agent-toggle>
              <Icon name="panel" />
            </button>
          )}
        </>
      )}
      <button className="iconbtn" onClick={() => catchUp()} aria-label={t("page.reload")} title={t("page.reload")} data-page-reload>
        <Icon name="reload" />
      </button>
      {external && !phone && (
        <a className="iconbtn" href={external} target="_blank" rel="noopener" aria-label={t("page.newTab")} title={t("page.newTab")} data-page-newtab>
          <Icon name="external" />
        </a>
      )}
      {menu.length > 0 && !phone && <OverflowMenu items={menu} label={t("page.versions")} icon="clock" className="page-host-versions" />}
      {/* A phone has no room for a chip and three buttons: the agent's and the versions' choices share one menu. */}
      {phone && <OverflowMenu
        items={[...menu, ...(menu.length ? ["-" as const] : []), ...(external ? [{ label: t("page.newTab"), icon: "external" as const, onSelect: () => window.open(external, "_blank", "noopener") }] : []), ...agentItems]}
        label={t("page.versions")}
        icon="vdots"
        className="page-host-versions"
      />}
    </>
  ) : undefined;

  const agentDrag = {
    ...pixelDrag(
      () => body.current,
      "--page-agent-w",
      () => aside.current?.getBoundingClientRect().width ?? agentWidth ?? 420,
      (w) => clampWidth(w, AGENT_W_MIN, Math.max(AGENT_W_MIN, (body.current?.clientWidth ?? window.innerWidth) - PAGE_W_MIN)),
      (w) => {
        setAgentWidth(w);
        writeLayout(AGENT_W_KEY, String(w));
      },
      -1,
    ),
    step: AGENT_STEP,
    reset: () => {
      body.current?.style.removeProperty("--page-agent-w");
      setAgentWidth(null);
      writeLayout(AGENT_W_KEY, "");
    },
  };

  let content;
  if (missing) {
    content = (
      <div className="screen"><div className="empty" data-page-state="missing">
        <b>{t("page.missing")}</b>
        <div>{t("page.missing.sub")}</div>
        <button className="btn primary" onClick={() => navigate(pathFor("agents"))}>{t("page.home")}</button>
      </div></div>
    );
  } else if (error && !data) {
    content = (
      <div className="screen"><div className="empty" data-page-state="error">
        <b>{t("page.error")}</b>
        <div>{error}</div>
        <button className="btn primary" onClick={() => void refresh()}>{t("common.retry")}</button>
      </div></div>
    );
  } else if (cookie === "failed") {
    content = (
      <div className="screen"><div className="empty" data-page-state="error">
        <b>{t("page.error")}</b>
        <div>{t("page.signin.failed")}</div>
        <button className="btn primary" onClick={() => { setCookie("waiting"); ensureCookie().then(() => setCookie("ready"), () => setCookie("failed")); }}>{t("common.retry")}</button>
      </div></div>
    );
  } else if (!data || cookie !== "ready" || !src) {
    content = <div className="page-host-loading" data-page-state="loading">{t("page.loading")}</div>;
  } else {
    const newer = newerLive || newerDraft;
    content = (
      <div
        ref={body}
        className={`page-host-body ${showAgent ? "with-agent" : ""}`}
        style={agentWidth != null ? ({ "--page-agent-w": `${agentWidth}px` } as CSSProperties) : undefined}
      >
        <div className="page-host-stage" aria-hidden={phone && showAgent ? true : undefined}>
          {nothingLive && !newer && <div className="page-host-note" data-page-state="unpublished">{t("page.unpublished")}</div>}
          {newer && (
            <button type="button" className="page-host-chip" onClick={() => catchUp()} data-page-newversion>
              <Icon name="reload" size={14} />
              <span>{newerLive ? t("page.newVersion", { n: data.version ?? 0 }) : t("page.draftChanged")}</span>
              <b>{t("page.reloadNow")}</b>
            </button>
          )}
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
        {showAgent && agentId && (
          <aside ref={aside} className="page-agent" data-page-agent={agentId} aria-label={t("page.agent.menu")}>
            {!phone && <PaneHandle side="left" drag={agentDrag} />}
            <AgentBoundary key={agentId}>
              <Suspense fallback={<div className="page-host-loading">{t("common.loading")}</div>}>
                <SessionScreen id={agentId} pane="right" onBack={() => setShown(false)} onOpen={(id) => navigate(sessionPath(id))} toast={toast} />
              </Suspense>
            </AgentBoundary>
          </aside>
        )}
      </div>
    );
  }

  return (
    <div className={`page-host ${phone ? "phone" : ""}`} data-page={slug}>
      <PageHeader title={data || !missing ? title : t("nav.p")} actions={actions} />
      {content}
      {picking && (
        <SessionPicker
          title={t("page.agent.pick")}
          exclude={agentId}
          onPick={(id) => {
            setPicking(false);
            void bind(id);
          }}
          onClose={() => setPicking(false)}
        />
      )}
    </div>
  );
}
