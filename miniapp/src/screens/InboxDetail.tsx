// The open entry of the Inbox: what it is, what it says, what each of its buttons will do (where the
// host says so), the buttons, a line to answer in the operator's own words, and the way to the chat it
// came from. A change proposal and a schedule proposal wait for the operator as a request does, so
// they are drawn here the same way, with their own buttons.

import { useId, useState } from "react";
import { api, type Notification, type Proposal } from "../api";
import { absTime } from "../format";
import { Icon, type IconName } from "../icons";
import { t } from "../i18n";
import { act, afterChange, entryPath, resolutionLabel, whereLine } from "../notifications";
import { navigate, pathFor } from "../router";
import { ScheduleCard, type ScheduleCardView } from "../schedulecard";
import { errorText } from "../ui";
import { Button, IconButton, Tooltip } from "../ui/primitives";
import { NoticeLoginCard } from "../browser/login";
import { waiting, type Tint } from "./inboxdata";

/** One row of the list and the entry behind it: a notification, a change proposal or a schedule proposal. */
export type InboxItem = {
  key: string;
  entry?: Notification;
  proposal?: Proposal;
  schedule?: ScheduleCardView;
  icon: IconName;
  tint: Tint;
  kind: string;
  title: string;
  preview: string;
  where: string;
  at: string;
  unread: boolean;
};

type Props = {
  item: InboxItem;
  names: Map<string, string>;
  toast: (text: string) => void;
  onOpenChat: (id: string) => void;
  onToggleRead: (entry: Notification) => void;
  onDelete: (entry: Notification) => void;
  onDecided: () => void;
};

/** A ring with a dot in it: the sign of the unread mark. The icon set has no glyph for it, and the dot it has is too small to find. */
const UNREAD_GLYPH = "M3 12a9 9 0 1 0 18 0 9 9 0 1 0-18 0M9.5 12a2.5 2.5 0 1 0 5 0 2.5 2.5 0 1 0-5 0";

const BUTTON: Record<string, "primary" | "default" | "danger" | "quiet"> = { primary: "primary", default: "default", danger: "danger", ghost: "quiet" };

export function InboxDetail({ item, names, toast, onOpenChat, onToggleRead, onDelete, onDecided }: Props) {
  const { entry, proposal, schedule } = item;
  const body = entry?.body ?? proposal?.summary ?? "";
  const asRequest = entry?.category === "permission";
  const paragraphs = body.split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
  return (
    <div className="inbox-pane" data-item={item.key}>
      <div className="inbox-pane-head">
        <span className={`inbox-tint ${item.tint}`}><Icon name={item.icon} size={16} /></span>
        <span className={`inbox-kind ${item.tint}`}>{item.kind}</span>
        <span className="inbox-when">· {absTime(item.at)}{entry && entry.count > 1 ? ` · ×${entry.count}` : ""}</span>
        <span className="grow" />
        {entry && (
          <>
            <Tooltip label={entry.seen ? t("inbox.markunread") : t("ph.inbox.markread")}>
              <button type="button" className="iconbtn" aria-label={entry.seen ? t("inbox.markunread") : t("ph.inbox.markread")} onClick={() => onToggleRead(entry)}><Icon name="dot" path={UNREAD_GLYPH} /></button>
            </Tooltip>
            {/* A request that waits keeps its row: deleting it would leave the run asking for an answer nobody can give. */}
            <IconButton icon="trash" label={t("inbox.delete")} disabled={waiting(entry)} onClick={() => onDelete(entry)} />
          </>
        )}
      </div>
      <h2 className="inbox-pane-title">{item.title}</h2>

      {schedule ? (
        <section className="inbox-schedules" aria-label={t("inbox.schedules")}>
          <ScheduleCard key={schedule.proposal!.id} itemId={schedule.proposal!.id} initial={schedule} onDecided={onDecided} />
        </section>
      ) : asRequest && body ? (
        <div className="inbox-block">
          <div className="inbox-label">{t("inbox.request")}</div>
          <pre className="inbox-code">{body}</pre>
        </div>
      ) : (
        paragraphs.length > 0 && <div className="inbox-prose">{paragraphs.map((p, i) => <div key={i}>{p}</div>)}</div>
      )}

      {proposal && <ProposalFacts p={proposal} />}
      {entry && <NoticeLoginCard entry={entry} toast={toast} />}
      {entry && <EntryAnswers key={entry.id} entry={entry} toast={toast} />}
      {proposal && <ProposalAnswers key={proposal.id} p={proposal} toast={toast} onDone={onDecided} />}

      {entry && (entry.link || entry.session_id) && (
        <div className="inbox-foot">
          <button type="button" className="inbox-open" onClick={() => (entry.session_id && !entry.link ? onOpenChat(entry.session_id) : navigate(entryPath(entry)))}>
            <Icon name={entry.session_id ? "chat" : "forward"} size={16} />
            <span className="grow">{t(entry.session_id ? "inbox.open.chat" : "notice.open")}</span>
            <Icon name="forward" size={14} />
          </button>
          <div className="inbox-activity">{whereLine(entry, names)}{entry.resolved ? ` · ${resolutionLabel(entry.resolved)}` : ""}</div>
        </div>
      )}
    </div>
  );
}

