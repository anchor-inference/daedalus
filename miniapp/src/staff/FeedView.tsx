// The Feed: a command-line member's transcript as a conversation, read from its CLI's own store and
// never from the screen. It is the phone's default view of a member (a TUI at 390 px is a keyhole)
// and the desktop's second one. It asks again from its last turn, because that turn is the one still
// being written, and merges what comes back by turn.

import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { StaffTurn } from "../api";
import { useEvent } from "../events";
import { clock, tokens, usd } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { renderMarkdown } from "../md";
import { RawHtml } from "../rawhtml";
import { api } from "../api";
import { mergeTurns, nextSince } from "./model";

/** How often a working member's Feed asks again when the event stream says nothing. */
const POLL_MS = 5000;

export function useTurns(staffId: string, live: boolean): { turns: StaffTurn[]; loaded: boolean; error: string } {
  const [turns, setTurns] = useState<StaffTurn[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState("");
  const held = useRef<StaffTurn[]>([]);
  const [poke, setPoke] = useState(0);
  useEvent(["staff.status", "staff.report", "staff.message"], (event) => {
    if (event.staff_id === staffId) setPoke((n) => n + 1);
  }, [staffId]);
  useEffect(() => {
    held.current = [];
    setTurns([]);
    setLoaded(false);
  }, [staffId]);
  useEffect(() => {
    if (!live) return;
    let stop = false;
    async function read() {
      try {
        const page = await api.get<{ turns: StaffTurn[] }>(`/api/staff/${encodeURIComponent(staffId)}/transcript?since=${nextSince(held.current)}`);
        if (stop) return;
        const merged = mergeTurns(held.current, Array.isArray(page.turns) ? page.turns : []);
        if (merged !== held.current) {
          held.current = merged;
          setTurns(merged);
        }
        setError("");
      } catch (e) {
        if (!stop) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!stop) setLoaded(true);
      }
    }
    void read();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void read();
    }, POLL_MS);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [staffId, live, poke]);
  return { turns, loaded, error };
}

function TurnTools({ tools }: { tools: StaffTurn["tools"] }) {
  const [open, setOpen] = useState(false);
  if (tools.length === 0) return null;
  const failed = tools.filter((tool) => tool.ok === false).length;
  return (
    <div className={`feed-tools ${open ? "open" : ""}`}>
      <button className="feed-tools-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span>{plural("staff.feed.tools", tools.length)}</span>
        {failed > 0 && <span className="feed-tools-failed">{plural("staff.feed.failed", failed)}</span>}
        {/* The chevron turns when the group opens: without it a folded and an open group looked the
            same, and the rows under "3 tools" read as a separate list. */}
        <span className="feed-tools-chevron"><Icon name="forward" size={12} /></span>
      </button>
      {open && (
        <ul className="feed-tool-list">
          {tools.map((tool, i) => (
            <li key={i} className={`feed-tool ${tool.ok === false ? "failed" : ""}`}>
              <span className="feed-tool-name mono">{tool.name}</span>
              <span className="feed-tool-summary truncate">{tool.summary}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FeedTurn({ turn, name }: { turn: StaffTurn; name: string }) {
  const html = useMemo(() => (turn.role === "assistant" && turn.text ? renderMarkdown(turn.text) : ""), [turn.role, turn.text]);
  const who = turn.role === "orchestrator" ? t("staff.feed.orchestrator") : turn.role === "user" ? t("staff.feed.you") : turn.role === "system" ? t("staff.feed.system") : name;
  const spend = turn.usage ? [turn.usage.input_tokens + turn.usage.cache_read_tokens ? `${tokens(turn.usage.input_tokens + turn.usage.cache_read_tokens)}↑` : "", turn.usage.output_tokens ? `${tokens(turn.usage.output_tokens)}↓` : "", turn.usage.cost_usd ? usd(turn.usage.cost_usd) : ""].filter(Boolean).join(" ") : "";
  return (
    <article className={`feed-turn ${turn.role}`} data-turn={turn.index} data-role={turn.role}>
      <div className="feed-turn-head">
        <span className="feed-turn-who">{who}</span>
        {turn.started_at && <span className="feed-turn-at">{clock(turn.started_at)}</span>}
        {spend && <span className="feed-turn-spend">{spend}</span>}
      </div>
      {turn.role === "assistant" ? (
        html ? <RawHtml className="answer feed-answer" html={html} /> : null
      ) : (
        turn.text && <div className="feed-prompt">{turn.text}</div>
      )}
      <TurnTools tools={turn.tools ?? []} />
    </article>
  );
}

/**
 * The member's turns, newest at the bottom, kept in view as they arrive unless the reader scrolled up.
 * `tail` goes after the last turn (the messages still on their way); `tailKey` changes with it, so a
 * receipt that moves keeps the bottom in view as a new turn does.
 */
export function FeedView({ name, live, feed, tail, tailKey = "" }: { name: string; live: boolean; feed: { turns: StaffTurn[]; loaded: boolean; error: string }; tail?: ReactNode; tailKey?: string }) {
  const { turns, loaded, error } = feed;
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [turns, tailKey]);
  return (
    <div
      className="chat-scroll feed"
      ref={scroller}
      onScroll={(e) => {
        const el = e.currentTarget;
        pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
      }}
    >
      <div className="feed-turns">
        {!live && <div className="empty calm">{t("staff.feed.nolive")}</div>}
        {live && loaded && turns.length === 0 && <div className="empty calm">{error || t("staff.feed.empty")}</div>}
        {turns.map((turn) => <FeedTurn key={turn.index} turn={turn} name={name} />)}
        {tail}
      </div>
    </div>
  );
}
