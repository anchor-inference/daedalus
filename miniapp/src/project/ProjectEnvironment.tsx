// A project's environment: what its folders are made of, prepared once before its members start.
// The spec is replaced whole on save; Prepare is a separate press, because preparing may clone and
// run a paid setup agent, and the server never runs it again by itself after the first launch.

import { useEffect, useState } from "react";
import { api } from "../api";
import { t } from "../i18n";
import { invalidate, useOffline, useQuery } from "../store";
import { errorText } from "../ui";

type Repo = { url: string; branch: string; folder: string };
type Seed = { path: string; content: string };
type Spec = { repos: Repo[]; seeds: Seed[]; mcp: string[]; skills: string[]; goal: string; goal_minutes: number; goal_usd: number };
export type EnvironmentView = {
  project_id: string;
  spec: Spec;
  configured: boolean;
  needs_preparation: boolean;
  state: "unprepared" | "preparing" | "ready" | "failed";
  stale: boolean;
  reason: string;
  log: string;
  hold: string;
  attempt: number;
  setup_session_id: string;
  started_at: string;
  finished_at: string;
  choices: { mcp: string[]; skills: string[] };
};

export const environmentKey = (projectId: string) => `/api/projects/${encodeURIComponent(projectId)}/environment`;

const EMPTY: Spec = { repos: [], seeds: [], mcp: [], skills: [], goal: "", goal_minutes: 30, goal_usd: 2 };

/** What the summary line says about the environment, or nothing for a project that has none. */
export function environmentStatus(view: EnvironmentView | undefined): string {
  if (!view || !view.configured) return "";
  if (!view.needs_preparation) return t("env.state.nothing");
  return t(`env.state.${view.state}`) + (view.stale ? ` · ${t("env.stale")}` : "");
}

function toggle(list: string[], name: string, on: boolean): string[] {
  return on ? [...new Set([...list, name])].sort() : list.filter((item) => item !== name);
}

export function ProjectEnvironment({ projectId, toast }: { projectId: string; toast: (message: string) => void }) {
  const [open, setOpen] = useState(false);
  const query = useQuery<EnvironmentView>(environmentKey(projectId), { staleMs: 5000 });
  return <details className="project-environment" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t("env.title")}{environmentStatus(query.data) && <span className="sub"> · {environmentStatus(query.data)}</span>}</summary>
    {open && <EnvironmentContent projectId={projectId} toast={toast} />}
  </details>;
}

