import { useRef } from "react";
import { useContextActions } from "./ui/context-menu";
import { copyText } from "./ui/components";
// Orchestration mode's own parts: the left column that lists the main orchestrator and the projects
// with an orchestrator, and the same list as a page on a phone, where there is no left column. The
// rules — what belongs to which mode, what the rail's Orchestration item counts — are in mode.ts;
// this file draws them. The rail (rail.tsx) is where the operator switches between the modes.

import type { ProjectFolder, SessionList, SessionSummary } from "./api";
import { Bell } from "./bell";
import { Dot } from "./ui/components";
import { relTime } from "./format";
import { plural, t } from "./i18n";
import { Icon } from "./icons";
import { MainEntry } from "./main/MainEntry";
import { useMain } from "./main/data";
import { orchestratedProjects, waitingInOrchestration } from "./mode";
import { ORCHESTRATION, navigate, projectHome } from "./router";
import { FoldButton } from "./sidebar";
import { PageHeader, go, useMedia } from "./shell";
import { useQuery } from "./store";
import { useStreamUp } from "./events";

/** The listing both modes read: the Agents list filters it, orchestration mode picks its projects out
 *  of it. One key, so the sidebar and the rail's count share one request. */
function useListing() {
  const live = useStreamUp();
  return useQuery<SessionList>("/api/sessions", { pollMs: live ? 60000 : 5000, staleMs: 3000 });
}

/** How many requests wait for the operator in orchestration mode; the rail shows it from Agents. */
export function useOrchestrationWaiting(): number {
  const { data } = useListing();
  const { data: main } = useMain();
  return waitingInOrchestration(data?.projects ?? [], main);
}

/** A project in orchestration mode's list: what its orchestrator is doing, the size of the team and
 *  how many at work, and how many requests wait for the operator. */
function ProjectRow({ project, sessions }: { project: ProjectFolder; sessions: SessionSummary[] }) {
  const o = project.orchestrator!;
  const href = projectHome(project.id);
  const orchestrator = sessions.find((s) => s.id === o.session_id);
  // The listing is a page of the most recent sessions; an orchestrator quiet for long may not be on
  // it, and then the project's own last activity says when it last moved.
  const state = project.setup_by === "dispatcher" ? t("orch.row.setup")
    : orchestrator?.status === "running" ? t("orch.row.working")
    : orchestrator?.status === "waiting" ? t("orch.row.waiting")
    : project.last_message_at ? relTime(project.last_message_at) : t("orch.row.idle");
  const row = useRef<HTMLAnchorElement>(null);
  useContextActions(row, [{ label:t("focus.entry.open", { name:project.name }), icon:"folder", onSelect:() => navigate(href) }, { label:t("context.copyLink"), icon:"link", onSelect:() => void copyText(new URL(href, location.href).href) }]);
  const meta = [state, plural("team.count.staff", o.staff), o.working > 0 ? plural("team.count.working", o.working) : ""].filter(Boolean).join(" · ");
  return (
    <a ref={row} className="orch-row" href={href} onClick={(e) => go(e, href)} title={t("focus.entry.open", { name: project.name })} data-project={project.id}>
      <span className="orch-row-icon"><Icon name="conductor" size={15} /></span>
      <span className="orch-row-text">
        <span className="orch-row-name truncate">{project.name}</span>
        <span className="orch-row-meta truncate">{meta}</span>
      </span>
      {orchestrator?.status === "running" && <span className="folder-live"><Dot status="running" /></span>}
      {o.needs_you > 0 && <span className="needs-badge" title={plural("focus.entry.needs", o.needs_you)} aria-label={plural("focus.entry.needs", o.needs_you)}>{o.needs_you}</span>}
    </a>
  );
}

/** Main first, then every project with an orchestrator; the same in the column and on a phone's page.
 *  A project opened leaves this list for its own column (focus mode), so no row here is ever current. */
function OrchestrationRows({ onMain }: { onMain: boolean }) {
  const { data, loading } = useListing();
  const projects = orchestratedProjects(data?.projects ?? []);
  // The palette is a keyboard's way in; a phone or a tablet without a pointer has no ⌘K to press,
  // so there the empty line points at the Agents list, where every project is one tap away.
  const keyboard = useMedia("(hover: hover) and (pointer: fine)");
  return (
    <>
      <div className="orch-main"><MainEntry current={onMain} /></div>
      <div className="orch-section sub">{t("orch.projects")}</div>
      {projects.map((p) => <ProjectRow key={p.id} project={p} sessions={data?.sessions ?? []} />)}
      {!loading && data && projects.length === 0 && <div className="orch-empty sub">{t(keyboard ? "orch.empty" : "orch.empty.touch")}</div>}
    </>
  );
}

export type OrchestrationSidebarProps = {
  /** The main chat is open. */
  onMain: boolean;
  onToggle: () => void;
};

/** The left column in orchestration mode. It keeps the Agents column's frame — the brand row with the
 *  bell and the fold, the drag handle — so switching modes changes the list and nothing around it. */
export function OrchestrationSidebar(p: OrchestrationSidebarProps) {
  return (
    <nav className="sidebar orch-sidebar" aria-label={t("orch.label")}>
      <div className="sidebar-brand">
        <a className="brand" href={ORCHESTRATION} onClick={(e) => go(e, ORCHESTRATION)} title="Daedalus">
          <span className="sidebar-text">Daedalus</span>
        </a>
        <Bell />
        <FoldButton onToggle={p.onToggle} />
      </div>
      <div className="sidebar-body orch-body">
        <OrchestrationRows onMain={p.onMain} />
      </div>
    </nav>
  );
}

/** A phone's stand-in for the column: the main orchestrator and the projects, as a page. */
export function OrchestrationList() {
  return (
    <>
      <PageHeader title={t("mode.orchestration")} />
      <div className="screen orch-list">
        <OrchestrationRows onMain={false} />
      </div>
    </>
  );
}
