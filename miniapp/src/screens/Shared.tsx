// The page a shared link opens. It is the dialog and nothing else: no composer, no tools, no
// files. A guest is not signed in, and a signed-in operator who opens the link sees this page
// too — the link is a page, not a way into the app.

import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { api, ApiError, type MediaPresentation } from "../api";
import { timeAgo } from "../ui/components";
import { Icon, type IconName } from "../icons";
import { t, useLang } from "../i18n";
import { renderMarkdown } from "../md";
import { RawHtml } from "../rawhtml";
import { splitMediaAnswer, type AnswerPart } from "../mediaformat";
import { bytes } from "../format";
import "../widget.css";

type SharedMessage = { role: "user" | "assistant"; text: string; at: string; via?: string; media?: MediaPresentation[]; widgets?: string[] };

/** `app_path`: the conversation in the app, where a widget the page can only name is drawn. */
type SharedPage = { title: string; older: boolean; messages: SharedMessage[]; app_path?: string };

const REFRESH_MS = 20_000;

function Md({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => renderMarkdown(text), [text]);
  return <RawHtml className={className} html={html} />;
}

function albumRatio(items: MediaPresentation["items"]): string | undefined {
  const photo = items.find((item) => (item.kind === "image" || item.kind === "animation") && item.width && item.height);
  return photo?.width && photo.height ? `${photo.width} / ${photo.height}` : undefined;
}

function Pictures({ presentation }: { presentation: MediaPresentation }) {
  // An attached document is served to a guest as a download, never inline, so it is a link with its
  // name and size; drawn as an <img> it was a broken picture.
  const file = presentation.items.length === 1 && presentation.items[0].kind === "file" ? presentation.items[0] : null;
  if (file) return (
    <a className="shared-file" href={file.url} download={file.filename}>
      <Icon name="download" size={15} />
      <span className="truncate">{file.filename}</span>
      {file.byte_size ? <span className="shared-file-size">{bytes(file.byte_size)}</span> : null}
    </a>
  );
  const ratio = presentation.layout === "album" ? albumRatio(presentation.items) : undefined;
  return (
    <div className={`inline-media ${presentation.layout} items-${Math.min(4, presentation.items.length)}`} style={ratio ? ({ "--album-ratio": ratio } as CSSProperties) : undefined}>
      <div className="inline-media-track">
        {presentation.items.map((item) =>
          item.kind === "audio" ? (
            <audio key={item.id} className="shared-audio" src={item.url} controls preload="metadata" />
          ) : (
            <figure key={item.id} className="inline-media-image">
              {item.kind === "video" ? <video src={item.url} controls playsInline preload="metadata" /> : <img src={item.url} alt={item.alt} />}
              {item.caption ? <figcaption>{item.caption}</figcaption> : null}
            </figure>
          ),
        )}
      </div>
    </div>
  );
}

function Answer({ message }: { message: SharedMessage }) {
  const parts = splitMediaAnswer(message.text, message.media ?? []);
  if (parts.length === 1 && parts[0].kind === "text") return <Md className="answer" text={message.text} />;
  return (
    <div className="answer answer-with-media">
      {parts.map((part: AnswerPart, index) =>
        part.kind === "text" ? <Md key={`text-${index}`} text={part.text} /> : <Pictures key={part.presentation.id} presentation={part.presentation} />,
      )}
    </div>
  );
}

/** A widget the agent drew, which this page cannot run: what it holds is the operator's, and a guest
 *  is not signed in. Its title, and the way to the conversation where it works. */
function StaticWidget({ title, appPath }: { title: string; appPath: string }) {
  return (
    <div className="widget-static" data-widget-static>
      <span className="artifact-glyph" aria-hidden><Icon name="sliders" size={18} /></span>
      <span className="widget-static-text">
        <span className="widget-static-title truncate">{title || t("widget.untitled")}</span>
        <span className="widget-static-meta">{t("widget.static.meta")}</span>
      </span>
      <a className="btn small" href={appPath}>{t("widget.static.open")}</a>
    </div>
  );
}

