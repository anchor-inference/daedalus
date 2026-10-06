// A project's team: who is on it, what each of them runs on and where, and what they are doing now.
// Hiring, editing and dismissing happen in the sheet over it. The page stands on its own for now and
// becomes a page of the project's focus mode later, so it takes the project by id and nothing else.

import { useState } from "react";
import { Skeleton } from "../ui/components";
import { Icon } from "../icons";
import { navigate, pathFor, projectPagePath } from "../router";
import { PageHeader } from "../shell";
import { invalidate, useQuery } from "../store";
import { useUsage } from "../project/data";
import { spendLine, staffUsage } from "../project/usage";
import { plural, t } from "../i18n";
import { HarnessBadge, StaffAvatar } from "./parts";
import { StaffSheet } from "./StaffSheet";
import { Setups } from "./setups";
import { HARNESS_NAMES, Staff, Team, statusTone } from "./team";
import { firstWait, waitKey } from "../project/focus";
import { HealthLine } from "../staff/health";

export function TeamPage({ projectId, toast, back }: { projectId: string; toast: (text: string) => void; back?: string | null }) {
  const [showArchived, setShowArchived] = useState(false);
  const key = `/api/projects/${encodeURIComponent(projectId)}/staff?archived=${showArchived ? 1 : 0}`;
  const { data: team, error, loading, refresh } = useQuery<Team>(key, { pollMs: 15000, staleMs: 5000 });
  const usage = useUsage(projectId);
  const [hiring, setHiring] = useState(false);
  const [editing, setEditing] = useState<Staff | null>(null);
  const reload = () => {
    invalidate(`/api/projects/${encodeURIComponent(projectId)}/staff`);
    refresh();
  };

  const closed = team && (team.project.ephemeral || !!team.project.system);
  const subtitle = team
    ? [plural("team.count.staff", team.counts.staff), plural("team.count.working", team.counts.working), t("team.limit", { n: team.project.concurrency })].join(" · ")
    : undefined;
  const active = (team?.staff ?? []).filter((m) => !m.archived_at);
  const archived = (team?.staff ?? []).filter((m) => m.archived_at);

  return (
    <>
      <PageHeader
        title={team ? t("team.title.of", { name: team.project.name }) : t("team.title")}
        subtitle={subtitle}
        back={back === undefined ? pathFor("agents") : back ?? undefined}
        actions={
          team && !closed ? (
            <>
              <button className="iconbtn" onClick={() => navigate(projectPagePath(projectId, "board"))} title={t("pboard.title")} aria-label={t("pboard.title")}><Icon name="board" /></button>
              <button className="iconbtn primary" onClick={() => setHiring(true)} title={t("team.hire")} aria-label={t("team.hire")}><Icon name="plus" /></button>
            </>
          ) : undefined
        }
      >
        {team && !closed && (
          <div className="chips">
            <button className="chip select" aria-pressed={!showArchived} onClick={() => setShowArchived(false)}>{t("team.filter.active")}</button>
            <button className="chip select" aria-pressed={showArchived} onClick={() => setShowArchived(true)}>{t("team.filter.all")}</button>
          </div>
        )}
      </PageHeader>
      <div className="screen narrow team">
        {loading && !team && !error && <Skeleton rows={3} />}
        {error && !team && (
          <div className="empty">
            <b>{t("team.error")}</b>
            <div>{error}</div>
            <button className="btn primary" onClick={refresh}>{t("common.retry")}</button>
          </div>
        )}
        {team && closed && (
          <div className="empty">
            <b>{t("team.closed")}</b>
            <div>{t(team.project.system ? "team.closed.system" : "team.closed.chat")}</div>
          </div>
        )}
        {team && !closed && active.length === 0 && (
          <div className="empty">
            <b>{t("team.empty")}</b>
            <div>{t("team.empty.sub")}</div>
            <button className="btn primary" onClick={() => setHiring(true)}><Icon name="plus" size={15} /> {t("team.hire")}</button>
            <Setups team={team} toast={toast} onDone={reload} />
          </div>
        )}
        {team && !closed && active.length > 0 && (
          <div className="staff-list">
            {active.map((m) => <StaffRow key={m.id} member={m} team={team} spend={spendLine(staffUsage(usage, m.id))} onOpen={() => setEditing(m)} />)}
          </div>
        )}
        {team && !closed && showArchived && archived.length > 0 && (
          <>
            <div className="section-title">{t("team.dismissed.section")}</div>
            <div className="staff-list archived">
              {archived.map((m) => <StaffRow key={m.id} member={m} team={team} spend={spendLine(staffUsage(usage, m.id))} />)}
            </div>
          </>
        )}
      </div>
      {hiring && team && <StaffSheet team={team} onClose={() => setHiring(false)} onDone={reload} toast={toast} />}
      {editing && team && <StaffSheet team={team} member={editing} onClose={() => setEditing(null)} onDone={reload} toast={toast} />}
    </>
  );
}

function StaffRow({ member, team, spend, onOpen }: { member: Staff; team: Team; spend: string | null; onOpen?: () => void }) {
  const folder = team.project.folders.find((f) => f.id === member.default_folder_id);
  // A Daedalus member runs where its folder is (the primary one when it names none).
  const env = member.harness === "daedalus" ? (folder ?? team.project.folders[0])?.env ?? team.project.local_env : member.env || team.project.default_env;
  const model = member.model ? team.choices.presets.find((p) => p.id === member.model)?.label ?? member.model : t("team.model.default.short");
  const runs = [HARNESS_NAMES[member.harness], model, member.permission_mode].filter(Boolean).join(" · ");
  const where = [t(`team.env.${env}`), t(`team.isolation.${member.isolation}.short`), folder ? folder.label || folder.path.split("/").pop() : "", plural("team.count.sessions", member.sessions)].filter(Boolean).join(" · ");
  const waiting = member.live?.waiting_for;
  // A launch that waits says why: the queue's reason in words, its place, and the host's own sentence
  // with the numbers in it on hover.
  const queued = firstWait(member);
  const status = queued ? (
    <span className="status waiting" title={queued.detail || undefined}>
      <span className="dot" aria-hidden />
      {t("focus.wait.line", { reason: t(waitKey(queued.reason)), n: queued.position })}
    </span>
  ) : (
    <span className={`status ${statusTone(member.status)}`} title={waiting || undefined}>
      <span className="dot" aria-hidden />
      {t(`team.status.${member.status}`)}
    </span>
  );
  const body = (
    <>
      <StaffAvatar name={member.name} color={member.color} />
      <div className="staff-main">
        <div className="staff-line">
          <span className="staff-name truncate">{member.name}{member.agent && <span className="staff-agent"> · {member.agent}</span>}</span>
          {member.one_off && <span className="badge">{t("team.oneoff.badge")}</span>}
          {member.archived_at && <span className="badge">{t("team.dismissed.badge")}</span>}
        </div>
        {member.role && <div className="staff-role">{member.role}</div>}
        <div className="staff-meta">
          <span>{runs}</span>
          <span>{where}</span>
          {spend && <span className="staff-spend">{spend}</span>}
        </div>
        <HealthLine health={member.health ?? null} compact />
        {/* On a phone the status moves under the name, so a long one never squeezes the name to nothing. */}
        <div className="staff-status-inline">{status}</div>
      </div>
      <div className="staff-side">
        {status}
        <HarnessBadge harness={member.harness} />
      </div>
    </>
  );
  return onOpen ? (
    <button className="staff-row" onClick={onOpen} aria-label={t("team.edit.for", { name: member.name })}>{body}</button>
  ) : (
    <div className="staff-row">{body}</div>
  );
}
