// The list of schedules, grouped: what waits for the operator, what runs, what is paused, what is done.
// A row says what the operator asks of a schedule at a glance — what it is, when it runs in their own
// clock, where it belongs and how it last went — and nothing that needs decoding.

import { absTime, describeSchedule, relTimeLong, untilShort } from "../format";
import { t } from "../i18n";
import { Icon, type IconName } from "../icons";
import type { ScheduleActions } from "./actions";
import { conversationOf, GROUPS, groupItems, type Item, lastRun, nameOf } from "./model";

const kindIcon = (kind: string): IconName => (kind === "message" ? "bell" : kind === "lazy" ? "bulb" : kind === "wake" ? "clock" : "loop");

export function ScheduleList({ items, selected, onSelect, titles, actions }: {
  items: Item[];
  selected: string | null;
  onSelect: (key: string) => void;
  titles: Record<string, string>;
  actions: ScheduleActions;
}) {
  const groups = groupItems(items);
  return (
    <div className="schedlist" role="list">
      {GROUPS.map((group) => groups[group].length === 0 ? null : (
        <section key={group} className={`schedlist-group ${group}`} aria-label={t(`sched.group.${group}`)}>
          <div className="schedlist-title">{t(`sched.group.${group}`)} <span className="n">{groups[group].length}</span></div>
          {groups[group].map((item) => <Row key={item.key} item={item} on={item.key === selected} onSelect={onSelect} titles={titles} actions={actions} />)}
        </section>
      ))}
    </div>
  );
}

function Row({ item, on, onSelect, titles, actions }: { item: Item; on: boolean; onSelect: (key: string) => void; titles: Record<string, string>; actions: ScheduleActions }) {
  const s = item.source === "schedule" ? item.schedule : null;
  const p = item.source === "proposal" ? item.proposal : null;
  const rule = s ?? p!;
  const next = s ? (s.enabled ? s.next_run_at : null) : p!.next_run_at;
  const chat = conversationOf(item);
  const chatTitle = chat ? titles[chat] : undefined;
  const last = s ? lastRun(s) : null;
  const open = () => onSelect(item.key);
  return (
    <div
      role="listitem"
      className={`schedrow ${on ? "on" : ""} ${item.group}`}
      data-schedule={item.key}
    >
      {/* The row is one button for the whole of its face, and the proposal's decisions sit beside it
          rather than inside it: a button nested in a button is neither valid nor reachable by keyboard. */}
      <button type="button" className="schedrow-open" aria-current={on ? "true" : undefined} onClick={open}>
        <span className={`schedrow-icon ${s && !s.enabled ? "muted" : ""}`} aria-hidden><Icon name={kindIcon(rule.kind)} size={16} /></span>
        <span className="schedrow-main">
          <span className="schedrow-head">
            <span className="schedrow-name">{nameOf(item)}</span>
            {next ? <span className="schedrow-next num" title={absTime(next)}>{untilShort(next)}</span>
              : s && !s.enabled && s.cron ? <span className="schedrow-next paused">{t("sched.paused.word")}</span> : null}
          </span>
          <span className="schedrow-when">{describeSchedule(rule)}</span>
          <span className="schedrow-meta">
            <span>{t(`sched.kind.${rule.kind}`)}</span>
            {chatTitle && <span className="schedrow-chat" title={chatTitle}><Icon name="ask" size={12} />{chatTitle}</span>}
            {last && (
              <span className={`schedrow-last ${last.status}`} title={last.at ? absTime(last.at) : undefined}>
                <span className="dot" aria-hidden />
                {last.status === "running" ? t("sched.running") : t(last.status === "failed" ? "sched.last.failed" : "sched.last.ok", { t: relTimeLong(last.at) })}
              </span>
            )}
            {p && <span className="schedrow-ask">{t("sched.proposal.from.agent")}</span>}
          </span>
        </span>
      </button>
      {p && (
        <div className="schedrow-actions">
          <button type="button" className="btn small" disabled={!!actions.busy} onClick={() => void actions.decide(p, "withdraw")}>{t("sched.proposal.withdraw")}</button>
          <button type="button" className="btn small primary" disabled={!!actions.busy || p.legacy_file_review_required} onClick={() => void actions.decide(p, "accept")}>{t("sched.proposal.accept")}</button>
        </div>
      )}
      {s && s.authority_state === "needs_approval" && item.group === "approval" && (
        <div className="schedrow-actions">
          <button type="button" className="btn small primary" disabled={!!actions.busy} onClick={() => void actions.approve(s)}>{t("sched.approval.action")}</button>
        </div>
      )}
    </div>
  );
}
