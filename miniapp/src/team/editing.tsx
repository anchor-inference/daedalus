// Who is editing which file in the project's shared folders right now, as the host's edit board has
// it: one compact line per agent. Shown on the team page and on its phone tab alike.

import type { ReactNode } from "react";
import { relTime } from "../format";
import { t } from "../i18n";
import type { Editing } from "./team";

/** Who is changing which files in the shared folders right now: one line per agent, its first files
 *  and how long ago it last wrote. A file two agents hold is where they may overwrite each other. */
export function EditingNow({ editing, heading }: { editing: Editing[]; heading?: ReactNode }) {
  const shared = new Set(editing.flatMap((e) => e.files.map((f) => f.path)).filter((p, i, all) => all.indexOf(p) !== i));
  return (
    <>
      {heading ?? <div className="section-title">{t("team.editing.section")}</div>}
      <div className="team-editing">
        {editing.map((e) => (
          <div key={e.holder} className="team-editing-row">
            <span className="team-editing-who truncate">{e.kind === "member" ? e.name : t("team.editing.session", { name: e.name })}</span>
            <span className="team-editing-files truncate" title={e.files.map((f) => f.path).join("\n")}>
              {e.files.slice(0, 3).map((f, i) => (
                <span key={f.path} className={shared.has(f.path) ? "overlap" : undefined}>{i > 0 ? ", " : ""}{f.shown}</span>
              ))}
              {e.files.length > 3 ? ` ${t("team.editing.more", { n: e.files.length - 3 })}` : ""}
            </span>
            <span className="team-editing-age">{relTime(Date.now() - e.age_s * 1000)}</span>
          </div>
        ))}
      </div>
    </>
  );
}
