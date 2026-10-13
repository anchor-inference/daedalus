// The step editor: a finished recording, step by step, for the operator to make into a skill the agent
// replays (BrowserReplay). They delete the steps that were noise, turn what they typed into parameters
// (a typed date becomes {month}), say how each sign-in is done, and name the skill.
//
// A sign-in's fields never take a value here, only an operator secret's placeholder («secret:name»):
// whatever else were written would be a password kept in a file.

import { useEffect, useMemo, useState } from "react";
import { api, type BrowserStep, type BrowserWorkflow } from "../api";
import { t } from "../i18n";
import { Icon } from "../icons";
import { errorText, haptic } from "../ui";
import { saveWorkflowSkill } from "./data";
import { blocker, editableSteps, initialRow, isSecretRef, paramsOf, skillRequest, skillSlug, type SignIn, type SkillRow } from "./skilldraft";
import { StepText } from "./steps";

type SecretsList = { secrets: { name: string }[] };

/** The operator's secret names, for the sign-in fields to offer: best effort, the field takes any name. */
function useSecretNames(sessionId: string | null): string[] {
  const [names, setNames] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    api.get<SecretsList>(sessionId ? `/api/secrets?session_id=${encodeURIComponent(sessionId)}` : "/api/secrets")
      .then((list) => { if (live) setNames((list.secrets ?? []).map((s) => s.name)); })
      .catch(() => { if (live) setNames([]); });
    return () => { live = false; };
  }, [sessionId]);
  return names;
}

