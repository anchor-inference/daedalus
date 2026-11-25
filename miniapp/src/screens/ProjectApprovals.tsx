// Settings → Approvals: what a chat allowed for every chat of its project, listed per project and
// taken back one at a time. A rule like that outlives the chat that made it and answers questions in
// chats the operator is not looking at, so it has to be somewhere the operator can read them all.

import { useCallback, useEffect, useState } from "react";
import { api, type Project } from "../api";
import { Button } from "../ui/primitives";
import { Row, SetBlock, SetGroup } from "../settingsrow";
import { t } from "../i18n";
import { errorText } from "../ui";
import { shortDateTime } from "../format";
import { useQuery } from "../store";

export type ProjectApprovalRule = { id: string; tool: string; rule: string; kind: string; value: string; hosts: string[]; label: string; server: string | null; created_at: string; session_id: string; via: string };

/** Every project's standing approvals, read once per project the card can see. */
export function ProjectApprovals({ toast }: { toast: (text: string) => void }) {
  const projects = useQuery<Project[]>("/api/projects", { staleMs: 20000 });
  const own = (projects.data ?? []).filter((p) => !p.system && !p.settings.ephemeral);
  const [rules, setRules] = useState<Record<string, ProjectApprovalRule[]>>({});
  const key = own.map((p) => p.id).join(",");
  const load = useCallback(() => {
    for (const id of key ? key.split(",") : []) {
      api.get<{ rules: ProjectApprovalRule[] }>(`/api/projects/${encodeURIComponent(id)}/approval-rules`)
        .then((answer) => setRules((now) => ({ ...now, [id]: answer.rules ?? [] })))
        .catch(() => setRules((now) => ({ ...now, [id]: [] })));
    }
  }, [key]);
  useEffect(load, [load]);
  const revoke = async (project: string, rule: string) => {
    try {
      const answer = await api.delete<{ rules: ProjectApprovalRule[] }>(`/api/projects/${encodeURIComponent(project)}/approval-rules/${encodeURIComponent(rule)}`);
      setRules((now) => ({ ...now, [project]: answer.rules ?? [] }));
    } catch (e) {
      toast(errorText(e));
    }
  };
  const held = own.filter((p) => (rules[p.id] ?? []).length > 0);
  return (
    <SetGroup title={t("approvals.project.title")} help={t("approvals.project.sub")} className="project-approvals" data-setting="approvals.project">
      {held.length === 0 ? (
        <SetBlock><span className="set-hint project-approvals-none">{t("approvals.project.none")}</span></SetBlock>
      ) : (
        held.map((project) => (
          <div key={project.id} className="project-approvals-group" data-project={project.id}>
            <div className="project-approvals-name">{project.name}</div>
            {(rules[project.id] ?? []).map((rule) => (
              <Row key={rule.id} className="project-approval" data-rule={rule.id}
                title={<span className="mono">{rule.label}</span>}
                desc={t("approvals.project.since", { tool: rule.tool, when: shortDateTime(rule.created_at) })}>
                <Button size="sm" icon="close" onClick={() => void revoke(project.id, rule.id)}>{t("approvals.project.revoke")}</Button>
              </Row>
            ))}
          </div>
        ))
      )}
    </SetGroup>
  );
}