/** A page with nothing to show yet, or nothing it may show: said in the middle of the window, with
 *  what to do next. Pinned under the header as one bold line it read as a blank page left unfinished,
 *  and a refused link was a dead end with no way on. */
function SharedState({ icon, title, body, action }: { icon?: IconName; title: string; body?: string; action?: { label: string; onClick: () => void } }) {
  return (
    <div className="shared-state" role="status">
      {icon && <span className="shared-state-icon"><Icon name={icon} size={22} /></span>}
      <b>{title}</b>
      {body && <p className="sub">{body}</p>}
      {action && <button type="button" className="btn" onClick={action.onClick}>{action.label}</button>}
    </div>
  );
}

export function SharedDialog({ slug }: { slug: string }) {
  useLang();
  const [page, setPage] = useState<SharedPage | null>(null);
  const [error, setError] = useState<number | null>(null);
  const [attempt, setAttempt] = useState(0);
  const had = useRef(false);

  useEffect(() => {
    const tag = document.createElement("meta");
    tag.name = "robots";
    tag.content = "noindex, nofollow";
    document.head.appendChild(tag);
    return () => tag.remove();
  }, []);

  useEffect(() => {
    let stopped = false;
    const load = () => {
      api
        .get<SharedPage>(`/c/${encodeURIComponent(slug)}/transcript`)
        .then((next) => {
          if (stopped) return;
          had.current = true;
          setPage(next);
          setError(null);
          document.title = next.title || t("share.page.kicker");
        })
        .catch((reason: unknown) => {
          if (stopped) return;
          const status = reason instanceof ApiError ? reason.status : 0;
          // A later refresh that merely failed to connect leaves the page that is already open.
          // A refusal does not: the link was turned off, or it never had its key.
          if (status === 404 || status === 403 || !had.current) {
            had.current = false;
            setPage(null);
            setError(status || 500);
            document.title = t("share.page.kicker");
          }
        });
    };
    load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") load();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [slug, attempt]);

  const retry = () => {
    setError(null);
    setAttempt((n) => n + 1);
  };
  const title = page?.title || t("share.page.kicker");
  return (
    <div className="shared">
      <div className="shared-column">
        <header className="shared-head">
          <div className="shared-titles">
            {/* The kicker says what kind of page this is above the dialog's own name; with no name
                to show it would only repeat the heading under it. */}
            {page?.title && <div className="shared-kicker">{t("share.page.kicker")}</div>}
            <h1>{title}</h1>
          </div>
        </header>
        <div className="shared-scroll">
          {error === 404 && <SharedState icon="unlink" title={t("share.page.gone")} body={t("share.page.gone.next")} />}
          {error === 403 && <SharedState icon="lock" title={t("share.page.locked")} body={t("share.page.locked.next")} />}
          {error !== null && error !== 404 && error !== 403 && <SharedState icon="alert" title={t("share.page.failed")} body={t("share.page.failed.next")} action={{ label: t("common.retry"), onClick: retry }} />}
          {error === null && page && page.messages.length === 0 && <SharedState title={t("share.page.empty")} />}
          {error === null && !page && <SharedState title={t("common.loading")} />}
          {page?.older && <div className="sub older-note">{t("share.page.older")}</div>}
          {page?.messages.map((message, index) => (
            <article key={`${message.at}-${index}`} className="turn">
              {message.role === "user" ? (
                <div className="msg user">
                  {message.via ? <span className="msg-origin">{t("turn.origin.inbound", { source: message.via })}</span> : null}
                  <Md text={message.text} />
                  {message.at ? <time className="shared-time" dateTime={message.at}>{timeAgo(message.at)}</time> : null}
                </div>
              ) : (
                <>
                  {(message.text || message.media?.length) ? <Answer message={message} /> : null}
                  {message.widgets?.map((title, n) => <StaticWidget key={n} title={title} appPath={page.app_path || "/app/"} />)}
                  {message.at ? <time className="shared-time" dateTime={message.at}>{timeAgo(message.at)}</time> : null}
                </>
              )}
            </article>
          ))}
        </div>
      </div>
    </div>
  );
}
