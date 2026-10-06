// Hiring a staff member, and changing one: the same form, because the same rules decide both. The
// name and the executor are fixed once hired — the name is the member's worktree and branch, and a
// different executor keeps a different transcript — so the edit form shows them and does not offer them.

import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Sheet } from "../ui/dialogs";
import { Icon } from "../icons";
import { confirmAsync, errorText } from "../ui";
import { t } from "../i18n";
import { HarnessBadge, StaffAvatar } from "./parts";
import {
  Catalog,
  Env,
  HARNESSES,
  HARNESS_NAMES,
  Harness,
  ISOLATIONS,
  Isolation,
  STAFF_COLOURS,
  Staff,
  Team,
  availability,
  branchPreview,
  placeExecutor,
  colourVar,
  defaultIsolation,
  foldersFor,
  modelGroups,
} from "./team";

const DAEDALUS_EFFORTS = ["", "off", "low", "medium", "high", "xhigh"];

/** The harness catalogs of both environments, asked for once per sheet. An installation without the
 *  harness manager answers 404; that, and any other failure, is "no catalog" — every command-line
 *  agent then reads as not installed, which is what the operator can act on. */
function useCatalogs(): Record<Env, Catalog | null | undefined> {
  const [catalogs, setCatalogs] = useState<Record<Env, Catalog | null | undefined>>({ container: undefined, host: undefined });
  useEffect(() => {
    let live = true;
    for (const env of ["container", "host"] as Env[]) {
      api
        .get<Catalog>(`/api/harnesses/catalog?env=${env}`)
        .then((c) => live && setCatalogs((all) => ({ ...all, [env]: c && typeof c === "object" ? c : null })))
        .catch(() => live && setCatalogs((all) => ({ ...all, [env]: null })));
    }
    return () => {
      live = false;
    };
  }, []);
  return catalogs;
}

