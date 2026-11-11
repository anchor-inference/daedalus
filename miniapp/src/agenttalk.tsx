// This chat's conversations with other agents: the Agents tab of the right panel, which holds one card per
// conversation with its thread and what only the operator can set, and the approval block over the composer
// for a message that waits to go out. The other side is a stranger, so everything it wrote, its description of
// itself included, is plain text in a frame that says whose it is. Nothing here turns it into markdown, a link
// or a picture.
//
// The conversations used to be strips over the composer, and a message waiting for the operator was one of
// them turning yellow: the message itself was a click and a sheet away, and was missed.

import { useEffect, useRef, useState } from "react";
import { AgentTalk, AgentTalkDetail, AgentTalkList, AgentTalkMessage, AgentTalkReview, api } from "./api";
import { absTime, usd } from "./format";
import { Icon } from "./icons";
import { t } from "./i18n";
import { invalidate, useQuery } from "./store";
import { Segmented, Switch } from "./ui/components";
import { confirmAsync, errorText } from "./ui";
import "./agenttalk.css";

const POLL_MS = 5000;
const WAKE_TURNS = 5;
const WAKE_USD = 0.5;
const SENDS = 30;
/** What the host stores for "no limit" on waking (`agent_talk.NO_LIMIT`); a cap on sends uses 0. */
export const NO_LIMIT = -1;

const encoded = encodeURIComponent;
const listPath = (sessionId: string) => `/api/sessions/${encoded(sessionId)}/agent-talks`;
const talkPath = (id: string) => `/api/agent-talks/${encoded(id)}`;

export function useAgentTalks(sessionId: string | null | undefined): { talks: AgentTalk[]; loaded: boolean } {
  const { data } = useQuery<AgentTalkList>(sessionId ? listPath(sessionId) : null, { pollMs: POLL_MS });
  return { talks: data?.talks ?? [], loaded: !!data };
}

/** What a conversation is doing, in one word and a tone: `attn` where it waits for the operator. */
export function talkState(talk: Pick<AgentTalk, "state" | "reason" | "waiting_for" | "quiet" | "held" | "unread">): { key: string; tone: "" | "attn" | "ok" } {
  if (talk.state === "closed") return { key: "closed", tone: "" };
  if (talk.held > 0) return { key: "held", tone: "attn" };
  if (talk.unread > 0) return { key: "unread", tone: "attn" };
  if (talk.waiting_for === "owner") return { key: "owner", tone: "" };
  if (talk.waiting_for === "reply") return { key: talk.quiet ? "quiet" : "waiting", tone: "" };
  return { key: "open", tone: "ok" };
}

export function talkCounts(talk: Pick<AgentTalk, "sends" | "sends_limit" | "replies" | "spent_usd">): string {
  const sent = talk.sends_limit > 0 ? t("talk.sent", { n: talk.sends, of: talk.sends_limit }) : t("talk.sent.free", { n: talk.sends });
  return [sent, t("talk.replies", { n: talk.replies }), usd(talk.spent_usd)].join(" · ");
}

/** What the tab's badge counts: messages waiting for the operator and replies the agent has not read yet. */
export function agentsWaiting(talks: readonly AgentTalk[]): number {
  return talks.filter((talk) => talk.state !== "closed").reduce((sum, talk) => sum + talk.held + talk.unread, 0);
}

type Seen = Map<string, { replies: number; held: number }>;

/** Whether the list moved in a way the operator should see at once: a conversation that was not there, a
 *  reply that came, a message that started waiting. Only a change counts, so the tab is opened once for it
 *  and a reader who closes it again is not dragged back on every poll. `before` null is the first read of a
 *  chat, which opens nothing: what was already there is what the chat was opened on. */
export function talksMoved(before: Seen | null, talks: readonly AgentTalk[]): boolean {
  if (before === null) return false;
  return talks.some((talk) => {
    const was = before.get(talk.id);
    if (!was) return talk.state !== "closed";
    return talk.replies > was.replies || (talk.held > 0 && talk.held > was.held);
  });
}

/** Opens the Agents tab when the conversations move (see `talksMoved`). Keyed on the chat, so another chat's
 *  list read in the same component is a first read again. */
export function useAgentsAutoOpen(sessionId: string, talks: readonly AgentTalk[], loaded: boolean, enabled: boolean, open: () => void) {
  const seen = useRef<{ session: string; map: Seen | null }>({ session: sessionId, map: null });
  const openRef = useRef(open);
  openRef.current = open;
  useEffect(() => {
    if (seen.current.session !== sessionId) seen.current = { session: sessionId, map: null };
    if (!loaded) return;
    const moved = talksMoved(seen.current.map, talks);
    seen.current.map = new Map(talks.map((talk) => [talk.id, { replies: talk.replies, held: talk.held }]));
    if (moved && enabled) openRef.current();
  }, [sessionId, talks, loaded, enabled]);
}

