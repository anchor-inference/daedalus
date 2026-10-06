// Ready-made teams for an empty team page: one agent, a worker and a reviewer, or the coordinator with
// one worker. Each is a plan shown in full and confirmed before any request is sent.

import { useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { invalidate } from "../store";
import { errorText } from "../ui";
import { confirmDialog } from "../ui/dialogs";
import { SETUPS, SetupKind, SetupPlan, Team, setupPlan } from "./team";

/** The plan of a ready-made setup as the confirmation shows it: one line per thing it will create. */
function SetupSteps({ plan }: { plan: SetupPlan }) {
  return (
    <ul className="setup-steps" data-setup-steps>
      {plan.enable && <li>{t("team.setup.step.enable")}</li>}
      {plan.hires.map((hire) => (
        <li key={hire.name}>{t("team.setup.step.hire", { name: hire.name, where: t(`team.isolation.${hire.isolation}.short`) })}{hire.role ? ` — ${hire.role}` : ""}</li>
      ))}
    </ul>
  );
}

/** Three ready-made teams for an empty page, each one click and one confirmation away. Nothing is sent
 *  before the operator confirms the plan; the requests are the ordinary hire and coordinator ones, so
 *  a refusal reads exactly as it would from the hiring form. */
export function Setups({ team, toast, onDone }: { team: Team; toast: (text: string) => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const project = encodeURIComponent(team.project.id);
  async function run(kind: SetupKind) {
    const plan = setupPlan(kind, team.project, { worker: t("team.setup.name.worker"), reviewer: t("team.setup.name.reviewer"), workerRole: t("team.setup.role.worker"), reviewerRole: t("team.setup.role.reviewer") });
    const ok = await confirmDialog({ title: t("team.setup.confirm", { name: t(`team.setup.${kind}`) }), body: <SetupSteps plan={plan} />, action: t("team.setup.create"), danger: false });
    if (!ok) return;
    setBusy(true);
    try {
      // The coordinator first: a project whose coordinator cannot start is told so before anyone is
      // hired for it to hand work to.
      if (plan.enable) {
        await api.post(`/api/projects/${project}/orchestrator`, {});
        invalidate("/api/projects");
        invalidate("/api/sessions");
      }
      for (const hire of plan.hires) await api.post(`/api/projects/${project}/staff`, { ...hire, folder_id: null });
      toast(t("team.setup.done", { name: t(`team.setup.${kind}`) }));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
      onDone();
    }
  }
  return (
    <div className="team-setups" data-team-setups>
      <div className="sub">{t("team.setup.title")}</div>
      {SETUPS.map((kind) => (
        <button key={kind} className="team-setup" data-setup={kind} disabled={busy} onClick={() => void run(kind)}>
          <b>{t(`team.setup.${kind}`)}</b>
          <span className="sub">{t(`team.setup.${kind}.sub`)}</span>
        </button>
      ))}
    </div>
  );
}
