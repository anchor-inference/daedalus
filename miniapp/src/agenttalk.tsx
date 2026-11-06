// This chat's conversations with other agents: the strip over the composer while one is live, and the sheet
// that shows all of one and holds what only the operator can set. The other side is a stranger, so everything
// it wrote, its description of itself included, is plain text in a frame that says whose it is. Nothing here
// turns it into markdown, a link or a picture.

import { useEffect, useState } from "react";
import { AgentTalk, AgentTalkDetail, AgentTalkList, AgentTalkMessage, AgentTalkReview, api } from "./api";
import { absTime, usd } from "./format";
import { Icon } from "./icons";
import { t } from "./i18n";
import { invalidate, useQuery } from "./store";
import { Segmented, Switch } from "./ui/components";
import { Sheet } from "./ui/dialogs";
import { confirmAsync, errorText } from "./ui";
import "./agenttalk.css";

const POLL_MS = 5000;
const WAKE_TURNS = 5;
const WAKE_USD = 0.5;

const encoded = encodeURIComponent;
const listPath = (sessionId: string) => `/api/sessions/${encoded(sessionId)}/agent-talks`;

export function useAgentTalks(sessionId: string | null | undefined): AgentTalk[] {
  const { data } = useQuery<AgentTalkList>(sessionId ? listPath(sessionId) : null, { pollMs: POLL_MS });
  return data?.talks ?? [];
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

function counts(talk: AgentTalk): string {
  return [t("talk.sent", { n: talk.sends, of: talk.sends_limit }), t("talk.replies", { n: talk.replies }), usd(talk.spent_usd)].join(" · ");
}

/** The strip over the composer: while a conversation is live the operator sees who it is with and can step in. */
export function AgentTalkBar({ talks, onOpen }: { talks: AgentTalk[]; onOpen: (id: string) => void }) {
  const live = talks.filter((talk) => talk.state !== "closed");
  if (!live.length) return null;
  return (
    <div className="talk-bar" data-agent-talks="">
      {live.map((talk) => {
        const state = talkState(talk);
        return (
          <button key={talk.id} type="button" className="talk-row" data-talk-state={state.key} data-tone={state.tone || undefined} onClick={() => onOpen(talk.id)}>
            <Icon name="link" size={13} />
            <span className="talk-who truncate">{talk.host}</span>
            <span className="talk-word">{t(`talk.state.${state.key}`)}</span>
            <span className="talk-counts truncate">{counts(talk)}</span>
          </button>
        );
      })}
    </div>
  );
}

export function AgentTalkSheet({ sessionId, first, toast, onClose }: { sessionId: string; first: string | null; toast: (text: string) => void; onClose: () => void }) {
  const [open, setOpen] = useState<string | null>(first);
  const talks = useAgentTalks(sessionId);
  return (
    <Sheet title={t("talk.title")} onClose={onClose} size="narrow">
      <div className="talk-sheet" data-talk-sheet="">
        {open ? <TalkDetail id={open} toast={toast} onBack={() => setOpen(null)} /> : <TalkList talks={talks} onOpen={setOpen} />}
      </div>
    </Sheet>
  );
}

function TalkList({ talks, onOpen }: { talks: AgentTalk[]; onOpen: (id: string) => void }) {
  if (!talks.length) return <div className="sub">{t("talk.none")}</div>;
  return (
    <div className="talk-list">
      {talks.map((talk) => (
        <button key={talk.id} type="button" className="talk-item" data-talk={talk.id} onClick={() => onOpen(talk.id)}>
          <span className="talk-item-head"><b className="truncate">{talk.host}</b><span className="sub">{t(`talk.state.${talkState(talk).key}`)}</span></span>
          <span className="sub">{counts(talk)}</span>
        </button>
      ))}
    </div>
  );
}

function TalkDetail({ id, toast, onBack }: { id: string; toast: (text: string) => void; onBack: () => void }) {
  const path = `/api/agent-talks/${encoded(id)}`;
  const { data, refresh } = useQuery<AgentTalkDetail>(path, { pollMs: POLL_MS });
  const [busy, setBusy] = useState(false);

  async function act(work: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    try {
      await work();
      invalidate(listPath(data!.session_id));
      await refresh();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  if (!data) return <div className="sub" role="status">{t("common.loading")}</div>;
  const live = data.state !== "closed";
  const end = async () => {
    if (await confirmAsync(t("talk.end.ask", { host: data.host }), { action: t("talk.end"), danger: true })) void act(() => api.post(`${path}/end`));
  };
  const settings = (body: Record<string, unknown>) => act(() => api.put(`${path}/settings`, body));
  const state = talkState(data);
  return (
    <div className="talk-detail">
      <button type="button" className="btn ghost small talk-back" onClick={onBack}><Icon name="back" size={14} />{t("talk.back")}</button>
      <div className="talk-who-line" data-talk-word={state.key}>
        <b className="talk-host">{data.host}</b>
        <span className="sub">{data.state === "closed" ? t(`talk.reason.${data.reason || "ended"}`) : t(`talk.state.${state.key}`)}</span>
      </div>
      <div className="sub" title={t("talk.spend.note")}>{data.mode ? `${t(`talk.mode.${data.mode}`)} · ` : ""}{counts(data)}</div>
      {data.topic && <blockquote className="talk-topic"><span className="sub">{t("talk.topic")}</span><div className="talk-text">{data.topic}</div></blockquote>}
      <div className="talk-messages" data-talk-messages="">
        {data.messages.map((message) => <TalkLine key={message.id} message={message} busy={busy} onDecide={(send) => act(() => api.post(`${path}/messages/${message.id}/${send ? "send" : "refuse"}`))} />)}
        {!data.messages.length && <div className="sub">{t("talk.empty")}</div>}
      </div>
      {live && <TalkControls talk={data} busy={busy} onChange={settings} onEnd={() => void end()} />}
    </div>
  );
}

function TalkLine({ message, busy, onDecide }: { message: AgentTalkMessage; busy: boolean; onDecide: (send: boolean) => void }) {
  const held = message.state === "held";
  const word = message.state === "sent" || message.state === "read" ? "" : t(`talk.message.${message.state}`);
  return (
    <div className="talk-msg" data-role={message.role} data-state={message.state}>
      <div className="talk-msg-head"><b>{t(`talk.role.${message.role}`)}</b><span className="sub">{absTime(message.at)}</span>{word && <span className="talk-msg-word">{word}</span>}</div>
      <div className="talk-text">{message.text}</div>
      {held && (
        <div className="talk-held">
          <span className="sub">{t(`talk.held.${message.note || "first"}`)}</span>
          <span className="talk-held-actions">
            <button type="button" className="btn primary small" disabled={busy} onClick={() => onDecide(true)}>{t("talk.send")}</button>
            <button type="button" className="btn ghost small" disabled={busy} onClick={() => onDecide(false)}>{t("talk.refuse")}</button>
          </span>
        </div>
      )}
    </div>
  );
}

function TalkControls({ talk, busy, onChange, onEnd }: { talk: AgentTalk; busy: boolean; onChange: (body: Record<string, unknown>) => void; onEnd: () => void }) {
  const waking = talk.wake_turns > 0;
  const [turns, setTurns] = useState(String(talk.wake_turns || WAKE_TURNS));
  const [dollars, setDollars] = useState(String(talk.wake_usd || WAKE_USD));
  useEffect(() => {
    if (waking) {
      setTurns(String(talk.wake_turns));
      setDollars(String(talk.wake_usd));
    }
  }, [waking, talk.wake_turns, talk.wake_usd]);
  const wake = (on: boolean) => onChange(on ? { wake_turns: Number(turns) || WAKE_TURNS, wake_usd: Number(dollars) || 0 } : { wake_turns: 0, wake_usd: 0 });
  return (
    <div className="talk-controls">
      <div className="talk-control">
        <span>{t("talk.review")}</span>
        <Segmented<AgentTalkReview> value={talk.review} label={t("talk.review")} onChange={(review) => onChange({ review })} options={[
          { id: "first", label: t("talk.review.first") }, { id: "all", label: t("talk.review.all") }, { id: "off", label: t("talk.review.off") },
        ]} />
        <span className="sub">{t(`talk.review.${talk.review}.hint`)}</span>
      </div>
      <div className="talk-control">
        <span className="talk-toggle"><span>{t("talk.wake")}</span><Switch checked={waking} label={t("talk.wake")} disabled={busy} onChange={wake} /></span>
        <span className="sub">{t(waking ? "talk.wake.on" : "talk.wake.off", { turns: talk.wake_turns, usd: usd(talk.wake_usd) })}</span>
        {!waking && (
          <span className="talk-wake-fields">
            <label>{t("talk.wake.turns")}<input className="field" inputMode="numeric" value={turns} onChange={(e) => setTurns(e.target.value)} /></label>
            <label>{t("talk.wake.usd")}<input className="field" inputMode="decimal" value={dollars} onChange={(e) => setDollars(e.target.value)} /></label>
          </span>
        )}
      </div>
      <button type="button" className="btn danger small talk-end" disabled={busy} onClick={onEnd} data-talk-end="">{t("talk.end")}</button>
    </div>
  );
}
