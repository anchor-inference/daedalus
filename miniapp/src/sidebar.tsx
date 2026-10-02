// The left column on a desktop in Agents mode: the sessions, grouped by project, with the project
// switcher, search and New at the top. Orchestration mode has its column of its own
// (orchestration.tsx). It stands beside the rail (rail.tsx), which holds the modes, the daily
// destinations and the menu with the rest. Folded, the column is gone and the rail alone is left: the
// rail is the folded form, so there is no strip of its own to keep in step with it.

import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import { retried } from "./chunks";
import type { Project } from "./api";
import { Icon } from "./icons";
import { pathFor } from "./router";
import { go } from "./shell";
import { t } from "./i18n";
import { Bell } from "./bell";

const SessionsScreen = lazy(retried(() => import("./screens/Sessions"), (m) => ({ default: m.SessionsScreen })));

export type SidebarProps = {
  session: string | null;
  /** Folds the column away; the rail's Home unfolds it. */
  onToggle: () => void;
  projects: Project[];
  project: string;
  onProjects: () => void;
  onOpen: (id: string) => void;
  toast: (t: string) => void;
};

/** The fold at the end of a column's brand row. The rail's Home is the way back: pointed at while
 *  the column is folded, it turns into this same button. */
export function FoldButton({ onToggle }: { onToggle: () => void }) {
  return (
    <button className="iconbtn quiet sidebar-fold" onClick={onToggle} title={t("shell.sidebar.collapse")} aria-label={t("shell.sidebar.collapse")} aria-expanded>
      <Icon name="columns" size={18} />
    </button>
  );
}

export function Sidebar(p: SidebarProps) {
  return (
    <nav className="sidebar" aria-label={t("shell.sidebar.label")}>
      <div className="sidebar-brand">
        <a className="brand" href={pathFor("agents")} onClick={(e) => go(e, pathFor("agents"))} title="Daedalus">
          <span className="sidebar-text">Daedalus</span>
        </a>
        <Bell />
        <FoldButton onToggle={p.onToggle} />
      </div>
      <div className="sidebar-body">
        <Suspense fallback={null}>
          <SessionsScreen onOpen={p.onOpen} toast={p.toast} current={p.session ?? undefined} compact project={p.project} projects={p.projects} onProjects={p.onProjects} />
        </Suspense>
      </div>
    </nav>
  );
}

/** The sidebar's collapse, with the window's width as the default until the operator chooses. */
export function useSidebar(read: (w: number) => boolean, remember: (c: boolean) => void): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(() => read(window.innerWidth));
  const toggle = useCallback(() => {
    setCollapsed((c) => {
      remember(!c);
      return !c;
    });
  }, [remember]);
  // A choice never made follows the window: a laptop lid at 1100 px gets the rail alone, docked at 1440 the column.
  useEffect(() => {
    const on = () => setCollapsed(read(window.innerWidth));
    window.addEventListener("resize", on);
    return () => window.removeEventListener("resize", on);
  }, [read]);
  return [collapsed, toggle];
}
