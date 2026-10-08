// A staff member's session seen from inside the project: a header that says who this is, what runs
// it, where it works and on what, with the three controls the team runtime offers; and the messages
// sent to it with how far each one got. A Daedalus member's session shows the last of them above its
// composer; the command-line staff view lists them in its Session tab instead, beside the terminal
// rather than under it, and reuses the header and the rows.

import type { ReactNode } from "react";
import { api, type StaffMessage } from "../api";
import { clock } from "../format";
import { t } from "../i18n";
import { applyMessageEvent, stripRows } from "../staff/model";
import { invalidate, peek, prime, useQuery } from "../store";
import { HarnessBadge, StaffAvatar } from "../team/parts";
import { Icon } from "../icons";
import { HARNESS_NAMES, type Staff } from "../team/team";
import { confirmAsync, errorText } from "../ui";
import { useFocus } from "./data";
import { firstWait, staffTone, waitKey } from "./focus";
import { useEvent } from "../events";

const memberKey = (id: string) => `/api/staff/${encodeURIComponent(id)}`;
export const messagesKey = (id: string, n = 3) => `/api/staff/${encodeURIComponent(id)}/messages?limit=${n}`;

/** One member, kept current by its events; `null` asks nothing (a session that is nobody's work). */
export function useMember(staffId: string | null) {
  const member = useQuery<Staff>(staffId ? memberKey(staffId) : null, { pollMs: 15000, staleMs: 3000 });
  useEvent(["staff."], (event) => {
    if (staffId && event.staff_id === staffId) invalidate(memberKey(staffId));
  }, [staffId]);
  return member;
}

/**
 * `facts` goes after the status in the pill ("turn 4 · 18 min"); `details` replaces the line under the
 * name with what the caller knows better (a command-line member's version, worktree and task).
 */
export function StaffHeader({ projectId, staffId, toast, facts, details, children, compact = false }: { projectId: string; staffId: string; toast: (text: string) => void; facts?: string; details?: ReactNode; children?: ReactNode; compact?: boolean }) {
  const { data: member } = useMember(staffId);
  const { board } = useFocus(projectId);
  if (!member) return null;
  const live = member.live;
  const tone = staffTone(member);
  const task = live?.task_id ? board?.tasks.find((x) => x.id === live.task_id) : undefined;
  const wait = firstWait(member);
  const runs = [HARNESS_NAMES[member.harness], member.model, member.permission_mode].filter(Boolean).join(" · ");
  async function act(what: "interrupt" | "pause" | "release") {
    if (!member) return;
    if (what === "release" && !(await confirmAsync(t("focus.staff.release.title", { name: member.name }), { body: t("focus.staff.release.body"), action: t("focus.staff.release") }))) return;
    try {
      await api.post(`/api/staff/${encodeURIComponent(member.id)}/${what}`, what === "release" ? { keep_worktree: true } : undefined);
      toast(t(what === "interrupt" ? "focus.staff.interrupted" : what === "pause" ? "focus.staff.pausing" : "focus.staff.released", { name: member.name }));
      invalidate(memberKey(member.id));
      invalidate(`/api/projects/${encodeURIComponent(projectId)}/staff`);
    } catch (e) {
      toast(errorText(e));
    }
  }
  const state = wait ? t("focus.wait.line", { reason: t(waitKey(wait.reason)), n: wait.position }) : live?.pause_requested ? t("focus.staff.paused") : t(`team.status.${member.status}`);
  // A phone's member page names the member in its bar, so the header is only the state and the two
  // controls a turn needs; Release, the weightiest, is in the details sheet beside the rest.
  if (compact) {
    return (
      <div className="staff-head compact">
        <div className="staff-head-row">
          <span className={`focus-dot tone-${tone}`} aria-hidden />
          <span className="staff-head-state truncate" data-status={member.status} title={wait?.detail || live?.waiting_for || undefined}>{state}{facts && !wait && ` · ${facts}`}</span>
          {live && (
            <span className="staff-head-actions">
              <button type="button" className="ph-btn sm" onClick={() => void act("pause")} disabled={live.pause_requested}><Icon name="pause" size={16} />{t("ph.member.pause")}</button>
              <button type="button" className="ph-btn sm" onClick={() => void act("interrupt")}><Icon name="stop" size={16} />{t("focus.staff.interrupt")}</button>
            </span>
          )}
        </div>
        {children}
      </div>
    );
  }
  return (
    <div className="staff-head">
      <div className="staff-head-row">
        <StaffAvatar name={member.name} color={member.color} />
        <div className="staff-head-who">
          <div className="staff-head-name truncate">{member.name}{member.role && <span className="staff-head-role"> · {member.role}</span>}</div>
          {details ?? (
            <div className="staff-head-meta truncate">
              <HarnessBadge harness={member.harness} />
              <span>{runs}</span>
              {live?.branch && <span className="mono">{t("focus.staff.branch", { branch: live.branch })}</span>}
              {task && <span>{t("focus.staff.task", { title: task.title })}</span>}
            </div>
          )}
        </div>
        <span className={`focus-pill tone-${tone}`} title={wait?.detail || live?.waiting_for || undefined} data-status={member.status}>
          <span className={`focus-dot tone-${tone}`} aria-hidden />
          {state}
          {facts && !wait && <span className="staff-head-facts"> · {facts}</span>}
        </span>
        {live && (
          <div className="staff-head-actions">
            <button className="btn small" onClick={() => void act("interrupt")}>{t("focus.staff.interrupt")}</button>
            <button className="btn small" onClick={() => void act("pause")} disabled={live.pause_requested}>{t("focus.staff.pause")}</button>
            {/* Outlined in the warning colour: as a ghost it was quiet enough to read as a caption, not as
                the third of three actions, and releasing the member is the weightiest of them. */}
            <button className="btn small warn" onClick={() => void act("release")}>{t("focus.staff.release")}</button>
          </div>
        )}
      </div>
      {children}
    </div>
  );
}

