// Where a new chat runs, for a Docker installation: in the container, as every chat always has, or on
// the host through its terminal daemon. And the mark a session that runs on the host carries.

import { useRef, useState } from "react";
import type { Project } from "./api";
import { EnvPill } from "./envpill";
import { folderName } from "./folders";
import { Icon } from "./icons";
import { useEnvironments } from "./projects";
import { Popover, Sheet } from "./ui/dialogs";
import { SheetRow } from "./ui/phone";
import { t } from "./i18n";

export type RunEnv = "container" | "host";

export type RunOn = {
  /** A Docker installation, where a container and a host exist side by side and where a thing runs is
   *  worth saying at all. Natively everything is on the host and nothing is shown. */
  docker: boolean;
  /** Whether the choice is offered at all: a Docker installation, for a chat that names no project
   *  (a project's folder decides where its chats run). A native install is on the host already. */
  offered: boolean;
  /** Whether the host can take a chat now: its terminal daemon answers. */
  hostReady: boolean;
  /** What the chat will run on: the host only while it is picked and can take it. */
  env: RunEnv;
  pick: (env: RunEnv) => void;
  /** The field of the create call: set only for the host, so a container chat is sent as before. */
  body: { env?: "host" };
};

export function useRunOn(project: string): RunOn {
  const environments = useEnvironments().data;
  const [picked, setPicked] = useState<RunEnv>("container");
  const offered = !!environments?.docker && !project;
  const hostReady = !!environments?.host_bridge;
  // A daemon that went away after the pick falls back to the container rather than leaving a choice
  // the server will refuse; the menu then says why the host is greyed out.
  const env: RunEnv = offered && hostReady && picked === "host" ? "host" : "container";
  return { docker: !!environments?.docker, offered, hostReady, env, pick: setPicked, body: env === "host" ? { env: "host" } : {} };
}

/** The desktop composer's control: the pill of where the chat will run, opening the two choices. */
export function RunOnSelect({ run }: { run: RunOn }) {
  const button = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  if (!run.offered) return null;
  const choose = (env: RunEnv) => { run.pick(env); setOpen(false); };
  return (
    <>
      <button ref={button} type="button" className={`runon-select ${run.env}`} aria-haspopup="menu" aria-expanded={open}
        aria-label={`${t("runon.label")}: ${t(`term.env.${run.env}`)}`} title={t(`term.env.title.${run.env}`)} onClick={() => setOpen(!open)}>
        <EnvPill env={run.env} tiny />
      </button>
      {open && (
        <Popover anchor={button.current} onClose={() => setOpen(false)} className="runon-menu" label={t("runon.label")}>
          {(["container", "host"] as const).map((env) => {
            const blocked = env === "host" && !run.hostReady;
            return (
              <button key={env} type="button" role="menuitemradio" aria-checked={run.env === env} disabled={blocked} className={`runon-option ${run.env === env ? "on" : ""}`} onClick={() => choose(env)}>
                <span className="runon-option-main">
                  <span className="runon-option-t">{t(`folder.env.${env}.long`)}</span>
                  <span className="sub">{blocked ? t("runon.host.down") : t(`runon.${env}.hint`)}</span>
                </span>
                {run.env === env && <Icon name="check" size={16} />}
              </button>
            );
          })}
        </Popover>
      )}
    </>
  );
}

/**
 * The phone's chip under the new chat's heading, beside the project and the mode: where the chat's
 * tools will run. It used to be a row of the + sheet, the one place nobody looks before typing, and
 * an operator asked where the choice had gone. A chat of its own picks the container or the host
 * here. A project's chats run where its folder is, so with a project picked the chip shows that
 * folder's side and its sheet says how to change it — move the folder in the project's settings —
 * which is how a desktop changes it too; a choice here the server would refuse is not offered.
 */
export function RunOnChip({ run, project, onSettings }: { run: RunOn; project?: Project | null; onSettings?: () => void }) {
  const [open, setOpen] = useState(false);
  if (!run.docker) return null;
  const primary = project ? [...project.folders].sort((a, b) => a.position - b.position)[0] : undefined;
  if (project && !primary) return null;
  const env: RunEnv = primary ? primary.env : run.env;
  const label = t(`term.env.${env}`);
  const choose = (picked: RunEnv) => { run.pick(picked); setOpen(false); };
  return (
    <>
      <button type="button" className={`ph-btn sm runon-chip ${env}`} data-runon-chip={env} aria-haspopup="dialog" aria-expanded={open}
        aria-label={`${t("runon.label")}: ${label}`} title={t(`term.env.title.${env}`)} onClick={() => setOpen(true)}>
        <Icon name={env === "host" ? "lock" : "terminal"} size={14} />
        <span className="truncate">{label}</span>
        <Icon name="chevron" size={12} />
      </button>
      {open && (
        <Sheet title={t("runon.label")} onClose={() => setOpen(false)} className="runon-sheet" size="narrow">
          {project ? (
            <div className="runon-project" data-runon-project={project.id}>
              <p className="sub runon-project-why">{t("runon.project.body", { name: project.name })}</p>
              {[...project.folders].sort((a, b) => a.position - b.position).map((folder) => (
                <div key={folder.id} className="runon-project-folder" data-folder={folder.id}>
                  <Icon name="folder" size={16} />
                  <span className="runon-project-main">
                    <span className="truncate">{folderName(folder)}{folder.id === primary?.id && <span className="sub"> · {t("runon.project.starts")}</span>}</span>
                    <span className="sub mono truncate">{folder.path}</span>
                  </span>
                  <EnvPill env={folder.env} tiny />
                </div>
              ))}
              {onSettings && <button type="button" className="ph-btn sm runon-project-settings" onClick={() => { setOpen(false); onSettings(); }}>
                <Icon name="settings" size={14} />{t("runon.project.settings")}
              </button>}
            </div>
          ) : (
            <div className="runon-choices" role="menu" aria-label={t("runon.label")}>
              {(["container", "host"] as const).map((each) => {
                const blocked = each === "host" && !run.hostReady;
                return <SheetRow key={each} icon={each === "host" ? "lock" : "terminal"} label={t(`folder.env.${each}.long`)}
                  hint={blocked ? t("runon.host.down") : t(`runon.${each}.hint`)} checked={run.env === each} disabled={blocked}
                  data={{ runon: each }} onClick={() => choose(each)} />;
              })}
            </div>
          )}
        </Sheet>
      )}
    </>
  );
}

/** The mark of a session that runs on the host while the installation runs in a container: the host
 *  pill, nothing otherwise. Natively every session is on the host, and a mark on every row says nothing. */
export function HostMark({ env }: { env?: string }) {
  const docker = !!useEnvironments().data?.docker;
  if (!docker || env !== "host") return null;
  return <span className="host-mark"><EnvPill env="host" tiny /></span>;
}
