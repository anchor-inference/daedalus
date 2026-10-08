// A project's team on a phone: borderless rows, each a member with the dot of how it goes, what it is
// on and what it spent; a long press holds every command the member has (tell it something, open its
// feed or terminal, pause, interrupt, release, edit, dismiss), so the row needs no button of its own.
// A big team is grouped by how its members are doing — problems first, then who works, who waits and
// who is off — with the long groups folded to their first rows.

import { useState, type ReactNode } from "react";
import { api } from "../api";
import { relTime } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { navigate, pathFor, projectSessionPath, projectStaffPath } from "../router";
import { invalidate, useOffline } from "../store";
import { confirmAsync, errorText } from "../ui";
import type { MenuItem } from "../ui/dialogs";
import { EmptyState, ListRow, SectionHeader } from "../ui/phone";
import { firstWait, splitTeam, staffTone, waitKey, type StaffTone } from "../project/focus";
import { spendLine, staffUsage, type ProjectUsage } from "../project/usage";
import { HarnessBadge, StaffAvatar } from "./parts";
import type { Staff, Team } from "./team";

const enc = encodeURIComponent;
/** Above this many members the list is grouped by how they are doing. */
const GROUP_AT = 8;
/** How many rows a long group shows before "Show N more". */
const FOLD_AT = 5;

/** Where a member leads on a phone: its conversation, a command-line member's own page, or nowhere
 *  (its edit sheet opens instead). */
export function memberPath(projectId: string, member: Staff): string | null {
  if (member.live?.session_id) return projectSessionPath(projectId, member.live.session_id);
  if (member.harness !== "daedalus" && member.live) return projectStaffPath(projectId, member.id);
  return null;
}

/**
 * Dismissing a member on a phone. One with a live session cannot be dismissed until it is released,
 * so the question says so and offers the release itself instead of a Dismiss the host would refuse.
 */
export async function dismissMember(member: Staff, projectId: string, toast: (text: string) => void, onDone?: () => void): Promise<void> {
  if (member.live) {
    if (!(await confirmAsync(t("team.dismiss.title", { name: member.name }), { body: t("ph.team.dismiss.live", { name: member.name }), action: t("ph.team.dismiss.release"), danger: false }))) return;
    await act(member, projectId, "release", toast);
    return;
  }
  if (!(await confirmAsync(t("team.dismiss.title", { name: member.name }), { body: t("team.dismiss.body"), action: t("team.dismiss") }))) return;
  try {
    await api.delete(`/api/staff/${enc(member.id)}`);
    toast(t("team.dismissed", { name: member.name }));
    invalidate(`/api/projects/${enc(projectId)}/staff`);
    onDone?.();
  } catch (e) {
    toast(errorText(e));
  }
}

/** The team runtime's three controls, as the member's header uses them. */
export async function act(member: Staff, projectId: string, what: "interrupt" | "pause" | "release", toast: (text: string) => void): Promise<void> {
  if (what === "release" && !(await confirmAsync(t("focus.staff.release.title", { name: member.name }), { body: t("focus.staff.release.body"), action: t("focus.staff.release") }))) return;
  try {
    await api.post(`/api/staff/${enc(member.id)}/${what}`, what === "release" ? { keep_worktree: true } : undefined);
    toast(t(what === "interrupt" ? "focus.staff.interrupted" : what === "pause" ? "focus.staff.pausing" : "focus.staff.released", { name: member.name }));
    invalidate(`/api/staff/${enc(member.id)}`);
    invalidate(`/api/projects/${enc(projectId)}/staff`);
  } catch (e) {
    toast(errorText(e));
  }
}

