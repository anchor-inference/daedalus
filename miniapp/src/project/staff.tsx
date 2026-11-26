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
import { Button } from "../ui/primitives";
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

/** Interrupt, pause after the turn, and release: the three controls of a member's turn. */
function useStaffAct(projectId: string, toast: (text: string) => void) {
  return async function act(member: Staff, what: "interrupt" | "pause" | "release") {
    if (what === "release" && !(await confirmAsync(t("focus.staff.release.title", { name: member.name }), { body: t("focus.staff.release.body"), action: t("focus.staff.release") }))) return;
    try {
      await api.post(`/api/staff/${encodeURIComponent(member.id)}/${what}`, what === "release" ? { keep_worktree: true } : undefined);
      invalidate(memberKey(member.id));
      invalidate(`/api/projects/${encodeURIComponent(projectId)}/staff`);
    } catch (e) {
      toast(errorText(e));
    }
  };
}

const stateWords = (member: Staff) => {
  const wait = firstWait(member);
  return { wait, state: wait ? t("focus.wait.line", { reason: t(waitKey(wait.reason)), n: wait.position }) : member.live?.pause_requested ? t("focus.staff.paused") : t(`team.status.${member.status}`) };
};

/**
 * A phone's member page names the member in its bar, so this header is only the state and the two
 * controls a turn needs; Release, the weightiest, is in the command-line member's details sheet beside
 * the rest, and only a Daedalus member's chat, which has no such sheet, asks for it here (`release`).
 * `facts` goes after the status ("turn 4 · 18 min").
 */
export function StaffHeader({ staffId, projectId, toast, facts, release = false, children }: { projectId: string; staffId: string; toast: (text: string) => void; facts?: string; release?: boolean; children?: ReactNode }) {
  const { data: member } = useMember(staffId);
  const act = useStaffAct(projectId, toast);
  if (!member) return null;
  const live = member.live;
  const { wait, state } = stateWords(member);
  return (
    <div className="staff-head compact">
      <div className="staff-head-row">
        <span className={`focus-dot tone-${staffTone(member)}`} aria-hidden />
        <span className="staff-head-state truncate" data-status={member.status} title={wait?.detail || live?.waiting_for || undefined}>{state}{facts && !wait && ` · ${facts}`}</span>
        {live && (
          <span className="staff-head-actions">
            <Button size="sm" icon="pause" onClick={() => void act(member, "pause")} disabled={live.pause_requested}>{t("ph.member.pause")}</Button>
            <Button size="sm" icon="stop" onClick={() => void act(member, "interrupt")}>{t("focus.staff.interrupt")}</Button>
            {release && <Button size="sm" variant="danger" onClick={() => void act(member, "release")}>{t("focus.staff.release")}</Button>}
          </span>
        )}
      </div>
      {children}
    </div>
  );
}

/** The member where a chat's header has its title: its face, name and role, and how its work goes. */
export function StaffWho({ staffId }: { staffId: string }) {
  const { data: member } = useMember(staffId);
  if (!member) return null;
  const { wait, state } = stateWords(member);
  const tone = staffTone(member);
  return (
    <span className="staff-who">
      <StaffAvatar name={member.name} color={member.color} />
      <span className="staff-head-name truncate">{member.name}{member.role && <span className="staff-head-role"> · {member.role}</span>}</span>
      <span className={`focus-pill tone-${tone}`} title={wait?.detail || member.live?.waiting_for || undefined} data-status={member.status}>
        <span className={`focus-dot tone-${tone}`} aria-hidden />
        <span className="staff-head-state truncate">{state}</span>
      </span>
    </span>
  );
}

/** The turn's three controls, at the head of a member's chat header while it works. */
export function StaffControls({ projectId, staffId, toast }: { projectId: string; staffId: string; toast: (text: string) => void }) {
  const { data: member } = useMember(staffId);
  const act = useStaffAct(projectId, toast);
  if (!member?.live) return null;
  return (
    // Each names itself on the button too: in a chat column narrower than the three words and the
    // header's own glyphs, the words give way and the controls are their icons.
    <span className="staff-head-actions staff-controls">
      <Button size="sm" icon="stop" aria-label={t("focus.staff.interrupt")} title={t("focus.staff.interrupt")} onClick={() => void act(member, "interrupt")}><span className="staff-control-word">{t("focus.staff.interrupt")}</span></Button>
      <Button size="sm" icon="pause" aria-label={t("focus.staff.pause")} title={t("focus.staff.pause")} onClick={() => void act(member, "pause")} disabled={member.live.pause_requested}><span className="staff-control-word">{t("focus.staff.pause")}</span></Button>
      {/* The danger colour: releasing the member is the weightiest of the three. */}
      <Button size="sm" variant="danger" icon="unlink" aria-label={t("focus.staff.release")} title={t("focus.staff.release")} onClick={() => void act(member, "release")}><span className="staff-control-word">{t("focus.staff.release")}</span></Button>
    </span>
  );
}

/** The line under a member's header: what runs it, the branch it works on, the card it works for. */
export function StaffFacts({ projectId, staffId }: { projectId: string; staffId: string }) {
  const { data: member } = useMember(staffId);
  const { board } = useFocus(projectId);
  if (!member) return null;
  const live = member.live;
  const task = live?.task_id ? board?.tasks.find((x) => x.id === live.task_id) : undefined;
  const runs = [HARNESS_NAMES[member.harness], member.model, member.permission_mode].filter(Boolean).join(" · ");
  return (
    <div className="staff-facts">
      <span className="staff-head-meta truncate">
        <HarnessBadge harness={member.harness} />
        <span>{runs}</span>
        {live?.branch && <span className="mono">{t("focus.staff.branch", { branch: live.branch })}</span>}
        {task && <span>{t("focus.staff.task", { title: task.title })}</span>}
      </span>
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