// ── the approval of a message that waits ──────────────────────────────────────────────────────

async function decide(talk: AgentTalk, message: AgentTalkMessage, send: boolean, text?: string) {
  await api.post(`${talkPath(talk.id)}/messages/${message.id}/${send ? "send" : "refuse"}`, text !== undefined ? { text } : undefined);
  invalidate(listPath(talk.session_id));
  invalidate(talkPath(talk.id));
}

/** One held message as an approval, drawn like the composer's own permission request: who it goes to, why it
 *  waits, the whole text, and Send, Edit and Don't send. Edit opens the text in place; what is sent then is
 *  the operator's words, which the host puts in the conversation in place of the agent's. */
export function TalkApproval({ talk, message, phone, toast }: { talk: AgentTalk; message: AgentTalkMessage; phone?: boolean; toast: (text: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(message.text);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!editing) setText(message.text);
  }, [message.text, editing]);
  const title = t("talk.approve.title", { host: talk.host });
  const why = t(`talk.held.${message.note || "first"}`);
  const changed = editing && text.trim() !== message.text.trim();
  async function act(send: boolean) {
    if (busy || (send && editing && !text.trim())) return;
    setBusy(true);
    try {
      await decide(talk, message, send, send && changed ? text : undefined);
      setEditing(false);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const body = editing
    ? <textarea className="field talk-edit" data-talk-edit="" aria-label={t("talk.edit")} rows={Math.min(10, Math.max(3, text.split("\n").length + 1))} value={text} autoFocus onChange={(e) => setText(e.target.value)} />
    : <div className={`${phone ? "ph-tray-detail" : "dock-detail"} talk-approve-text`}>{message.text}</div>;
  const sendLabel = t(changed ? "talk.send.edited" : "talk.send");
  if (phone) {
    return (
      <div className="dock approval ph-tray talk-approval" role="group" aria-label={title} data-talk-held={message.id}>
        <div className="ph-tray-head warn"><Icon name="link" size={14} /><span>{t("talk.approve.head")}</span></div>
        <div className="ph-tray-title">{title}</div>
        <div className="sub talk-approve-why">{why}</div>
        {body}
        <div className="ph-tray-actions">
          <button type="button" className="ph-btn primary" data-action="send" disabled={busy} onClick={() => void act(true)}>{sendLabel}</button>
          <button type="button" className="ph-btn" data-action="edit" disabled={busy} onClick={() => setEditing((on) => !on)}>{t(editing ? "talk.edit.undo" : "talk.edit")}</button>
          <button type="button" className="ph-btn ph-deny" data-action="refuse" disabled={busy} onClick={() => void act(false)}>{t("talk.refuse")}</button>
        </div>
      </div>
    );
  }
  return (
    <div className="dock approval talk-approval" role="group" aria-label={title} data-talk-held={message.id}>
      <div className="dock-title">
        <Icon name="link" size={16} />
        <span className="grow truncate">{title}</span>
      </div>
      <div className="sub talk-approve-why">{why}</div>
      {body}
      <div className="dock-actions">
        <button type="button" className="btn small primary" data-action="send" disabled={busy} onClick={() => void act(true)}>{sendLabel}</button>
        <button type="button" className="btn small" data-action="edit" disabled={busy} onClick={() => setEditing((on) => !on)}>{t(editing ? "talk.edit.undo" : "talk.edit")}</button>
        <button type="button" className="btn small" data-action="refuse" disabled={busy} onClick={() => void act(false)}>{t("talk.refuse")}</button>
      </div>
    </div>
  );
}

function HeldOf({ talk, phone, toast }: { talk: AgentTalk; phone?: boolean; toast: (text: string) => void }) {
  const { data } = useQuery<AgentTalkDetail>(talkPath(talk.id), { pollMs: POLL_MS });
  const held = data?.messages.filter((message) => message.state === "held") ?? [];
  return <>{held.map((message) => <TalkApproval key={message.id} talk={talk} message={message} phone={phone} toast={toast} />)}</>;
}

/** Over the composer, where the strips were: every message of this chat's conversations that waits to go out. */
export function TalkApprovals({ talks, phone, toast }: { talks: readonly AgentTalk[]; phone?: boolean; toast: (text: string) => void }) {
  const waiting = talks.filter((talk) => talk.state !== "closed" && talk.held > 0);
  if (!waiting.length) return null;
  return (
    <div className="talk-approvals" data-talk-approvals="">
      {waiting.map((talk) => <HeldOf key={talk.id} talk={talk} phone={phone} toast={toast} />)}
    </div>
  );
}

// ── the tab ───────────────────────────────────────────────────────────────────────────────────

export function AgentsTab({ talks, loaded, toast }: { talks: readonly AgentTalk[]; loaded: boolean; toast: (text: string) => void }) {
  if (!loaded) return <div className="talks"><div className="sub talks-loading">{t("common.loading")}</div></div>;
  if (!talks.length) {
    return (
      <div className="talks">
        <div className="subagents-empty">
          <span className="subagents-empty-mark" aria-hidden="true"><Icon name="bots" size={20} /></span>
          <p>{t("talk.none")}</p>
        </div>
      </div>
    );
  }
  const live = talks.filter((talk) => talk.state !== "closed");
  const over = talks.filter((talk) => talk.state === "closed");
  return (
    <div className="talks" data-agents-tab="">
      {live.length > 0 && (
        <section className="subagents-group" data-group="live">
          <div className="dt-label"><span>{t("talk.group.live")}</span><span className="dt-aside">{live.length}</span></div>
          {live.map((talk) => <TalkCard key={talk.id} talk={talk} toast={toast} />)}
        </section>
      )}
      {over.length > 0 && (
        <details className="talks-over" data-group="closed" open={live.length === 0}>
          <summary className="dt-label"><span>{t("talk.group.closed")}</span><span className="dt-aside">{over.length}</span></summary>
          {over.map((talk) => <TalkCard key={talk.id} talk={talk} toast={toast} />)}
        </details>
      )}
    </div>
  );
}

const PHASE: Record<string, string> = { held: "question", unread: "question", open: "done", waiting: "working", quiet: "standby", owner: "standby", closed: "stopped" };

/** One conversation: who and how it stands, what it cost, what their door says of itself, the thread, a
 *  message that waits, and the settings. The rail and the chip borrow the Subagents tab's colours. */
function TalkCard({ talk, toast }: { talk: AgentTalk; toast: (text: string) => void }) {
  const path = talkPath(talk.id);
  const live = talk.state !== "closed";
  const { data } = useQuery<AgentTalkDetail>(path, { pollMs: live ? POLL_MS : 0 });
  const [busy, setBusy] = useState(false);
  const state = talkState(talk);
  const phase = PHASE[state.key] ?? "standby";

  async function act(work: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    try {
      await work();
      invalidate(listPath(talk.session_id));
      invalidate(path);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const end = async () => {
    if (await confirmAsync(t("talk.end.ask", { host: talk.host }), { action: t("talk.end"), danger: true })) void act(() => api.post(`${path}/end`));
  };
  const messages = data?.messages ?? [];
  const held = messages.filter((message) => message.state === "held");
  return (
    <article className="subagent-card talk-card" data-talk={talk.id} data-phase={phase} data-talk-state={state.key}>
      <div className="talk-card-head">
        <Icon name="link" size={14} />
        <span className="subagent-name truncate">{talk.host}</span>
        <span className="subagent-chip" data-phase={phase}>{talk.state === "closed" ? t(`talk.reason.${talk.reason || "ended"}`) : t(`talk.state.${state.key}`)}</span>
      </div>
      <div className="talk-counts sub" title={t("talk.spend.note")}>{talk.mode ? `${t(`talk.mode.${talk.mode}`)} · ` : ""}{talkCounts(talk)}</div>
      {talk.topic && <blockquote className="talk-topic"><span className="sub">{t("talk.topic")}</span><div className="talk-text">{talk.topic}</div></blockquote>}
      <div className="talk-messages" data-talk-messages="">
        {messages.filter((message) => message.state !== "held").map((message) => <TalkLine key={message.id} message={message} />)}
        {data && !messages.length && <div className="sub">{t("talk.empty")}</div>}
      </div>
      {/* The newest line of the thread, where it would stand once sent. */}
      {held.map((message) => <TalkApproval key={message.id} talk={talk} message={message} toast={toast} />)}
      {live && <TalkControls talk={talk} busy={busy} onChange={(body) => act(() => api.put(`${path}/settings`, body))} onEnd={() => void end()} />}
    </article>
  );
}

function TalkLine({ message }: { message: AgentTalkMessage }) {
  const word = message.state === "sent" || message.state === "read" ? "" : t(`talk.message.${message.state}`);
  return (
    <div className="talk-msg" data-role={message.role} data-state={message.state}>
      <div className="talk-msg-head"><b>{t(`talk.role.${message.role}`)}</b><span className="sub">{absTime(message.at)}</span>{word && <span className="talk-msg-word">{word}</span>}</div>
      <div className="talk-text">{message.text}</div>
    </div>
  );
}

/** A number with "No limit" beside it. The switch is the choice, not a magic number to guess: off, the field
 *  holds the limit; on, the field is put away and `none` is what is sent. */
function LimitField({ id, title, value, none, fallback, unit, whole, disabled, onCommit }: {
  id: string; title: string; value: number; none: number; fallback: number; unit?: string; whole?: boolean; disabled?: boolean; onCommit: (next: number) => void;
}) {
  const free = value === none;
  const [text, setText] = useState(String(free ? fallback : value));
  useEffect(() => {
    if (!free) setText(String(value));
  }, [value, free]);
  const parsed = Number(text);
  const valid = text.trim() !== "" && Number.isFinite(parsed) && parsed > 0 && (!whole || Number.isInteger(parsed));
  const commit = () => {
    if (valid && parsed !== value) onCommit(parsed);
  };
  return (
    <div className="talk-limit" data-talk-limit={id}>
      <label className="talk-limit-title" htmlFor={`talk-${id}`}>{title}</label>
      {!free && (
        <span className="settings-num">
          <input id={`talk-${id}`} className="field" inputMode={whole ? "numeric" : "decimal"} value={text} aria-invalid={!valid} disabled={disabled}
            onChange={(e) => setText(e.target.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === "Enter") commit(); }} />
          {unit && <span className="settings-unit">{unit}</span>}
        </span>
      )}
      <span className="talk-free">
        <span>{t("talk.nolimit")}</span>
        <Switch checked={free} label={`${title}: ${t("talk.nolimit")}`} disabled={disabled} onChange={(on) => onCommit(on ? none : valid ? parsed : fallback)} />
      </span>
    </div>
  );
}

function TalkControls({ talk, busy, onChange, onEnd }: { talk: AgentTalk; busy: boolean; onChange: (body: Record<string, unknown>) => void; onEnd: () => void }) {
  const waking = talk.wake_turns !== 0;
  // Waking switched off keeps what the operator chose for it here, so switching it on again starts from that.
  const [plan, setPlan] = useState({ turns: talk.wake_turns || WAKE_TURNS, usd: talk.wake_turns ? talk.wake_usd : WAKE_USD });
  useEffect(() => {
    if (waking) setPlan({ turns: talk.wake_turns, usd: talk.wake_usd });
  }, [waking, talk.wake_turns, talk.wake_usd]);
  const wake = (next: { turns: number; usd: number }, on = waking) => {
    setPlan(next);
    if (on) onChange({ wake_turns: next.turns, wake_usd: next.usd });
  };
  const none = t("talk.nolimit.lower");
  return (
    <div className="talk-controls">
      <div className="talk-control">
        <span className="talk-control-title">{t("talk.review")}</span>
        <Segmented<AgentTalkReview> value={talk.review} label={t("talk.review")} onChange={(review) => onChange({ review })} options={[
          { id: "first", label: t("talk.review.first") }, { id: "all", label: t("talk.review.all") }, { id: "off", label: t("talk.review.off") },
        ]} />
        <span className="sub">{t(`talk.review.${talk.review}.hint`)}</span>
      </div>
      <LimitField id="sends" title={t("talk.sends")} value={talk.sends_limit} none={0} fallback={SENDS} whole disabled={busy} onCommit={(next) => onChange({ sends_limit: next })} />
      <div className="talk-control">
        <span className="talk-toggle"><span className="talk-control-title">{t("talk.wake")}</span><Switch checked={waking} label={t("talk.wake")} disabled={busy} onChange={(on) => onChange(on ? { wake_turns: plan.turns, wake_usd: plan.usd } : { wake_turns: 0, wake_usd: 0 })} /></span>
        <span className="sub">{waking ? t("talk.wake.on", { turns: talk.wake_turns < 0 ? none : talk.wake_turns, usd: talk.wake_usd < 0 ? none : usd(talk.wake_usd) }) : t("talk.wake.off")}</span>
        <LimitField id="wake-turns" title={t("talk.wake.turns")} value={plan.turns} none={NO_LIMIT} fallback={WAKE_TURNS} whole disabled={busy} onCommit={(turns) => wake({ ...plan, turns })} />
        <LimitField id="wake-usd" title={t("talk.wake.usd")} value={plan.usd} none={NO_LIMIT} fallback={WAKE_USD} unit="$" disabled={busy} onCommit={(dollars) => wake({ ...plan, usd: dollars })} />
      </div>
      <button type="button" className="btn danger small talk-end" disabled={busy} onClick={onEnd} data-talk-end="">{t("talk.end")}</button>
    </div>
  );
}