/**
 * The messages sent to a member, as the list route gives them (newest first), kept current: a receipt
 * moves the moment its `staff.message` event arrives, and the list is read again after, for anything
 * the event does not carry.
 */
export function useStaffMessages(staffId: string, n: number): StaffMessage[] {
  const key = messagesKey(staffId, n);
  const { data } = useQuery<StaffMessage[]>(key, { pollMs: 15000, staleMs: 3000 });
  useEvent(["staff.message"], (event) => {
    if (event.staff_id !== staffId) return;
    const held = peek<StaffMessage[]>(key);
    if (Array.isArray(held)) prime(key, applyMessageEvent(held, event.payload ?? {}));
    invalidate(`/api/staff/${encodeURIComponent(staffId)}/messages`);
  }, [staffId, key]);
  return Array.isArray(data) ? data : [];
}

/** A failed message sent again, as a new message: the failed one keeps its receipt. */
async function retry(staffId: string, m: StaffMessage, toast?: (text: string) => void) {
  try {
    await api.post(`/api/staff/${encodeURIComponent(staffId)}/messages`, { text: m.text, when: m.mode });
    invalidate(`/api/staff/${encodeURIComponent(staffId)}/messages`);
  } catch (e) {
    toast?.(errorText(e));
  }
}

/**
 * One message with its receipt. Retry is the operator's own failed message only: a failed message of
 * the orchestrator's already wakes the orchestrator, and a second copy from here would reach the
 * member twice.
 */
export function MessageRow({ staffId, name, message: m, toast }: { staffId: string; name: string; message: StaffMessage; toast?: (text: string) => void }) {
  return (
    <div className="staff-message" data-message={m.id} data-state={m.state}>
      <span className="staff-message-from">{t(`focus.msg.from.${m.origin}`, { name })} · {clock(m.created_at)}</span>
      <span className={`pill msg-state ${m.state}`} title={m.error || undefined}>{t(`focus.msg.state.${m.state}`)}</span>
      <span className="staff-message-text truncate">{t("focus.msg.quote", { text: m.text })}</span>
      {m.state === "failed" && m.origin === "operator" && <button className="btn small ghost staff-message-retry" onClick={() => void retry(staffId, m, toast)}>{t("delivery.retry")}</button>}
    </div>
  );
}

/** The last messages sent to a Daedalus member, above its session's composer, newest last. */
export function StaffMessages({ staffId, n = 2, toast }: { staffId: string; n?: number; toast?: (text: string) => void }) {
  const { data: member } = useMember(staffId);
  const rows = stripRows(useStaffMessages(staffId, Math.max(3, n)), n);
  if (!member || rows.length === 0) return null;
  return (
    <div className="staff-messages" aria-label={t("focus.staff.messages", { name: member.name })}>
      {rows.map((m) => <MessageRow key={m.id} staffId={staffId} name={member.name} message={m} toast={toast} />)}
    </div>
  );
}
