// Search inside one conversation, from its header: the words typed are looked up in this chat's own
// history on the host, and a hit opens the chat at that message, however far back it is. The
// sidebar's search finds a conversation; this one finds the place in it, which an orchestrator's chat
// of several weeks needs more than any other.

import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import { snippetParts } from "./anchor";
import { Popover } from "./dialogs";
import { shortDateTime } from "./format";
import { t } from "./i18n";
import { Icon } from "./icons";

/** One matching message: its seq (what the chat opens at), a passage with the matches in [brackets],
 *  who wrote it, and when. */
export type ChatHit = { seq: number; snippet: string; role: string; at: string; origin?: string | null };
export type ChatSearchAnswer = { hits: ChatHit[]; partial?: boolean; semantic?: boolean; reason?: string };

/** How long typing has to pause before the host is asked: a search per key would queue a dozen. */
const DEBOUNCE_MS = 300;
const LIMIT = 30;

type State = { kind: "idle" } | { kind: "loading" } | { kind: "busy" } | { kind: "failed" } | { kind: "done"; answer: ChatSearchAnswer };

/** Who wrote a hit. A user message with an origin other than the operator's is the host's wake-up,
 *  a schedule's prompt or a batch of events, which the chat draws as a system note, not as "You". */
function roleWord(role: string, origin?: string | null): string {
  if (role === "user") return !origin || origin === "operator" || origin.startsWith("inbound") ? t("chatsearch.role.user") : t("chatsearch.role.system");
  if (role === "assistant") return t("chatsearch.role.assistant");
  if (role === "tool") return t("chatsearch.role.tool");
  return t("chatsearch.role.system");
}

/** The header's search button and the list it opens. `onPick` is given the seq of the chosen hit. */
export function ChatSearch({ sessionId, onPick }: { sessionId: string; onPick: (seq: number) => void }) {
  const button = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  return (
    <>
      <button ref={button} type="button" className={`iconbtn chat-search-btn ${open ? "on" : ""}`} onClick={() => setOpen((o) => !o)} aria-label={t("chatsearch.open")} title={t("chatsearch.open")} aria-expanded={open}>
        <Icon name="search" />
      </button>
      {open && (
        <Popover anchor={button.current} align="right" className="chat-search-pop" label={t("chatsearch.open")} onClose={() => setOpen(false)}>
          <ChatSearchBody
            sessionId={sessionId}
            onPick={(seq) => {
              setOpen(false);
              onPick(seq);
            }}
          />
        </Popover>
      )}
    </>
  );
}

function ChatSearchBody({ sessionId, onPick }: { sessionId: string; onPick: (seq: number) => void }) {
  // Kept across openings of the same chat, so a second look at the hits does not start from nothing.
  const [query, setQuery] = useState(() => remembered.get(sessionId) ?? "");
  const [state, setState] = useState<State>({ kind: "idle" });
  /** Answers come back in whatever order the network gives them; only the newest question's lands. */
  const asked = useRef(0);
  useEffect(() => {
    remembered.set(sessionId, query);
    const q = query.trim();
    const mine = ++asked.current;
    if (!q) {
      setState({ kind: "idle" });
      return;
    }
    setState({ kind: "loading" });
    const timer = window.setTimeout(() => {
      api
        .get<ChatSearchAnswer>(`/api/sessions/${encodeURIComponent(sessionId)}/search?q=${encodeURIComponent(q)}&limit=${LIMIT}`)
        .then((answer) => {
          if (mine === asked.current) setState({ kind: "done", answer });
        })
        .catch((e) => {
          // 429 is the host's search running for someone else already: nothing is wrong, it is only
          // not now, and saying "unavailable" there sent people to Settings for nothing.
          if (mine === asked.current) setState({ kind: e instanceof ApiError && e.status === 429 ? "busy" : "failed" });
        });
    }, DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query, sessionId]);
  const hits = state.kind === "done" ? state.answer.hits : [];
  return (
    <div className="chat-search">
      <input
        type="search"
        className="field search"
        value={query}
        maxLength={500}
        placeholder={t("chatsearch.placeholder")}
        aria-label={t("chatsearch.placeholder")}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && hits.length) {
            e.preventDefault();
            onPick(hits[0].seq);
          }
        }}
      />
      {state.kind === "loading" && <div className="chat-search-note sub" role="status">{t("chatsearch.loading")}</div>}
      {state.kind === "busy" && <div className="chat-search-note sub" role="status">{t("chatsearch.busy")}</div>}
      {state.kind === "failed" && <div className="chat-search-note sub" role="status">{t("chatsearch.failed")}</div>}
      {state.kind === "done" && !hits.length && <div className="chat-search-note sub" role="status">{t("chatsearch.empty")}</div>}
      {hits.length > 0 && (
        <div className="chat-search-hits">
          {hits.map((hit) => (
            <button key={hit.seq} type="button" className="chat-hit" data-seq={hit.seq} onClick={() => onPick(hit.seq)}>
              <span className="chat-hit-meta">
                <b>{roleWord(hit.role, hit.origin)}</b>
                <span>{shortDateTime(hit.at)}</span>
              </span>
              <span className="chat-hit-snippet">
                {snippetParts(hit.snippet).map((part, i) => (part.hit ? <mark key={i}>{part.text}</mark> : <span key={i}>{part.text}</span>))}
              </span>
            </button>
          ))}
        </div>
      )}
      {state.kind === "done" && state.answer.partial && <div className="chat-search-note sub">{t("chatsearch.partial")}</div>}
    </div>
  );
}

/** The last words searched in each chat, for as long as the page is open. */
const remembered = new Map<string, string>();
