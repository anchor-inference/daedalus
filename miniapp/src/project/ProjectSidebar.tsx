// The left column in a project's focus mode: only what belongs to the project. The way back to every
// project, the project and how it is set up, its orchestrator, the team with each member's state and
// why a launch waits, the one-off helpers, the project's terminals and its pages. It takes the place of
// the sessions column (App.tsx decides which one is drawn, in one place), and keeps that column's
// frame — the brand row with the bell and the fold, the drag handle — so the shell around it does not
// move when a project is entered. Folded, it is gone like the other columns, and the rail is left.

import { useState, type ReactNode, type RefObject } from "react";
import { Bell } from "../bell";
import { plural, t } from "../i18n";
import { EnvPill } from "../envpill";
import { Icon, type IconName } from "../icons";
import { PaneHandle, type PaneDrag } from "../layout";
import { FoldButton } from "../sidebar";
import { relTime } from "../format";
import { ORCHESTRATION, navigate, projectHome, projectPagePath, projectSessionPath, projectStaffPath } from "../router";
import { go } from "../shell";
import { HarnessBadge, StaffAvatar } from "../team/parts";
import { StaffSheet } from "../team/StaffSheet";
import type { Staff } from "../team/team";
import { invalidate } from "../store";
import { useFocus, useProject, useUsage, staffKey } from "./data";
import { chipText, totalsLine } from "./usage";
import { FocusView, firstWait, splitTeam, staffTone, waitKey } from "./focus";

export type ProjectSidebarProps = {
  projectId: string;
  view: FocusView;
  /** The terminal the Terminals page shows, from its route. */
  terminal: string | null;
  onToggle: () => void;
  drag: PaneDrag;
  toast: (text: string) => void;
  /** Room at the top for the entry pinned above every project: the main orchestrator's. */
  pinned?: ReactNode;
};

export function ProjectSidebar(p: ProjectSidebarProps) {
  const { project } = useProject(p.projectId);
  const { team, board, terminals, sessions, wakeups: alarms, watches } = useFocus(p.projectId);
  const usage = useUsage(p.projectId);
  const spent = chipText(usage);
  const [hiring, setHiring] = useState(false);
  const orchestrator = project?.settings.orchestrator;
  const orchestratorId = orchestrator?.enabled ? orchestrator.session_id : "";
  const orchestratorRow = orchestratorId ? sessions?.sessions.find((s) => s.id === orchestratorId) : undefined;
  const { team: members, oneOff } = splitTeam(team?.staff ?? []);
  const tasks = new Map((board?.tasks ?? []).map((task) => [task.id, task]));
  const openTasks = (board?.tasks ?? []).filter((task) => task.status !== "done" && task.status !== "dropped").length;
  // The page holds both: the orchestrator's alarms and the project's watches that are switched on.
  const wakeups = (orchestratorId ? alarms.filter((w) => w.enabled).length : 0) + watches.filter((w) => w.enabled).length;
  const here = (page: string) => p.view.kind === "page" && p.view.page === page;
  // A project is entered from orchestration mode's list, and its way back leads there.
  const allProjects = ORCHESTRATION;

  return (
    <nav className="sidebar project-sidebar" aria-label={t("focus.label", { name: project?.name ?? "" })}>
      <div className="sidebar-brand">
        <a className="brand" href={allProjects} onClick={(e) => go(e, allProjects)} title="Daedalus">
          <span className="sidebar-text">Daedalus</span>
        </a>
        <Bell />
        <FoldButton onToggle={p.onToggle} />
      </div>
      {p.pinned && <div className="sidebar-pinned" aria-label={t("focus.pinned")}>{p.pinned}</div>}
      <div className="sidebar-body focus-body">
        <a className="focus-back" href={allProjects} onClick={(e) => go(e, allProjects)}>
          <Icon name="back" size={16} />
          <span>{t("focus.all")}</span>
        </a>
        <div className="focus-project">
          <div className="focus-project-line">
            <span className="focus-project-name truncate">{project?.name ?? "…"}</span>
            {project && <EnvPill env={project.settings.default_env ?? project.folders[0]?.env ?? "container"} tiny />}
          </div>
          {project && (
            <div className="focus-project-meta truncate">
              {[plural("focus.count.folders", project.folders.length), team ? plural("team.count.staff", team.counts.staff) : "", orchestrator?.enabled ? t("focus.autonomy", { level: t(`focus.autonomy.${orchestrator.autonomy}`) }) : ""].filter(Boolean).join(" · ")}
            </div>
          )}
          {spent && <span className="chip tiny focus-spend" title={usage ? totalsLine(usage) : undefined}>{spent}</span>}
        </div>

        <FocusRow
          icon="conductor"
          label={t("focus.orchestrator")}
          href={projectHome(p.projectId)}
          current={p.view.kind === "orchestrator"}
          meta={!orchestrator?.enabled ? t("focus.orchestrator.off") : orchestratorRow?.status === "running" ? t("focus.orchestrator.now") : orchestratorRow?.last_message_at ? relTime(orchestratorRow.last_message_at) : ""}
          className={`orchestrator ${orchestratorRow?.status === "running" ? "live" : ""} ${!orchestrator?.enabled ? "off" : ""}`}
        />

        <div className="focus-sec">
          <a href={projectPagePath(p.projectId, "team")} onClick={(e) => go(e, projectPagePath(p.projectId, "team"))} className={here("team") ? "current" : ""}>
            {t("focus.team")} <span className="n">{members.length}</span>
          </a>
          {team && <button className="iconbtn small quiet" onClick={() => setHiring(true)} title={t("team.hire")} aria-label={t("team.hire")}><Icon name="plus" size={16} /></button>}
        </div>
        {team && members.length === 0 && <div className="focus-none">{t("focus.nobody")}</div>}
        {members.map((m) => <StaffRow key={m.id} projectId={p.projectId} member={m} taskTitle={m.live?.task_id ? tasks.get(m.live.task_id)?.title : undefined} current={isCurrent(p.view, m)} />)}

        {oneOff.length > 0 && (
          <>
            <div className="focus-sec"><span>{t("focus.oneoff")} <span className="n">{oneOff.length}</span></span></div>
            {oneOff.map((m) => <StaffRow key={m.id} projectId={p.projectId} member={m} taskTitle={m.live?.task_id ? tasks.get(m.live.task_id)?.title : undefined} current={isCurrent(p.view, m)} compact />)}
          </>
        )}

        {terminals && terminals.terminals.length > 0 && (
          <>
            <div className="focus-sec">
              <a href={projectPagePath(p.projectId, "terminals")} onClick={(e) => go(e, projectPagePath(p.projectId, "terminals"))} className={here("terminals") ? "current" : ""}>
                {t("focus.terminals")} <span className="n">{terminals.terminals.length}</span>
              </a>
            </div>
            {terminals.terminals.slice(0, 6).map((term) => (
              <FocusRow
                key={term.id}
                icon="terminal"
                label={term.title || t("term.untitled")}
                href={projectPagePath(p.projectId, "terminals", { t: term.id })}
                current={here("terminals") && p.terminal === term.id}
                meta={term.status === "running" ? "" : term.exit_code !== null ? t("focus.terminal.code", { code: term.exit_code }) : term.exit_signal ?? ""}
                className={term.status === "running" ? "" : "ended"}
              />
            ))}
          </>
        )}

        <div className="focus-sec"><span>{t("focus.project")}</span></div>
        <FocusRow icon="board" label={t("focus.page.board")} href={projectPagePath(p.projectId, "board")} current={here("board")} meta={board ? String(openTasks) : ""} />
        <FocusRow icon="pen" label={t("focus.page.brief")} href={projectPagePath(p.projectId, "brief")} current={here("brief")} />
        <FocusRow icon="clock" label={t("focus.page.wakeups")} href={projectPagePath(p.projectId, "wakeups")} current={here("wakeups")} meta={wakeups ? String(wakeups) : ""} />
        <FocusRow icon="journal" label={t("focus.page.journal")} href={projectPagePath(p.projectId, "journal")} current={here("journal")} />
        <FocusRow icon="folder" label={t("focus.page.folders")} href={projectPagePath(p.projectId, "folders")} current={here("folders")} meta={project ? String(project.folders.length) : ""} />
      </div>
      <PaneHandle side="right" drag={p.drag} />
      {hiring && team && <StaffSheet team={team} onClose={() => setHiring(false)} onDone={() => invalidate(staffKey(p.projectId).split("?")[0])} toast={p.toast} />}
    </nav>
  );
}

