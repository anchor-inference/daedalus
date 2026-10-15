// The project's member route: the runtimes a member's launch may use, in order. The host moves to the
// next one only when a runtime is refused before it ever saw the task (a spent usage window, no
// sign-in, not installed), so the editor says that rather than promising a fallback on any failure.

import { useEffect, useState } from "react";
import type { RouteEntry } from "../api";
import { Icon } from "../icons";
import { t } from "../i18n";
import { HARNESS_NAMES } from "../team/team";

const RUNTIMES: RouteEntry["runtime"][] = ["claude", "codex", "grok", "opencode", "pi", "cursor", "daedalus"];

type Write = (method: "PATCH", path: string, fields: Record<string, unknown>, label: string,
              onSuccess?: () => void) => Promise<boolean>;

/** The next runtime a new row offers: the first the route does not name yet. */
export function nextRuntime(route: RouteEntry[]): RouteEntry["runtime"] | null {
  return RUNTIMES.find((runtime) => !route.some((entry) => entry.runtime === runtime)) ?? null;
}

export function moved(route: RouteEntry[], index: number, by: -1 | 1): RouteEntry[] {
  const target = index + by;
  if (target < 0 || target >= route.length) return route;
  const next = [...route];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function ProjectRoute({ projectId, saved, write, canWrite }: {
  projectId: string;
  saved: RouteEntry[];
  write: Write;
  canWrite: boolean;
}) {
  const savedKey = JSON.stringify(saved);
  const [route, setRoute] = useState<RouteEntry[]>(saved);
  const [saving, setSaving] = useState(false);
  useEffect(() => setRoute(saved), [savedKey]);
  const dirty = JSON.stringify(route.map((e) => ({ runtime: e.runtime, model: e.model.trim() }))) !== savedKey;
  const twice = new Set(route.map((e) => e.runtime)).size !== route.length;
  const spare = nextRuntime(route);

  function change(index: number, entry: Partial<RouteEntry>) {
    setRoute((current) => current.map((e, i) => (i === index ? { ...e, ...entry } : e)));
  }

  async function save() {
    if (!dirty || twice || saving || !canWrite) return;
    setSaving(true);
    try {
      await write("PATCH", `/api/projects/${encodeURIComponent(projectId)}`,
        { member_route: route.map((e) => ({ runtime: e.runtime, model: e.model.trim() })) }, t("project.route.saved"));
    } finally { setSaving(false); }
  }

  const summary = saved.length ? saved.map((e) => HARNESS_NAMES[e.runtime]).join(" → ") : t("project.route.none");
  return <details className="project-route" data-member-route>
    <summary>{t("project.route")} <span className="sub">· {summary}</span></summary>
    <p className="sub">{t("project.route.hint")}</p>
    {route.map((entry, index) => (
      <div className="project-route-row" key={index} data-route-row={entry.runtime}>
        <span className="sub project-route-index">{index + 1}</span>
        <select className="field" aria-label={t("project.route.runtime", { n: index + 1 })} value={entry.runtime}
          onChange={(e) => change(index, { runtime: e.target.value as RouteEntry["runtime"] })}>
          {RUNTIMES.map((runtime) => <option key={runtime} value={runtime}>{HARNESS_NAMES[runtime]}</option>)}
        </select>
        <input className="field mono" aria-label={t("project.route.model", { n: index + 1 })} value={entry.model} maxLength={200}
          placeholder={t("project.route.model.default")} spellCheck={false} autoCapitalize="off" autoCorrect="off"
          onChange={(e) => change(index, { model: e.target.value })} />
        <button type="button" className="iconbtn" aria-label={t("project.route.up")} disabled={index === 0}
          onClick={() => setRoute((current) => moved(current, index, -1))}><Icon name="up" size={14} /></button>
        <button type="button" className="iconbtn" aria-label={t("project.route.down")} disabled={index === route.length - 1}
          onClick={() => setRoute((current) => moved(current, index, 1))}><Icon name="down" size={14} /></button>
        <button type="button" className="iconbtn" aria-label={t("project.route.remove")}
          onClick={() => setRoute((current) => current.filter((_, i) => i !== index))}><Icon name="trash" size={14} /></button>
      </div>
    ))}
    {twice && <div className="sub attn" role="status">{t("project.route.twice")}</div>}
    <div className="project-route-actions">
      {spare && <button type="button" className="btn ghost small" onClick={() => setRoute((current) => [...current, { runtime: spare, model: "" }])}>
        <Icon name="plus" size={14} /> {t("project.route.add")}</button>}
      <button type="button" className="btn small" onClick={() => void save()} disabled={!dirty || twice || saving || !canWrite}>{t("project.route.save")}</button>
    </div>
  </details>;
}