/** What the host knows of a change proposal, as the rows of a table. */
function ProposalFacts({ p }: { p: Proposal }) {
  const rows: [string, string, boolean?][] = [[t("inbox.branch"), p.branch, true], [t("inbox.repo"), p.repo], ...(p.pr_number ? [[t("inbox.pr"), `#${p.pr_number}`] as [string, string]] : [])];
  return (
    <div className="inbox-kv">
      {rows.map(([k, v, mono]) => (
        <div key={k} className="inbox-kv-row"><span className="inbox-kv-key">{k}</span><span className={mono ? "mono" : ""}>{v}</span></div>
      ))}
    </div>
  );
}

/**
 * A request's table of outcomes, its buttons and, for a question, a line to answer in one's own words.
 * An answered request gives way to what the answer was, wherever it was given. In the view of what
 * waits it then leaves the list, and the next one opens in its place.
 */
function EntryAnswers({ entry, toast }: { entry: Notification; toast: (text: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<string | null>(null);
  const resolved = outcome ?? entry.resolved;
  if (resolved) {
    return <div className="inbox-outcome" data-outcome={resolved}><Icon name="check" size={16} /><span className={`notice-outcome ${resolved}`}>{resolutionLabel(resolved)}</span></div>;
  }
  if (!entry.needs_you) return null;
  const buttons = entry.actions.filter((a) => a.id !== "open");
  const described = buttons.filter((a) => a.description);
  // Only a question takes a free answer: the host refuses one for a permission.
  const writes = !!entry.request_ref && entry.request_ref.startsWith("ask:");

  async function take(action: string, value?: string) {
    setBusy(true);
    try {
      const result = await act(entry.id, action, value);
      if (result.resolution) setOutcome(result.resolution);
      if (result.conflict) toast(t("notice.conflict", { outcome: resolutionLabel(result.resolution ?? "answered") }));
      afterChange();
    } catch (e) {
      toast(t("notice.failed", { error: errorText(e) }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {described.length > 0 && (
        <div className="inbox-block">
          <div className="inbox-label">{t("inbox.outcomes")}</div>
          <div className="inbox-outcomes">
            {described.map((a) => (
              <div key={a.id} className="inbox-outcomes-row"><span className="inbox-outcomes-name">{a.label}</span><span className="inbox-outcomes-text">{a.description}</span></div>
            ))}
          </div>
        </div>
      )}
      {buttons.length > 0 && (
        <div className="inbox-actions">
          {buttons.map((a) => (
            <Button key={a.id} variant={BUTTON[a.style] ?? "default"} data-action={a.id} disabled={busy} onClick={() => void take(a.id)}>{a.label || a.id}</Button>
          ))}
        </div>
      )}
      {writes && <ReplyLine label={t("inbox.reply")} placeholder={t("inbox.reply.placeholder")} send={t("inbox.reply.send")} busy={busy} onSend={(text) => take("answer", text)} />}
    </>
  );
}

/** A change proposal's three answers; rejecting sends the reason, which the agent reads. */
function ProposalAnswers({ p, toast, onDone }: { p: Proposal; toast: (text: string) => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  async function decide(decision: "approve" | "reject", reason = "") {
    setBusy(true);
    try {
      await api.post(`/api/proposals/${p.id}/decide`, { decision, reason });
      onDone();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <div className="inbox-actions">
        <Button variant="primary" data-action="approve" disabled={busy} onClick={() => void decide("approve")}>{t("inbox.approve")}</Button>
        <Button data-action="reject" disabled={busy} onClick={() => void decide("reject")}>{t("inbox.reject.do")}</Button>
        <Button variant="quiet" data-action="diff" onClick={() => navigate(pathFor("changes", p.id))}>{t("inbox.diff")}</Button>
      </div>
      <ReplyLine label={t("inbox.reason")} placeholder={t("inbox.reason.placeholder")} send={t("inbox.reject.do")} busy={busy} onSend={(text) => decide("reject", text)} />
    </>
  );
}

/** One line to answer in one's own words, sent with Enter or the arrow. */
function ReplyLine({ label, placeholder, send, busy, onSend }: { label: string; placeholder: string; send: string; busy: boolean; onSend: (text: string) => Promise<void> | void }) {
  const [text, setText] = useState("");
  const id = useId();
  const submit = () => {
    const value = text.trim();
    if (!value || busy) return;
    void Promise.resolve(onSend(value)).then(() => setText(""));
  };
  return (
    <div className="inbox-block">
      <label className="inbox-reply-label" htmlFor={id}>{label}</label>
      <div className="inbox-reply">
        <input id={id} className="inbox-reply-input" type="text" value={text} placeholder={placeholder} disabled={busy} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.nativeEvent.isComposing) { e.preventDefault(); submit(); } }} />
        <IconButton icon="up" label={send} className="inbox-send" disabled={busy || !text.trim()} onClick={submit} />
      </div>
    </div>
  );
}
