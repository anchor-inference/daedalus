// The centre of a project's focus mode: the orchestrator's chat (or the way to switch one on), a
// session of the project, or one of its pages. The route decides which, through `focusView`; this
// file only mounts what that names, with the project's way back instead of the agents list's.

import { lazy, Suspense } from "react";
import { SessionScreen, retried } from "../chunks";
import { Skeleton } from "../ui/components";
import { t } from "../i18n";
import { ORCHESTRATION, ORCHESTRATION_LIST, back as goBack, navigate, projectHome, projectPagePath, useRoute } from "../router";
import { useProject } from "./data";
import { focusView } from "./focus";
import { BriefPage, EnableOrchestrator, FoldersPage, JournalPage, TerminalsPage, WakeupsPage } from "./pages";
import { SetupLine } from "../main/cards";
import { PhoneBoard, PhoneTeam, PhoneTerminals } from "./phone";

const TeamPage = lazy(retried(() => import("../team/TeamPage"), (m) => ({ default: m.TeamPage })));
const ProjectBoard = lazy(retried(() => import("../board/ProjectBoard"), (m) => ({ default: m.ProjectBoard })));
const StaffView = lazy(retried(() => import("../staff/StaffView"), (m) => ({ default: m.StaffView })));

export function ProjectScreen({ projectId, page, inner, toast, wide }: { projectId: string; page: string | null; inner: string | null; toast: (text: string) => void; wide: boolean }) {
  const route = useRoute();
  const { project, loading } = useProject(projectId);
  const view = focusView(page, inner);
  // On a desktop the sidebar is the way back; a phone has no sidebar, so every page carries one.
  const home = projectHome(projectId);
  const back = wide ? null : home;
  if (!project) return loading ? <Skeleton rows={4} /> : <div className="empty"><b>{t("team.noproject")}</b></div>;

  let body;
  if (view.kind === "orchestrator") {
    const orchestrator = project.settings.orchestrator;
    body = orchestrator?.enabled && orchestrator.session_id ? (
      <SessionScreen
        key={orchestrator.session_id}
        id={orchestrator.session_id}
        focus={{ projectId, kind: "orchestrator" }}
        onBack={() => navigate(wide ? ORCHESTRATION : ORCHESTRATION_LIST)}
        toast={toast}
        // A project the main orchestrator is setting up says so, with the button that ends the setup.
        // What waits for the operator is in the Questions tab, which a phone opens from the header.
        banner={project.setup_by === "dispatcher" ? <SetupLine projectId={projectId} name={project.name} toast={toast} /> : undefined}
      />
    ) : (
      // The project's home, so a phone's way back is the orchestration list it was picked from.
      <EnableOrchestrator project={project} toast={toast} back={wide ? null : ORCHESTRATION_LIST} />
    );
  } else if (view.kind === "session") {
    // A member's conversation is reached from the team on a phone, and goes back there.
    body = <SessionScreen key={view.id} id={view.id} focus={{ projectId, kind: "member" }} onBack={() => (wide ? navigate(home) : goBack(projectPagePath(projectId, "team")))} toast={toast} />;
  } else if (view.kind === "staff") {
    // A command-line member, reached from its row: on a phone back leads to the team it came from.
    body = <StaffView key={view.id} projectId={projectId} staffId={view.id} wide={wide} toast={toast} onBack={() => (wide ? navigate(home) : goBack(projectPagePath(projectId, "team")))} />;
  } else if (!wide && view.page === "team") {
    body = <PhoneTeam projectId={projectId} toast={toast} />;
  } else if (!wide && view.page === "board") {
    body = <PhoneBoard projectId={projectId} toast={toast} board={(bannered) => <ProjectBoard projectId={projectId} toast={toast} embedded selected={route.query.get("task")} bannered={bannered} />} />;
  } else if (!wide && view.page === "terminals") {
    body = <PhoneTerminals projectId={projectId} toast={toast} />;
  } else if (view.page === "team") {
    body = <TeamPage projectId={projectId} toast={toast} back={back} />;
  } else if (view.page === "board") {
    body = <ProjectBoard projectId={projectId} toast={toast} selected={route.query.get("task")} back={back} />;
  } else if (view.page === "journal") {
    body = <JournalPage projectId={projectId} back={back} toast={toast} />;
  } else if (view.page === "brief") {
    body = <BriefPage projectId={projectId} back={back} toast={toast} />;
  } else if (view.page === "wakeups") {
    body = <WakeupsPage projectId={projectId} back={back} toast={toast} />;
  } else if (view.page === "folders") {
    body = <FoldersPage projectId={projectId} back={back} toast={toast} />;
  } else {
    body = <TerminalsPage projectId={projectId} selected={route.query.get("t")} back={back} toast={toast} />;
  }
  return <Suspense fallback={<div className="empty">{t("common.loading")}</div>}>{body}</Suspense>;
}
