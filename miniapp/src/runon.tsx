// Where a new chat runs, for a Docker installation: in the container, as every chat always has, or on
// the host through its terminal daemon. And the mark a session that runs on the host carries.

import { useRef, useState } from "react";
import { EnvPill } from "./envpill";
import { Icon } from "./icons";
import { useEnvironments } from "./projects";
import { Popover } from "./ui/dialogs";
import { SheetRow } from "./ui/phone";
import { t } from "./i18n";

export type RunEnv = "container" | "host";

export type RunOn = {
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
  return { offered, hostReady, env, pick: setPicked, body: env === "host" ? { env: "host" } : {} };
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

/** The phone's + sheet row: on the host or not, with the reason when the daemon is down. */
export function RunOnRow({ run, onDone }: { run: RunOn; onDone: () => void }) {
  if (!run.offered) return null;
  const host = run.env === "host";
  return <SheetRow icon="lock" label={t("runon.host.row")} hint={run.hostReady ? t("runon.host.hint") : t("runon.host.down")}
    checked={host} disabled={!run.hostReady} data={{ runon: "host" }} onClick={() => { run.pick(host ? "container" : "host"); onDone(); }} />;
}

/** The mark of a session that runs on the host while the installation runs in a container: the host
 *  pill, nothing otherwise. Natively every session is on the host, and a mark on every row says nothing. */
export function HostMark({ env }: { env?: string }) {
  const docker = !!useEnvironments().data?.docker;
  if (!docker || env !== "host") return null;
  return <span className="host-mark"><EnvPill env="host" tiny /></span>;
}
