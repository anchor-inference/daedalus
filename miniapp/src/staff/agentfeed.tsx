// Messages between the agents of a project, drawn: an entry in a member's Feed ("From Ada", "To Bob",
// with its state, its hop and whether an answer came), and a compact row in the orchestrator's event
// card that opens to the whole message. The rules are in `agentmessages.ts`.

import { useState } from "react";
import type { AgentMessage, AgentMessagePage, AgentParty } from "../api";
import { useEvent } from "../events";
import { clock } from "../format";
import { t } from "../i18n";
import { Icon } from "../icons";
import { invalidate, useQuery } from "../store";
import { agentLineTone, agentMessagesBase, agentMessagesKey, direction, otherParty, waitState, type AgentLine } from "./agentmessages";
import "./agentfeed.css";

/**
 * A project's messages between agents, or one member's, newest first, kept current: every
 * `agent.message` of the project reads the log again, and the poll catches what no event announces
 * (a wait that ran out turns a message "unanswered" without one).
 */
export function useAgentMessages(projectId: string | null | undefined, staffId?: string | null): AgentMessage[] {
  const key = projectId ? agentMessagesKey(projectId, staffId) : null;
  const { data } = useQuery<AgentMessagePage>(key, { pollMs: 20000, staleMs: 3000 });
  useEvent(["agent.message"], (event) => {
    if (projectId && event.project_id === projectId) invalidate(agentMessagesBase(projectId));
  }, [projectId]);
  return Array.isArray(data?.messages) ? data!.messages : [];
}

function partyName(party: Pick<AgentParty, "kind" | "name">): string {
  return party.kind === "orchestrator" ? t("agentmsg.orchestrator") : party.name || t("agentmsg.someone");
}

/** The pill of a message's state, and for one that waits, where its answer stands. */
function StateBits({ message }: { message: AgentMessage }) {
  const wait = waitState(message);
  return (
    <>
      <span className={`pill agent-state ${message.state}`} data-agent-state={message.state} title={message.detail || undefined}>{t(`agentmsg.state.${message.state}`)}</span>
      {message.hop > 0 && <span className="agent-hop">{t("agentmsg.hop", { n: message.hop })}</span>}
      {wait === "waiting" && <span className="agent-wait" data-wait="waiting">{t("agentmsg.wait.waiting", { s: message.wait_s })}</span>}
    </>
  );
}

/** One message in a member's Feed, from or to another agent of the project. */
export function AgentFeedEntry({ message, staffId }: { message: AgentMessage; staffId: string }) {
  const dir = direction(message, staffId);
  const other = partyName(otherParty(message, staffId));
  const who = t(`agentmsg.${message.kind === "reply" ? "reply" : "msg"}.${dir}`, { name: other });
  const why = (message.state === "refused" || message.state === "failed") && message.detail ? message.detail : "";
  return (
    <article className={`feed-agent ${dir}`} data-agent-message={message.id} data-direction={dir} data-kind={message.kind} data-state={message.state}>
      <div className="feed-turn-head feed-agent-head">
        <Icon name={dir === "to" ? "send" : message.kind === "reply" ? "reply" : "bots"} size={13} />
        <span className="feed-turn-who feed-agent-who">{who}</span>
        <span className="feed-turn-at">{clock(message.created_at)}</span>
        <StateBits message={message} />
      </div>
      {message.text && <div className="feed-prompt feed-agent-text">{message.text}</div>}
      {why && <div className="feed-agent-why">{why}</div>}
    </article>
  );
}

/**
 * A message between agents as one row of the orchestrator's event card: who to whom, what became of
 * it and the first words, a click away from the whole message. Opened, it reads the project's log for
 * the message's present state, since the line says only what it was when the orchestrator was woken.
 */
export function AgentEventLine({ line, time, projectId, reply }: { line: AgentLine; time: string; projectId: string | null; reply?: () => void }) {
  const [open, setOpen] = useState(false);
  const logged = useQuery<AgentMessagePage>(open && projectId && line.id ? agentMessagesKey(projectId, null, 200) : null, { staleMs: 5000 });
  const found = line.kind === "message" ? logged.data?.messages?.find((m) => m.id === line.id) : undefined;
  const tone = agentLineTone(line);
  const state = line.kind === "reply" ? (line.state === "failed" ? "unanswered" : "answered") : line.state;
  const words = line.kind === "reply" && line.state === "failed" ? line.detail : line.text;
  return (
    <li className={`event-line agent ${tone} ${open ? "open" : ""} ${reply ? "answerable" : ""}`} data-agent-line={line.id || "refused"} data-kind={line.kind} data-state={state}>
      <span className="event-time num">{time}</span>
      <div className="agent-line">
        <button type="button" className="agent-line-head" aria-expanded={open} onClick={() => setOpen((o) => !o)} title={t(open ? "agentmsg.line.close" : "agentmsg.line.open")}>
          <Icon name={line.kind === "reply" ? "reply" : "bots"} size={13} />
          <span className="agent-line-who">{line.from}<span className="agent-line-arrow" aria-hidden="true"> → </span>{line.to}</span>
          <span className={`pill agent-state ${state}`}>{t(`agentmsg.state.${state}`)}</span>
          {line.hop !== null && <span className="agent-hop">{t("agentmsg.hop", { n: line.hop })}</span>}
          {line.wait && <span className="agent-wait">{t("agentmsg.line.waits")}</span>}
          <span className="agent-line-text">{words}</span>
          <span className="agent-line-chevron"><Icon name="forward" size={12} /></span>
        </button>
        {open && (
          <div className="agent-line-body">
            {(found?.text ?? words) && <div className="agent-line-full">{found?.text ?? words}</div>}
            {line.detail && line.detail !== "turn" && words !== line.detail && <div className="agent-line-why">{line.detail}</div>}
            {line.detail === "turn" && <div className="agent-line-why">{t("agentmsg.line.turnend")}</div>}
            {found && (
              <div className="agent-line-now" data-now={found.state}>
                {t("agentmsg.line.now")} <span className={`pill agent-state ${found.state}`}>{t(`agentmsg.state.${found.state}`)}</span>
                {found.delivery && <span className="agent-line-delivery">{t(`focus.msg.state.${found.delivery}`)}</span>}
              </div>
            )}
          </div>
        )}
      </div>
      {reply ? (
        <button type="button" className="iconbtn small event-reply" onClick={reply} aria-label={t("reply.action")} title={t("reply.action")}>
          <Icon name="reply" size={14} />
        </button>
      ) : null}
    </li>
  );
}
