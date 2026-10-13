// "Choose a project" for a chat about to start: every project, searchable, with "no project" (a chat
// of its own) first. The start screen used to name three projects and the voice project as chips, and
// the rest only through a "N more" list without a search; with a dozen projects the one wanted was a
// hunt, and on a phone there was no choice at all short of the full new-agent sheet.
//
// One list for both widths: a desktop shows it in a popover under the chips, a phone in a sheet.

import { useMemo, useState } from "react";
import type { Project } from "./api";
import { projectPath, projectReachable } from "./folders";
import { t } from "./i18n";
import { Icon } from "./icons";

/** The projects whose name or path holds every word of the query, in the order given. */
export function filterProjects(projects: Project[], query: string): Project[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return projects;
  return projects.filter((p) => {
    const text = `${p.name} ${projectPath(p)}`.toLowerCase();
    return words.every((word) => text.includes(word));
  });
}

export function ProjectPickList({ projects, target, onPick }: { projects: Project[]; target: string; onPick: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const shown = useMemo(() => filterProjects(projects, query), [projects, query]);
  // Enter takes the first match, so a name typed to its first letters is one key from being picked.
  const first = shown.find(projectReachable);
  const row = (id: string, name: string, sub: string, icon: "bots" | "folder" | "mic", disabled = false, note = "", path = true) => (
    <button key={id || "chat"} type="button" className={`project-pick-row ${target === id ? "on" : ""}`} data-project={id || undefined} disabled={disabled} aria-pressed={target === id} onClick={() => onPick(id)}>
      <span className="project-pick-tile"><Icon name={icon} size={14} /></span>
      <span className="project-pick-main">
        <span className="project-pick-name truncate">{name}</span>
        {(sub || note) && <span className="project-pick-sub truncate">{note && <b className="project-pick-note">{note} · </b>}<span className={path ? "mono" : ""}>{sub}</span></span>}
      </span>
      {target === id && <Icon name="check" size={16} />}
    </button>
  );
  return (
    <div className="project-pick">
      <label className="project-pick-search">
        <Icon name="search" size={15} />
        <input type="search" autoFocus value={query} maxLength={200} placeholder={t("start.pick.search")} aria-label={t("start.pick.search")}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && first) { e.preventDefault(); onPick(first.id); } }} />
      </label>
      <div className="project-pick-list" role="group" aria-label={t("start.where.label")}>
        {!query.trim() && row("", t("start.pick.none"), t("start.pick.none.sub"), "bots", false, "", false)}
        {shown.map((p) => row(p.id, p.name, projectPath(p), p.system === "companion" ? "mic" : "folder", !projectReachable(p), projectReachable(p) ? "" : t("project.notmounted")))}
        {query.trim() && shown.length === 0 && <div className="project-pick-empty sub">{t("common.nothing")}</div>}
      </div>
    </div>
  );
}