/** A member's state as a word in its colour, and what it is on or why it waits. */
function stateLine(member: Staff, tone: StaffTone, task?: string): { word: string; rest: string; tone: StaffTone | "queued" } {
  const wait = firstWait(member);
  if (wait) return { word: t("ph.team.waits"), rest: t(waitKey(wait.reason)) + (wait.position ? ` · №${wait.position}` : ""), tone: "queued" };
  if (member.status === "exited" || member.status === "error") return { word: t(`team.status.${member.status}`), rest: member.live?.waiting_for ?? "", tone };
  return { word: t(`focus.tone.${tone}`), rest: [task, member.live?.waiting_for].filter(Boolean).join(" · "), tone };
}

type Group = { key: "problems" | "working" | "waiting" | "off"; members: Staff[] };

function grouped(members: Staff[]): Group[] {
  const of = (m: Staff): Group["key"] => {
    const tone = staffTone(m);
    if (tone === "error" || tone === "silent") return "problems";
    if (tone === "working" || tone === "review") return "working";
    if (tone === "waiting") return "waiting";
    return "off";
  };
  return (["problems", "working", "waiting", "off"] as const).map((key) => ({ key, members: members.filter((m) => of(m) === key) })).filter((g) => g.members.length > 0);
}

export function PhoneTeamList({ projectId, team, tasks, usage, toast, onEdit, onHire, empty }: { projectId: string; team: Team; tasks: Map<string, string>; usage: ProjectUsage | null; toast: (text: string) => void; onEdit: (member: Staff) => void; onHire?: () => void; empty?: ReactNode }) {
  const offline = useOffline();
  const { team: members, oneOff } = splitTeam(team.staff);
  const [open, setOpen] = useState<Record<string, boolean>>({ off: false, waiting: false });
  const [more, setMore] = useState<Record<string, boolean>>({});
  if (members.length === 0 && oneOff.length === 0) {
    return (
      <>
        <EmptyState icon="bots" title={t("ph.team.empty")} body={t("ph.team.empty.sub")}
          action={onHire && !team.project.ephemeral && !team.project.system ? <button type="button" className="ph-btn accent" onClick={onHire}><Icon name="plus" size={18} />{t("ph.team.hire")}</button> : undefined} />
        {empty}
      </>
    );
  }
  const row = (member: Staff) => {
    const tone = staffTone(member);
    const task = member.live?.task_id ? tasks.get(member.live.task_id) : undefined;
    const line = stateLine(member, tone, task);
    const path = memberPath(projectId, member);
    const cli = member.harness !== "daedalus";
    const live = !!member.live;
    const spend = spendLine(staffUsage(usage, member.id));
    const items: MenuItem[] = [
      ...(live && path ? [{ label: t("ph.team.tell", { name: member.name }), icon: "send" as const, hint: t("ph.team.tell.hint"), onSelect: () => navigate(path) }] : []),
      ...(path ? [{ label: t(cli ? "ph.team.feed" : "ph.team.chat"), icon: cli ? "journal" as const : "bots" as const, onSelect: () => navigate(path) }] : []),
      ...(cli && member.live?.terminal_id ? [{ label: t("ph.team.terminal"), icon: "terminal" as const, onSelect: () => navigate(pathFor("terminals", member.live!.terminal_id!)) }] : []),
      ...(live && !offline ? [
        "-" as const,
        { label: t("ph.team.pause"), icon: "pause" as const, disabled: !!member.live?.pause_requested, onSelect: () => void act(member, projectId, "pause", toast) },
        { label: t("focus.staff.interrupt"), icon: "stop" as const, hint: t("ph.team.interrupt.hint"), onSelect: () => void act(member, projectId, "interrupt", toast) },
        { label: t("focus.staff.release"), icon: "unlink" as const, hint: t("ph.team.release.hint"), onSelect: () => void act(member, projectId, "release", toast) },
      ] : []),
      { label: t("ph.team.edit"), icon: "pen", onSelect: () => onEdit(member) },
      ...(!offline && !team.project.ephemeral ? [{ label: t("team.dismiss"), icon: "trash" as const, danger: true, onSelect: () => void dismissMember(member, projectId, toast) }] : []),
    ];
    const wait = firstWait(member);
    return (
      <ListRow
        key={member.id}
        className="ph-srow-member"
        data={{ staff: member.id }}
        lead={<span className="ph-member-av"><StaffAvatar name={member.name} color={member.color} /><span className="ph-st" data-tone={tone} aria-label={t(`focus.tone.${tone}`)} role="img" /></span>}
        title={
          <span className="ph-member-t">
            <b>{member.name}</b>
            {!member.one_off && member.role && <span className="ph-member-role">{member.role}</span>}
            <HarnessBadge harness={member.harness} />
            {member.env === "host" && <span className="ph-tag warn ph-host"><Icon name="lock" size={12} />{t("term.env.host")}</span>}
          </span>
        }
        label={member.name}
        meta={wait ? undefined : <><span className="ph-member-st" data-tone={line.tone}>{line.word}</span>{line.rest && <><span className="ph-sep" /><span className="ph-ell">{line.rest}</span></>}</>}
        body={wait || spend ? (
          <>
            {/* A wait's reason may take two lines: it is the one thing that says why the member is
                stuck, and one line cut "the machine's terminal limit is reached" before its verb. */}
            {wait && <span className="ph-member-wait" title={wait.detail || undefined}>{t("focus.wait.line", { reason: t(waitKey(wait.reason)), n: wait.position })}</span>}
            {spend && <span className="ph-member-spend">{spend}</span>}
          </>
        ) : undefined}
        trail={<span className="ph-member-tm">{wait ? t("ph.team.queued") : member.live?.started_at ? relTime(member.live.started_at) : ""}</span>}
        onOpen={() => (path ? navigate(path) : onEdit(member))}
        actions={items}
        more={false}
        preview={{ title: <span className="ph-member-pv"><StaffAvatar name={member.name} color={member.color} /><span>{member.name}{member.role ? ` · ${member.role}` : ""}</span></span>, meta: [line.word, line.rest].filter(Boolean).join(" · ") }}
      />
    );
  };
  const big = members.length > GROUP_AT;
  return (
    <>
      {!big && members.length > 0 && (
        <section>
          <SectionHeader count={members.length}>{t("ph.team.staff")}</SectionHeader>
          <div className="ph-list" role="list">{members.map(row)}</div>
        </section>
      )}
      {big && grouped(members).map((group) => {
        const folded = group.key === "off" || group.key === "waiting";
        const shown = open[group.key] ?? !folded;
        const all = more[group.key];
        const visible = !shown ? [] : all || group.members.length <= FOLD_AT + 1 ? group.members : group.members.slice(0, FOLD_AT);
        return (
          <section key={group.key} data-group={group.key}>
            <SectionHeader tone={group.key === "problems" ? "bad" : undefined} count={group.members.length}
              action={group.key === "problems" ? undefined : <button type="button" className="ph-ib" aria-expanded={shown} aria-label={t(shown ? "ph.team.fold" : "ph.team.unfold", { group: t(`ph.team.group.${group.key}`) })} onClick={() => setOpen((o) => ({ ...o, [group.key]: !shown }))}><Icon name={shown ? "up" : "down"} size={18} /></button>}>
              {t(`ph.team.group.${group.key}`)}
            </SectionHeader>
            <div className="ph-list" role="list">{visible.map(row)}</div>
            {shown && !all && group.members.length > FOLD_AT + 1 && (
              <button type="button" className="ph-showmore" onClick={() => setMore((m) => ({ ...m, [group.key]: true }))}>
                <span>{plural(`ph.team.more.${group.key}`, group.members.length - FOLD_AT)}</span><Icon name="down" size={16} />
              </button>
            )}
          </section>
        );
      })}
      {oneOff.length > 0 && (
        <section>
          <SectionHeader count={oneOff.length}>{t("focus.oneoff")}</SectionHeader>
          <div className="ph-list" role="list">{oneOff.map(row)}</div>
        </section>
      )}
    </>
  );
}