function FocusRow({ icon, label, href, current, meta, className = "" }: { icon: IconName; label: string; href: string; current: boolean; meta?: string; className?: string }) {
  return (
    <a className={`focus-row ${current ? "current" : ""} ${className}`} href={href} onClick={(e) => go(e, href)} aria-current={current ? "page" : undefined}>
      <Icon name={icon} size={16} />
      <span className="focus-row-label truncate">{label}</span>
      {meta && <span className="focus-row-meta">{meta}</span>}
    </a>
  );
}

/** Where a member's row leads: into the session it works in, a command-line member's own view, or
 *  the team page for a Daedalus member with no session. */
function openMember(projectId: string, member: Staff): void {
  const session = member.live?.session_id;
  if (session) navigate(projectSessionPath(projectId, session));
  else if (member.harness !== "daedalus") navigate(projectStaffPath(projectId, member.id));
  else navigate(projectPagePath(projectId, "team"));
}

/** Whether the centre shows this member: its conversation, or its command-line view. */
function isCurrent(view: FocusView, member: Staff): boolean {
  if (view.kind === "staff") return view.id === member.id;
  return view.kind === "session" && !!member.live?.session_id && member.live.session_id === view.id;
}

/** A member of the team: who, what they are on, what runs them, and a dot for how it goes. A launch
 *  waiting in the queue says why it waits — "queued" alone is the question the operator would then
 *  have to go and ask. */
function StaffRow({ projectId, member, taskTitle, current, compact }: { projectId: string; member: Staff; taskTitle?: string; current: boolean; compact?: boolean }) {
  const tone = staffTone(member);
  const wait = firstWait(member);
  const line = wait ? t("focus.wait.line", { reason: t(waitKey(wait.reason)), n: wait.position }) : taskTitle ?? t(`focus.tone.${tone}`);
  return (
    <button className={`focus-staff ${current ? "current" : ""} ${compact ? "compact" : ""}`} onClick={() => openMember(projectId, member)} aria-current={current ? "page" : undefined} title={wait?.detail || member.live?.waiting_for || undefined}>
      <StaffAvatar name={member.name} color={member.color} size="small" />
      <span className="focus-staff-main">
        <span className="focus-staff-name truncate">
          {member.name}
          {member.role && !compact && <span className="focus-staff-role"> · {member.role}</span>}
        </span>
        {!compact && <span className={`focus-staff-line truncate ${wait ? "waits" : ""}`}>{line}</span>}
      </span>
      {/* The same outlined chip that marks a host terminal: an amber word inside the name once read as
          part of the role, or as a warning that had leaked out of the status line. */}
      {member.env === "host" && <EnvPill env="host" tiny />}
      <HarnessBadge harness={member.harness} />
      <span className={`focus-dot tone-${tone}`} aria-label={t(`focus.tone.${tone}`)} role="img" />
    </button>
  );
}