export function SkillEditor({ group, recording, toast, onDone }: { group: string; recording: BrowserWorkflow; toast: (text: string) => void; onDone: () => void }) {
  const steps = useMemo(() => editableSteps(recording.steps), [recording.steps]);
  const [rows, setRows] = useState<SkillRow[]>(() => steps.map(initialRow));
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const secrets = useSecretNames(recording.session_id);
  const params = paramsOf(rows, steps);
  const why = blocker(rows, steps, name, description);
  const listId = `bp-skill-secrets-${recording.id}`;
  const change = (n: number, patch: Partial<SkillRow>) => setRows((all) => all.map((r) => (r.n === n ? { ...r, ...patch } : r)));
  const save = async () => {
    setBusy(true);
    try {
      const saved = await saveWorkflowSkill(group, recording.id, skillRequest(rows, steps, name, description));
      haptic("medium");
      toast(t("browser.skill.saved", { name: saved.skill }));
      onDone();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="bp-skill" data-workflow={recording.id}>
      <div className="bp-rec-head">
        <Icon name="skill" size={14} />
        <b>{t("browser.skill.head")}</b>
      </div>
      <p className="sub bp-skill-lead">{t("browser.skill.lead")}</p>
      <label className="bp-proc-label" htmlFor={`bp-skill-name-${recording.id}`}>{t("browser.skill.name")}</label>
      <input id={`bp-skill-name-${recording.id}`} className="bp-proc-title bp-skill-name" value={name} maxLength={48} placeholder={t("browser.skill.name.placeholder")} onChange={(e) => setName(e.target.value)} />
      {name && skillSlug(name) !== name && <span className="sub bp-skill-slug mono">{skillSlug(name)}</span>}
      <label className="bp-proc-label" htmlFor={`bp-skill-desc-${recording.id}`}>{t("browser.skill.description")}</label>
      <input id={`bp-skill-desc-${recording.id}`} className="bp-proc-title bp-skill-desc" value={description} maxLength={320} placeholder={t("browser.skill.description.placeholder")} onChange={(e) => setDescription(e.target.value)} />
      <ol className="bp-skill-steps">
        {steps.map((step) => {
          const row = rows.find((r) => r.n === step.n)!;
          return <SkillStepRow key={step.n} step={step} row={row} listId={listId} onChange={(patch) => change(step.n, patch)} />;
        })}
      </ol>
      <datalist id={listId}>{secrets.map((s) => <option key={s} value={`«secret:${s}»`} />)}</datalist>
      <div className="bp-skill-params" aria-live="polite">
        {params.length ? (
          <>
            <span className="sub">{t("browser.skill.params")}</span>
            {params.map((p) => <code key={p} className="bp-skill-param">{`{${p}}`}</code>)}
          </>
        ) : <span className="sub">{t("browser.skill.params.none")}</span>}
      </div>
      <div className="bp-proc-foot">
        <button type="button" className="btn small primary bp-skill-save" disabled={busy || !!why} title={why ? t(why) : undefined} onClick={() => void save()}>{t(busy ? "browser.skill.saving" : "browser.skill.save")}</button>
        <button type="button" className="btn small" disabled={busy} onClick={onDone}>{t("bs.notes.cancel")}</button>
        {why && <span className="sub bp-skill-why">{t(why)}</span>}
      </div>
    </div>
  );
}

function SkillStepRow({ step, row, listId, onChange }: { step: BrowserStep; row: SkillRow; listId: string; onChange: (patch: Partial<SkillRow>) => void }) {
  const quiet = step.action === "arrive" || step.action === "scroll";
  return (
    <li className={`bp-skill-step${row.keep ? "" : " gone"}${quiet ? " quiet" : ""}`} data-action={step.action} data-n={step.n}>
      <div className="bp-skill-step-head">
        {step.action === "handoff" && <Icon name="lock" size={12} />}
        <span className="truncate bp-skill-step-words"><StepText step={step} /></span>
        <button type="button" className="iconbtn bp-skill-drop" onClick={() => onChange({ keep: !row.keep })}
          aria-label={t(row.keep ? "browser.skill.drop" : "browser.skill.keep")} title={t(row.keep ? "browser.skill.drop" : "browser.skill.keep")}>
          <Icon name={row.keep ? "trash" : "undo"} size={13} />
        </button>
      </div>
      {row.keep && step.action === "type" && (
        <div className="bp-skill-field">
          <input className="bp-skill-input mono" value={row.text} maxLength={2000} aria-label={t("browser.skill.typed")} onChange={(e) => onChange({ text: e.target.value })} />
          {!/\{[a-z]/.test(row.text) && (
            <button type="button" className="btn small bp-skill-param-btn" onClick={() => onChange({ text: `{${paramName(step)}}` })}>{t("browser.skill.make.param")}</button>
          )}
        </div>
      )}
      {row.keep && step.action === "select" && (
        <div className="bp-skill-field">
          <input className="bp-skill-input mono" value={row.option} maxLength={300} aria-label={t("browser.skill.option")} onChange={(e) => onChange({ option: e.target.value })} />
        </div>
      )}
      {row.keep && step.action === "navigate" && !!step.to && (
        <div className="bp-skill-field">
          <input className="bp-skill-input mono" value={row.to} maxLength={2000} aria-label={t("browser.skill.address")} onChange={(e) => onChange({ to: e.target.value })} />
        </div>
      )}
      {row.keep && step.action === "handoff" && (
        <div className="bp-skill-signin">
          <select className="bp-skill-mode" value={row.signin} aria-label={t("browser.skill.signin")} onChange={(e) => onChange({ signin: e.target.value as SignIn })}>
            <option value="operator">{t("browser.skill.signin.operator")}</option>
            {(step.reason ?? "login") === "login" && <option value="saved">{t("browser.skill.signin.saved")}</option>}
            {!!step.fields?.length && <option value="secrets">{t("browser.skill.signin.secrets")}</option>}
          </select>
          {row.signin === "secrets" && step.fields?.map((field, i) => (
            <label key={i} className="bp-skill-secret">
              <span className="sub truncate">{field.name || field.role}</span>
              <input className={`bp-skill-input mono${row.secrets[i] && !isSecretRef(row.secrets[i]) ? " bad" : ""}`} list={listId} value={row.secrets[i] ?? ""} placeholder="«secret:name»"
                onChange={(e) => onChange({ secrets: row.secrets.map((s, j) => (j === i ? e.target.value : s)) })} />
            </label>
          ))}
        </div>
      )}
    </li>
  );
}

/** A parameter's name from the field it is typed into: its recorded blank, else its name in words. */
function paramName(step: BrowserStep): string {
  const base = (step.slot || step.element?.name || "text").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return /^[a-z]/.test(base) ? base.slice(0, 40) : "text";
}