function EnvironmentContent({ projectId, toast }: { projectId: string; toast: (message: string) => void }) {
  const offline = useOffline();
  const key = environmentKey(projectId);
  const [polling, setPolling] = useState(false);
  const { data, error } = useQuery<EnvironmentView>(key, { staleMs: 0, pollMs: polling ? 3000 : 0 });
  const [draft, setDraft] = useState<Spec>(EMPTY);
  const [baseline, setBaseline] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const saved = data ? JSON.stringify(data.spec) : null;
  useEffect(() => { setPolling(data?.state === "preparing"); }, [data?.state]);
  useEffect(() => {
    // The draft follows the server until the operator starts editing it.
    if (saved !== null && (baseline === null || JSON.stringify(draft) === baseline)) {
      setDraft(JSON.parse(saved) as Spec);
      setBaseline(saved);
    }
  }, [saved]);
  if (error && !data) return <div className="result-warning" role="status">{errorText(error)}</div>;
  if (!data) return <div className="sub">{t("common.loading")}</div>;
  const dirty = baseline !== null && JSON.stringify(draft) !== baseline;
  const set = (patch: Partial<Spec>) => setDraft({ ...draft, ...patch });

  async function save() {
    if (!dirty || busy || offline) return;
    setBusy(true);
    try {
      const view = await api.put<EnvironmentView>(key, { spec: draft });
      setBaseline(JSON.stringify(view.spec));
      setDraft(view.spec);
      invalidate(key);
      toast(t("common.saved"));
    } catch (failure) {
      toast(errorText(failure));
    } finally { setBusy(false); }
  }

  async function prepare() {
    if (busy || offline || dirty) return;
    setBusy(true);
    try {
      await api.post(`${key}/prepare`);
      setPolling(true);
      invalidate(key);
      toast(t("env.prepare.started"));
    } catch (failure) {
      toast(errorText(failure));
    } finally { setBusy(false); }
  }

  const finished = data.finished_at ? new Date(data.finished_at).toLocaleString() : "";
  return <div className="project-environment-body">
    <p className="sub">{t("env.intro")}</p>
    {data.configured && <div className="env-status" role="status">
      <b>{environmentStatus(data)}</b>
      {finished && data.state !== "preparing" && <span className="sub"> · {t("env.finished", { when: finished })}</span>}
      {data.hold && <div className="result-warning">{data.hold}</div>}
      {data.stale && <div className="sub">{t("env.stale.hint")}</div>}
    </div>}

    <label className="field">{t("env.repos")}</label>
    <div className="sub">{t("env.repos.hint")}</div>
    {draft.repos.map((repo, index) => <div className="env-row" key={index}>
      <input className="field mono" aria-label={t("env.repo.url")} placeholder={t("env.repo.url")} value={repo.url} spellCheck={false} autoCapitalize="off"
        onChange={(event) => set({ repos: draft.repos.map((item, at) => at === index ? { ...item, url: event.target.value } : item) })} />
      <input className="field mono" aria-label={t("env.repo.branch")} placeholder={t("env.repo.branch")} value={repo.branch} spellCheck={false} autoCapitalize="off"
        onChange={(event) => set({ repos: draft.repos.map((item, at) => at === index ? { ...item, branch: event.target.value } : item) })} />
      <input className="field mono" aria-label={t("env.repo.folder")} placeholder={t("env.repo.folder")} value={repo.folder} spellCheck={false} autoCapitalize="off"
        onChange={(event) => set({ repos: draft.repos.map((item, at) => at === index ? { ...item, folder: event.target.value } : item) })} />
      <button type="button" className="btn ghost small" onClick={() => set({ repos: draft.repos.filter((_, at) => at !== index) })}>{t("common.remove")}</button>
    </div>)}
    <button type="button" className="btn ghost small" onClick={() => set({ repos: [...draft.repos, { url: "", branch: "", folder: "" }] })}>{t("env.repo.add")}</button>

    <label className="field">{t("env.seeds")}</label>
    <div className="sub">{t("env.seeds.hint")}</div>
    {draft.seeds.map((seed, index) => <div className="env-row env-seed" key={index}>
      <input className="field mono" aria-label={t("env.seed.path")} placeholder={t("env.seed.path")} value={seed.path} spellCheck={false} autoCapitalize="off"
        onChange={(event) => set({ seeds: draft.seeds.map((item, at) => at === index ? { ...item, path: event.target.value } : item) })} />
      <textarea className="field mono" aria-label={t("env.seed.content")} rows={4} value={seed.content} spellCheck={false}
        onChange={(event) => set({ seeds: draft.seeds.map((item, at) => at === index ? { ...item, content: event.target.value } : item) })} />
      <button type="button" className="btn ghost small" onClick={() => set({ seeds: draft.seeds.filter((_, at) => at !== index) })}>{t("common.remove")}</button>
    </div>)}
    <button type="button" className="btn ghost small" onClick={() => set({ seeds: [...draft.seeds, { path: "AGENTS.md", content: "" }] })}>{t("env.seed.add")}</button>

    <label className="field">{t("env.mcp")}</label>
    <div className="sub">{t("env.subset.hint")}</div>
    {data.choices.mcp.length === 0 && <div className="sub">{t("env.mcp.none")}</div>}
    {data.choices.mcp.map((name) => <label className="toggle-row" key={name}>
      <input type="checkbox" checked={draft.mcp.includes(name)} onChange={(event) => set({ mcp: toggle(draft.mcp, name, event.target.checked) })} />
      <span className="mono">{name}</span>
    </label>)}

    <label className="field">{t("env.skills")}</label>
    {data.choices.skills.length === 0 && <div className="sub">{t("env.skills.none")}</div>}
    {data.choices.skills.map((name) => <label className="toggle-row" key={name}>
      <input type="checkbox" checked={draft.skills.includes(name)} onChange={(event) => set({ skills: toggle(draft.skills, name, event.target.checked) })} />
      <span className="mono">{name}</span>
    </label>)}

    <label className="field" htmlFor={`env-goal-${projectId}`}>{t("env.goal")}</label>
    <textarea id={`env-goal-${projectId}`} className="field" rows={3} value={draft.goal} placeholder={t("env.goal.placeholder")}
      onChange={(event) => set({ goal: event.target.value })} />
    <div className="env-row">
      <label className="sub" htmlFor={`env-minutes-${projectId}`}>{t("env.goal.minutes")}</label>
      <input id={`env-minutes-${projectId}`} className="field" type="number" min={1} max={120} value={draft.goal_minutes}
        onChange={(event) => set({ goal_minutes: Number(event.target.value) })} />
      <label className="sub" htmlFor={`env-usd-${projectId}`}>{t("env.goal.usd")}</label>
      <input id={`env-usd-${projectId}`} className="field" type="number" min={0.01} max={20} step={0.5} value={draft.goal_usd}
        onChange={(event) => set({ goal_usd: Number(event.target.value) })} />
    </div>
    <div className="sub">{t("env.goal.hint")}</div>

    <div className="env-actions">
      <button type="button" className="btn small primary" disabled={!dirty || busy || offline} onClick={() => void save()}>{t("common.save")}</button>
      <button type="button" className="btn small" disabled={busy || offline || dirty || !data.needs_preparation || data.state === "preparing"}
        title={dirty ? t("env.prepare.saveFirst") : undefined} onClick={() => void prepare()}>{t("env.prepare")}</button>
    </div>
    {data.log && <details className="env-log">
      <summary>{t("env.log", { attempt: data.attempt })}</summary>
      <pre className="mono">{data.log}</pre>
      {data.setup_session_id && <div className="sub">{t("env.log.session", { id: data.setup_session_id })}</div>}
    </details>}
  </div>;
}