export function StaffSheet({ team, member, onClose, onDone, toast }: { team: Team; member?: Staff; onClose: () => void; onDone: () => void; toast: (text: string) => void }) {
  const editing = !!member;
  const project = team.project;
  const [name, setName] = useState(member?.name ?? "");
  const [role, setRole] = useState(member?.role ?? "");
  const [harness, setHarness] = useState<Harness>(member?.harness ?? "daedalus");
  const [agent, setAgent] = useState(member?.agent ?? "");
  const [model, setModel] = useState(member?.model ?? "");
  const [effort, setEffort] = useState(member?.effort ?? "");
  const [permissionMode, setPermissionMode] = useState(member?.permission_mode ?? "");
  const [cliEnv, setCliEnv] = useState<Env>((member?.env || project.default_env) as Env);
  const [folderId, setFolderId] = useState(member?.default_folder_id ?? "");
  const [isolation, setIsolation] = useState<Isolation>(member?.isolation ?? defaultIsolation(foldersFor(project.folders, project.local_env)[0]));
  const [isolationTouched, setIsolationTouched] = useState(editing);
  const [instructions, setInstructions] = useState(member?.instructions ?? "");
  const [notes, setNotes] = useState(member?.notes ?? "");
  const [color, setColor] = useState(member?.color ?? "");
  const [oneOff, setOneOff] = useState(false);
  const [busy, setBusy] = useState(false);

  const daedalus = harness === "daedalus";
  // A Daedalus member works wherever its folder is, host folders included, so its environment is
  // read off the folder rather than fixed to the bot's own; a command-line member picks one.
  const chosenDaedalusFolder = project.folders.find((f) => f.id === folderId) ?? project.folders[0];
  const env: Env = daedalus ? chosenDaedalusFolder?.env ?? project.local_env : cliEnv;
  const catalogs = useCatalogs();
  const catalog = catalogs[env];
  const entry = daedalus ? undefined : catalog?.[harness];
  const folders = daedalus ? project.folders : foldersFor(project.folders, env);
  const folder = folders.find((f) => f.id === folderId) ?? folders[0];
  const reason = availability(harness, catalog ?? null);
  // No folder chosen means the project's primary one, and follows it if the operator reorders the
  // folders later. Where the primary is in the other environment, the first folder here is named outright.
  const primaryId = project.folders[0]?.id ?? "";
  const sentFolder = folderId || (folder && folder.id !== primaryId ? folder.id : "");

  // A folder of the other environment is not one this member can work in; the choice falls back to
  // the first folder that is, and the isolation follows it unless the operator already chose one.
  useEffect(() => {
    if (folderId && !folders.some((f) => f.id === folderId)) setFolderId("");
  }, [folderId, folders]);
  useEffect(() => {
    if (!isolationTouched) setIsolation(defaultIsolation(folder));
  }, [folder, isolationTouched]);

  const pickHarness = (next: Harness) => {
    if (next === harness) return;
    setHarness(next);
    if (next !== "daedalus") setCliEnv(placeExecutor(next, cliEnv, catalogs).env);
    setAgent("");
    setModel("");
    setEffort("");
    setPermissionMode("");
  };

  const agents = useMemo(() => (daedalus ? team.choices.personas : (entry?.agents ?? []).map((a) => a.name)), [daedalus, team.choices.personas, entry]);
  const groups = modelGroups(daedalus ? null : entry);
  const models = daedalus ? team.choices.presets : [...groups.offered, ...groups.others].map((m) => ({ id: m, label: m }));
  const defaultModel = team.choices.presets.find((p) => p.id === team.choices.default_preset)?.label ?? "";
  const worktreeProblem = isolation === "worktree" && folder && (folder.readonly ? t("team.isolation.readonlyfolder") : folder.env === project.local_env && !folder.is_git ? t("team.isolation.nogit") : "");
  const canSave = !busy && name.trim().length > 0 && name.trim().length <= 32 && (editing || reason === "") && !worktreeProblem && folders.length > 0;

  async function save() {
    if (!canSave) return;
    setBusy(true);
    const fields = {
      role: role.trim(),
      agent,
      model,
      effort,
      permission_mode: daedalus ? "" : permissionMode.trim(),
      env: daedalus ? "" : cliEnv === project.default_env && !member?.env ? "" : cliEnv,
      folder_id: sentFolder,
      isolation,
      instructions,
    };
    try {
      if (member) {
        const changes: Record<string, unknown> = {};
        const current: Record<string, unknown> = { role: member.role, agent: member.agent, model: member.model, effort: member.effort, permission_mode: member.permission_mode, env: member.env, folder_id: member.default_folder_id ?? "", isolation: member.isolation, instructions: member.instructions };
        for (const [key, value] of Object.entries(fields)) if (value !== current[key]) changes[key] = value;
        if (notes !== member.notes) changes.notes = notes;
        if (color && color !== member.color) changes.color = color;
        if (Object.keys(changes).length) await api.patch(`/api/staff/${encodeURIComponent(member.id)}`, changes);
        toast(t("common.saved"));
      } else {
        await api.post(`/api/projects/${encodeURIComponent(project.id)}/staff`, { name: name.trim(), harness, ...fields, folder_id: sentFolder || null, one_off: oneOff });
        toast(t("team.hired", { name: name.trim() }));
      }
      onDone();
      onClose();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function dismiss() {
    if (!member) return;
    const body = member.live ? t("team.dismiss.working") : t("team.dismiss.body");
    if (!(await confirmAsync(t("team.dismiss.title", { name: member.name }), { body, action: t("team.dismiss") }))) return;
    try {
      await api.delete(`/api/staff/${encodeURIComponent(member.id)}`);
      toast(t("team.dismissed", { name: member.name }));
      onDone();
      onClose();
    } catch (e) {
      toast(errorText(e));
    }
  }

  const title = member ? member.name : t("team.hire.title");
  return (
    <Sheet title={title} ariaLabel={member ? t("team.edit.for", { name: member.name }) : undefined} onClose={onClose} className="staff-sheet">
      {member ? (
        <div className="staff-sheet-who">
          <StaffAvatar name={member.name} color={color || member.color} />
          <HarnessBadge harness={member.harness} />
          <span className="sub">{t("team.fixed", { executor: HARNESS_NAMES[member.harness] })}</span>
        </div>
      ) : (
        <>
          <label className="field" htmlFor="staff-name">{t("team.name")}</label>
          <input id="staff-name" className="field" autoFocus maxLength={32} value={name} onChange={(e) => setName(e.target.value)} placeholder={t("team.name.placeholder")} />
        </>
      )}

      <label className="field" htmlFor="staff-role">{t("team.role")}</label>
      <input id="staff-role" className="field" maxLength={200} value={role} onChange={(e) => setRole(e.target.value)} placeholder={t("team.role.placeholder")} />

      {!member && (
        <fieldset className="executor-choice">
          <legend className="field">{t("team.executor")}</legend>
          <div className="executor-grid">
            {HARNESSES.map((h) => {
              const { why, waiting } = placeExecutor(h, cliEnv, catalogs);
              return (
                <button
                  key={h}
                  type="button"
                  className={`executor ${harness === h ? "on" : ""}`}
                  aria-pressed={harness === h}
                  disabled={waiting || why !== ""}
                  onClick={() => pickHarness(h)}
                  title={why && !waiting ? t(`team.unavailable.${why}`) : HARNESS_NAMES[h]}
                >
                  <HarnessBadge harness={h} title="" />
                  <span className="executor-name">{HARNESS_NAMES[h]}</span>
                  {why && !waiting && <span className="executor-why">{t(`team.unavailable.${why}`)}</span>}
                </button>
              );
            })}
          </div>
        </fieldset>
      )}
      {/* A CLI that updated past what its adapter was tested with may be hired, but is said to be
          unproven until a self-check has passed on the version it now is. */}
      {!daedalus && entry?.version_guard === "unverified" && (
        <div className="staff-guard" role="note" data-guard="unverified">
          {t("harness.guard", { version: entry.version ?? "", low: entry.tested_versions?.[0] ?? "", high: entry.tested_versions?.[1] ?? "" })}
        </div>
      )}

      <div className="staff-grid">
        <div>
          <label className="field" htmlFor="staff-agent">{t(daedalus ? "team.persona" : "team.agent")}</label>
          {agents.length > 0 || daedalus ? (
            <select id="staff-agent" className="field" value={agent} onChange={(e) => setAgent(e.target.value)}>
              <option value="">{t(daedalus ? "team.persona.none" : "team.agent.default")}</option>
              {agents.map((a) => <option key={a} value={a}>{a}</option>)}
              {agent && !agents.includes(agent) && <option value={agent}>{agent}</option>}
            </select>
          ) : (
            <input id="staff-agent" className="field mono" value={agent} onChange={(e) => setAgent(e.target.value)} placeholder={t("team.agent.default")} />
          )}
        </div>
        <div>
          <label className="field" htmlFor="staff-model">{t("team.model")}</label>
          {models.length > 0 || daedalus ? (
            <select id="staff-model" className="field" value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="">{daedalus && defaultModel ? t("team.model.default.named", { name: defaultModel }) : t("team.model.default")}</option>
              {groups.others.length > 0 ? (
                <>
                  <optgroup label={t("team.model.offered")}>{groups.offered.map((m) => <option key={m} value={m}>{m}</option>)}</optgroup>
                  <optgroup label={t("team.model.others")}>{groups.others.map((m) => <option key={m} value={m}>{m}</option>)}</optgroup>
                </>
              ) : (
                models.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)
              )}
              {model && !models.some((m) => m.id === model) && <option value={model}>{model}</option>}
            </select>
          ) : (
            <input id="staff-model" className="field mono" value={model} onChange={(e) => setModel(e.target.value)} placeholder={t("team.model.default")} />
          )}
        </div>
        <div>
          <label className="field" htmlFor="staff-effort">{t("team.effort")}</label>
          {daedalus ? (
            <select id="staff-effort" className="field" value={effort} onChange={(e) => setEffort(e.target.value)}>
              {DAEDALUS_EFFORTS.map((value) => <option key={value} value={value}>{value ? t(`add.effort.${value}`) : t("team.effort.default")}</option>)}
            </select>
          ) : (
            <input id="staff-effort" className="field mono" value={effort} onChange={(e) => setEffort(e.target.value)} placeholder={t("team.effort.default")} />
          )}
        </div>
        {!daedalus && (
          <div>
            <label className="field" htmlFor="staff-permissions">{t("team.permissions")}</label>
            <input id="staff-permissions" className="field mono" value={permissionMode} onChange={(e) => setPermissionMode(e.target.value)} placeholder={t("team.permissions.default")} />
          </div>
        )}
      </div>

      <label className="field">{t("team.env")}</label>
      {daedalus ? (
        <div className="sub">{t("team.env.daedalus", { env: t(`team.env.${env}`) })}</div>
      ) : (
        <div className="segmented" role="group" aria-label={t("team.env")}>
          {(["container", "host"] as Env[]).map((value) => (
            <button key={value} type="button" className={cliEnv === value ? "on" : ""} aria-pressed={cliEnv === value} onClick={() => setCliEnv(value)}>{t(`team.env.${value}`)}</button>
          ))}
        </div>
      )}

      <label className="field" htmlFor="staff-folder">{t("team.folder")}</label>
      {folders.length === 0 ? (
        <div className="sub attn">{t("team.folder.none", { env: t(`team.env.${env}`) })}</div>
      ) : (
        <select id="staff-folder" className="field" value={folder?.id ?? ""} onChange={(e) => setFolderId(e.target.value === primaryId ? "" : e.target.value)}>
          {folders.map((f) => (
            <option key={f.id} value={f.id}>{f.label ? `${f.label} · ${f.path}` : f.path}</option>
          ))}
        </select>
      )}

      <label className="field">{t("team.isolation")}</label>
      <div className="segmented" role="group" aria-label={t("team.isolation")}>
        {ISOLATIONS.map((value) => (
          <button key={value} type="button" className={isolation === value ? "on" : ""} aria-pressed={isolation === value} onClick={() => { setIsolation(value); setIsolationTouched(true); }}>{t(`team.isolation.${value}`)}</button>
        ))}
      </div>
      {worktreeProblem ? (
        <div className="sub attn">{worktreeProblem}</div>
      ) : isolation === "worktree" ? (
        <div className="sub">{t("team.isolation.branch")} <code className="branch-preview">{branchPreview(member?.name ?? name, t("team.branch.task"))}</code></div>
      ) : (
        <div className="sub">{t(`team.isolation.${isolation}.hint`)}</div>
      )}

      <label className="field" htmlFor="staff-instructions">{t("team.instructions")}</label>
      <textarea id="staff-instructions" className="field" rows={3} value={instructions} onChange={(e) => setInstructions(e.target.value)} placeholder={t("team.instructions.placeholder")} />

      {member ? (
        <>
          <label className="field" htmlFor="staff-notes">{t("team.notes")}</label>
          <textarea id="staff-notes" className="field" rows={3} value={notes} onChange={(e) => setNotes(e.target.value)} placeholder={t("team.notes.placeholder")} />
          <label className="field">{t("team.colour")}</label>
          <div className="colour-row" role="group" aria-label={t("team.colour")}>
            {STAFF_COLOURS.map((c) => (
              <button key={c} type="button" className={`colour-swatch ${(color || member.color) === c ? "on" : ""}`} style={{ ["--c" as string]: colourVar(c) }} aria-pressed={(color || member.color) === c} aria-label={t(`team.colour.${c}`)} title={t(`team.colour.${c}`)} onClick={() => setColor(c)} />
            ))}
          </div>
        </>
      ) : (
        <label className="toggle-row">
          <input type="checkbox" checked={oneOff} onChange={(e) => setOneOff(e.target.checked)} />
          <span>{t("team.oneoff")}</span>
          <span className="sub">{t("team.oneoff.hint")}</span>
        </label>
      )}

      <div className="sheet-foot">
        {member ? <button className="btn danger" onClick={dismiss}><Icon name="trash" size={15} /> {t("team.dismiss")}</button> : <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>}
        <button className="btn primary" onClick={save} disabled={!canSave}>{member ? t("common.save") : t("team.hire")}</button>
      </div>
    </Sheet>
  );
}
