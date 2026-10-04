import { useEffect, useState, type ReactNode } from "react";
import { api, ApiError, type Project } from "../api";
import { t } from "../i18n";
import { ORCHESTRATION_LIST } from "../router";
import { PageHeader } from "../shell";
import { invalidate, useOffline, useQuery } from "../store";
import { errorText } from "../ui";
import { Skeleton } from "../ui/components";

type Goal = { project_id: string; goal_revision: number; entity_revision: number; body: string; checks: string[] | null };
type Draft = { body: string; checks: string };
type Intent = { client_operation_id: string; expected_entity_revision: number; expected_goal_revision: number;
  body: string; checks: string[]; root_task_ids: string[] };

const key = (id: string) => `/api/projects/${encodeURIComponent(id)}/scope-revisions/current`;
const draftKey = (id: string) => `daedalus.project.goal.draft.${id}`;
const pendingKey = (id: string) => `daedalus.project.goal.pending.${id}`;

function read<T>(key: string): T | null {
  try { return JSON.parse(sessionStorage.getItem(key) ?? "null") as T | null; } catch { return null; }
}
function keep(key: string, value: unknown): void {
  try { if (value === null) sessionStorage.removeItem(key); else sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* The mounted draft remains available. */ }
}

/** The first goal uses the same revisioned project command as later goal changes. */
export function GuidedGoal({ project, toast, wide, ready, embedded = false, onSaved }: { project: Project; toast: (text: string) => void; wide: boolean; ready: ReactNode; embedded?: boolean; onSaved?: () => void }) {
  const endpoint = key(project.id);
  const { data, error, refresh } = useQuery<Goal>(endpoint, { staleMs: 5000 });
  const [initial] = useState(() => read<Draft>(draftKey(project.id)));
  const [body, setBody] = useState(initial?.body ?? "");
  const [checks, setChecks] = useState(initial?.checks ?? "");
  const [pending, setPending] = useState<Intent | null>(() => read<Intent>(pendingKey(project.id)));
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const offline = useOffline();
  const changedGoal = !!data?.body && (!!body.trim() || !!checks.trim());
  useEffect(() => keep(draftKey(project.id), { body, checks }), [project.id, body, checks]);

  async function send(intent: Intent) {
    if (busy || offline) return;
    setBusy(true);
    try {
      const receipt = await api.post<{ project_id: string; goal_revision: number; receipt_id: string }>(
        `/api/projects/${encodeURIComponent(project.id)}/scope-revisions`, intent);
      if (receipt.project_id !== project.id || !receipt.receipt_id || receipt.goal_revision <= intent.expected_goal_revision)
        throw new Error(t("goal.start.unconfirmed"));
      const current = await api.get<Goal>(endpoint);
      if (current.project_id !== project.id || current.goal_revision !== receipt.goal_revision ||
          current.body !== intent.body || current.checks?.join("\n") !== intent.checks.join("\n"))
        throw new Error(t("goal.start.unconfirmed"));
      keep(pendingKey(project.id), null);
      keep(draftKey(project.id), null);
      setPending(null);
      setBody("");
      setChecks("");
      invalidate(endpoint);
      invalidate(`/api/projects/${encodeURIComponent(project.id)}/brief`);
      invalidate("/api/projects");
      toast(t("goal.start.saved"));
      onSaved?.();
    } catch (failure) {
      if (failure instanceof ApiError && failure.status === 409 &&
          !failure.message.includes("command identity was reused with a different request")) {
        keep(pendingKey(project.id), null);
        setPending(null);
        setConflict(true);
      }
      toast(errorText(failure));
    } finally { setBusy(false); }
  }

  function save() {
    if (!data || pending || conflict || changedGoal || busy || offline) return;
    const criteria = checks.split("\n").map((line) => line.trim()).filter(Boolean);
    if (!body.trim() || !criteria.length || criteria.length > 12) return;
    const intent: Intent = { client_operation_id: crypto.randomUUID(), expected_entity_revision: data.entity_revision,
      expected_goal_revision: data.goal_revision, body: body.trim(), checks: criteria, root_task_ids: [] };
    keep(pendingKey(project.id), intent);
    setPending(intent);
    void send(intent);
  }

  async function review() {
    setBusy(true);
    try { await api.get<Goal>(endpoint); invalidate(endpoint); setConflict(false); }
    catch (failure) { toast(errorText(failure)); }
    finally { setBusy(false); }
  }

  if (data?.body && !body.trim() && !checks.trim() && !pending && !conflict)
    return <>{ready}</>;
  const criteria = checks.split("\n").map((line) => line.trim()).filter(Boolean);
  return <>
    {!embedded && <PageHeader title={project.name} back={wide ? undefined : ORCHESTRATION_LIST} />}
    <div className={embedded ? "guided-goal" : "screen narrow guided-goal"}>
      {!embedded && <h2>{t("goal.start.title")}</h2>}
      <p className="sub">{t("goal.start.intro")}</p>
      {!data && !error && <Skeleton rows={3} />}
      {error && !data && <div className="empty"><div>{t("goal.start.readFailed")}</div><button className="btn primary" onClick={() => void refresh()}>{t("common.retry")}</button></div>}
      {data && <>
        <label className="field" htmlFor="guided-goal-body">{t("project.start.goal")}</label>
        <textarea id="guided-goal-body" className="field" rows={4} maxLength={4000} value={body} onChange={(event) => setBody(event.target.value)} />
        <label className="field" htmlFor="guided-goal-checks">{t("project.start.checks")}</label>
        <textarea id="guided-goal-checks" className="field" rows={4} value={checks} onChange={(event) => setChecks(event.target.value)} placeholder={t("project.start.checksHint")} />
        {criteria.length > 12 && <p className="sub attn" role="status">{t("project.start.tooMany")}</p>}
        <p className="sub">{t("goal.start.noRun")}</p>
        <p className="sub">{t("goal.start.cost")}</p>
        {offline && <p className="sub attn" role="status">{t("goal.start.offline")}</p>}
        {changedGoal && !conflict && <p className="sub attn" role="status">{t("goal.start.alreadySet")}
          <button className="linkbtn" onClick={() => { setBody(""); setChecks(""); keep(draftKey(project.id), null); }}>{t("goal.start.openCurrent")}</button></p>}
        {pending && <p className="sub attn" role="status">{t("goal.start.unconfirmed")} <button className="linkbtn" disabled={busy || offline} onClick={() => void send(pending)}>{t("common.retry")}</button></p>}
        {conflict && <p className="sub attn" role="status">{t("goal.start.conflict")} <button className="linkbtn" disabled={busy || offline} onClick={() => void review()}>{t("goal.start.review")}</button></p>}
        <button className="btn primary" disabled={!body.trim() || !criteria.length || criteria.length > 12 || busy || offline || !!pending || conflict || changedGoal} onClick={save}>{t("common.save")}</button>
      </>}
    </div>
  </>;
}
