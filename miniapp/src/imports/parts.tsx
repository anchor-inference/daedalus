// The pieces the desktop's explorer and the phone's screens both draw: a program's mark, a session's
// row, the preview with its options, the progress of a job and the machine not answering.

import { useState, type ReactNode } from "react";
import { relTime } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { ModelSelect, type ModelChoice } from "../modelselect";
import { useQuery } from "../store";
import type { Preset, Settings } from "../api";
import type { Failure, ImportFlow } from "./flow";
import {
  canLand, destinationLine, exchanges, harnessMeta, importedId, isLargePreview, isLargeSession, jobStages, lastWriteSeconds, markQuery, offersProject,
  previewStats, progress, sessionMeta, shortId, shortPath, snippetParts, stageLine, windowShare, type ForeignSession, type ImportPreview,
} from "./model";

/** A program's tile: its two letters on its colour. */
export function HarnessMark({ id, name, size = "md" }: { id: string; name?: string; size?: "sm" | "md" | "lg" }) {
  const meta = harnessMeta(id, name);
  return <span className={`imp-mark ${size}`} style={{ ["--c" as string]: meta.color }} aria-hidden>{meta.mark}</span>;
}

/** Text with its matching parts marked. */
export function Marked({ parts }: { parts: { text: string; hit: boolean }[] }) {
  return <>{parts.map((part, i) => part.hit ? <mark key={i}>{part.text}</mark> : <span key={i}>{part.text}</span>)}</>;
}

/** One session of another program, on two lines like a chat in the sidebar so it reads the same way. */
export function SessionRow({ s, selected, query, onSelect, onOpen }: { s: ForeignSession; selected: boolean; query?: string; onSelect: () => void; onOpen?: () => void }) {
  const live = !!s.flags?.live;
  const done = !!importedId(s);
  const meta = sessionMeta(s);
  const large = isLargeSession(s);
  return (
    <button type="button" role="option" aria-selected={selected} data-session={s.id}
      className={`imp-srow ${selected ? "sel" : ""} ${done ? "done" : ""}`}
      onClick={onSelect} onDoubleClick={onOpen} title={s.cwd}>
      <span className="imp-sic">
        <Icon name={done ? "check" : "bots"} size={12} />
        {live && <span className="imp-live" />}
      </span>
      <span className="imp-smain">
        <span className="imp-st1">{done && <span className="imp-donelbl">{t("imp.row.done")} </span>}<Marked parts={markQuery(s.title || t("imp.untitled"), query ?? "")} /></span>
        {s.snippet && <span className="imp-snip"><Marked parts={snippetParts(s.snippet, query)} /></span>}
        <span className="imp-st2">
          {meta.map((part, i) => (
            <span key={i} className={i === 0 ? "mono" : part === meta[meta.length - 1] && large ? "big" : ""}>
              {i > 0 && <span className="imp-dot" aria-hidden>·</span>}{part}
            </span>
          ))}
          {live && <span className="imp-runw"><span className="imp-dot" aria-hidden>·</span>{t("imp.row.live")}</span>}
        </span>
      </span>
      <span className="imp-stm num">{live ? t("imp.row.now") : relTime(s.updated_at)}</span>
    </button>
  );
}

/** The machine not answering, its daemon too old, or the host refusing: what happened and how to fix it. */
export function FailurePanel({ failure, onRetry }: { failure: Failure; onRetry: () => void }) {
  if (failure.code === "host_down") {
    return (
      <div className="fb-empty imp-down" role="status">
        <Icon name="offline" size={26} />
        <b>{t("imp.down.title")}</b>
        <p>
          {t("imp.down.body")}{failure.start || failure.install ? <> <code>{failure.configured === false ? failure.install : failure.start || failure.install}</code></> : null} {t("imp.down.after")}
        </p>
        <div className="fb-empty-act"><button type="button" className="btn" onClick={onRetry}><Icon name="reload" size={14} />{t("fb.retry")}</button></div>
      </div>
    );
  }
  if (failure.code === "host_outdated") {
    return (
      <div className="fb-empty imp-down" role="status">
        <Icon name="alert" size={26} />
        <b>{t("fb.host.old.title")}</b>
        <p>{t("imp.old.body")}{failure.install && <> <code>{failure.install}</code></>}</p>
        <div className="fb-empty-act"><button type="button" className="btn" onClick={onRetry}><Icon name="reload" size={14} />{t("fb.retry")}</button></div>
      </div>
    );
  }
  return (
    <div className="fb-empty imp-down" role="status">
      <Icon name="alert" size={26} />
      <b>{t("imp.refused")}</b>
      <p>{failure.message}</p>
      <div className="fb-empty-act"><button type="button" className="btn" onClick={onRetry}><Icon name="reload" size={14} />{t("fb.retry")}</button></div>
    </div>
  );
}

/** A model's label from the presets, or the value itself when it is not one. */
function useModelLabel(value: string): string {
  const settings = useQuery<Settings>("/api/settings", { staleMs: 60000 });
  const preset = (settings.data?.presets ?? {})[value] as Preset | undefined;
  if (preset) return preset.label || `${preset.provider}/${preset.model}`;
  return value || t("newagent.model.default");
}

function choiceValue(choice: ModelChoice): string {
  if ("clear" in choice) return "";
  if ("preset" in choice) return choice.preset;
  if ("provider" in choice) return `${choice.provider}/${choice.model}`;
  return choice.model;
}

/** Which model continues the session: the source's own when Daedalus has it, changeable here. */
function ModelRow({ flow, preview, phone }: { flow: ImportFlow; preview: ImportPreview; phone: boolean }) {
  const [open, setOpen] = useState(false);
  const picked = useModelLabel(flow.model);
  const label = flow.modelPicked ? picked : preview.model.label || picked;
  const source = preview.model.source;
  const name = harnessMeta(preview.header.harness, preview.harness_name).name;
  const same = preview.model.same && (!flow.modelPicked || flow.model === preview.model.preset);
  const hint = !source ? "" : same ? t("imp.model.same", { name }) : t("imp.model.other", { name, model: source });
  return (
    <div className="imp-model" data-model={flow.model}>
      <div className="imp-plbl">{t("imp.model")}</div>
      <div className="imp-model-row">
        <ModelSelect model={label} fallback={null} open={open} onOpenChange={setOpen} onChoose={(choice) => flow.setModel(choiceValue(choice))} sheet={phone} />
        {hint && <span className="imp-model-hint">{hint}</span>}
      </div>
    </div>
  );
}

/** The two ways a session comes over: whole, or a summary and the verbatim tail. */
function ModeChoice({ flow, preview }: { flow: ImportFlow; preview: ImportPreview }) {
  const large = isLargePreview(preview, flow.model);
  const share = windowShare(preview, flow.model);
  const name = harnessMeta(preview.header.harness, preview.harness_name).name;
  const compactions = preview.counts.compactions ?? 0;
  const option = (id: "full" | "tail", title: string, sub: string) => (
    <button type="button" role="radio" aria-checked={flow.mode === id} data-mode={id} className={`imp-mopt ${flow.mode === id ? "on" : ""}`} onClick={() => flow.setMode(id)}>
      <span className="imp-radio" />
      <span><b>{title}</b><span className="imp-d">{sub}</span></span>
    </button>
  );
  return (
    <div>
      <div className="imp-plbl">{t("imp.mode")}</div>
      <div className="imp-mode" role="radiogroup" aria-label={t("imp.mode")}>
        {option("full", t("imp.mode.full"), large ? t("imp.mode.full.large") : share !== null ? t("imp.mode.full.fits", { n: share }) : t("imp.mode.full.sub"))}
        {option("tail", t("imp.mode.tail"), compactions ? t("imp.mode.tail.compacted", { n: plural("imp.compactions.of", compactions), name }) : t("imp.mode.tail.sub"))}
      </div>
    </div>
  );
}

/** What the session is, how it will come over and where it lands; or the job bringing it. */
export function PreviewPane({ flow, phone = false, home, onOpenChat }: { flow: ImportFlow; phone?: boolean; home?: string; onOpenChat: (id: string) => void }) {
  const { preview, selected, previewFailure, job } = flow;
  if (!selected) return <div className="imp-pv empty"><Icon name="bots" size={22} /><span>{t("imp.pick")}</span></div>;
  if (job) return <ProgressView flow={flow} home={home} />;
  if (previewFailure) return <div className="imp-pv"><FailurePanel failure={previewFailure} onRetry={() => flow.select(null)} /></div>;
  const s = preview?.header ?? selected;
  const name = harnessMeta(s.harness, preview?.harness_name).name;
  const head = (
    <div>
      <h3>{s.title || t("imp.untitled")}</h3>
      <div className="imp-pmeta">
        {phone && <span>{name}</span>}
        <span className="mono">{shortId(s.id)}</span>
        {s.branch && <span><Icon name="fork" size={10} /> {s.branch}</span>}
        {!phone && <span className="mono">{shortPath(s.cwd, home)}</span>}
        <span>{t("imp.ago", { t: relTime(s.updated_at) })}</span>
      </div>
    </div>
  );
  if (!preview) return <div className="imp-pv" aria-busy="true">{head}<div className="imp-skel" /><div className="imp-skel short" /></div>;
  const large = isLargePreview(preview, flow.model);
  const dest = destinationLine(preview, flow.makeProject, home);
  const imported = flow.already ?? preview.imported_as?.session_id ?? importedId(s);
  const { first, last } = exchanges(preview);
  return (
    <div className="imp-pv" data-preview={s.id}>
      {head}
      <div className="imp-stats">{previewStats(preview).map((stat) => <div key={stat.label} className="imp-stat"><b className="num">{stat.value}</b><span>{stat.label}</span></div>)}</div>
      {imported && (
        <div className="imp-infobox" role="status" data-imported={imported}>
          <Icon name="check" size={14} />
          <span className="grow">{t("imp.already")}</span>
          <button type="button" className="btn small" onClick={() => onOpenChat(imported)}>{t("imp.already.open")}</button>
        </div>
      )}
      {preview.live && (
        <div className="imp-warnbox" role="alert" data-live>
          <Icon name="alert" size={14} />
          <span>{t("imp.live", { name, s: lastWriteSeconds(s) })}</span>
        </div>
      )}
      {large && !preview.live && (
        <div className="imp-warnbox" role="status" data-large>
          <Icon name="alert" size={14} />
          <span>{t("imp.large", { n: plural("imp.messages", preview.messages) })}</span>
        </div>
      )}
      {!large && first && (
        <div><div className="imp-plbl">{t("imp.first")}</div><div className="imp-q"><span className="imp-who">{t("imp.you")}{first.at ? ` · ${t("imp.ago", { t: relTime(first.at) })}` : ""}</span>{first.text}</div></div>
      )}
      {!large && !phone && last && !preview.live && (
        <div><div className="imp-plbl">{t("imp.last")}</div><div className="imp-q"><span className="imp-who">{name}{s.model ? ` · ${s.model}` : ""}</span>{last.text}</div></div>
      )}
      <ModeChoice flow={flow} preview={preview} />
      <ModelRow flow={flow} preview={preview} phone={phone} />
      <div className="grow" />
      <div>
        <div className="imp-plbl">{t("imp.dest")}</div>
        <div className={`imp-dest ${canLand(preview) ? "" : "bad"}`} data-dest={preview.destination.kind}>
          <span className="imp-dest-tile"><Icon name={!canLand(preview) ? "alert" : preview.destination.kind === "new_chat" && !flow.makeProject ? "bots" : "folder"} size={13} /></span>
          <div className="imp-dest-text"><b>{dest.title}</b><span className="imp-d">{dest.sub}</span></div>
        </div>
        {offersProject(preview) && (
          <label className="imp-check">
            <input type="checkbox" checked={flow.makeProject} onChange={(e) => flow.setMakeProject(e.target.checked)} />
            <span><b>{t("imp.makeproject")}</b><span className="imp-d">{t("imp.makeproject.sub")}</span></span>
          </label>
        )}
      </div>
      {flow.jobFailure !== null && !flow.job && !flow.already && <div className="imp-errbox" role="alert"><Icon name="alert" size={14} /><span>{flow.jobFailure || t("imp.failed")}</span></div>}
    </div>
  );
}

/** The import as named stages with their numbers, not a spinner. */
export function ProgressView({ flow, home }: { flow: ImportFlow; home?: string }) {
  const { job, preview, selected } = flow;
  if (!job) return null;
  const s = preview?.header ?? selected!;
  const destination = preview ? destinationName(preview, flow.makeProject) : "";
  const stages = jobStages(job, flow.mode);
  const pct = progress(job, flow.mode);
  const name = harnessMeta(s.harness, preview?.harness_name).name;
  return (
    <div className="imp-pv" data-job={job.job_id} data-state={job.state} data-stage={job.stage}>
      <div>
        <h3>{s.title || t("imp.untitled")}</h3>
        <div className="imp-pmeta"><span>{name}</span><span className="mono">{shortId(s.id)}</span></div>
      </div>
      <div className="imp-pbar" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={t("imp.progress")}><i style={{ width: `${pct}%` }} /></div>
      <div className="imp-prog">
        {stages.map((stage) => {
          const line = stageLine(stage, job.counts, destination);
          return (
            <div key={stage.id} className={`imp-pstep ${stage.state}`} data-stage={stage.id}>
              {stage.state === "done" ? <Icon name="check" size={14} /> : stage.state === "now" ? <span className="imp-spin" /> : stage.state === "failed" ? <Icon name="alert" size={14} /> : <Icon name="dot" size={14} />}
              <span className="grow">{line.text}</span>
              {line.aside && <span className="imp-r num">{line.aside}</span>}
            </div>
          );
        })}
      </div>
      {job.state === "failed" && (
        <div className="imp-errbox" role="alert">
          <Icon name="alert" size={14} />
          <span className="grow">{job.error?.message || flow.jobFailure || t("imp.failed")}</span>
          <button type="button" className="btn small" onClick={() => void flow.start()}>{t("common.retry")}</button>
        </div>
      )}
      <div className="grow" />
      <div className="imp-infobox"><Icon name="bulb" size={14} /><span>{t("imp.untouched", { name })}</span></div>
    </div>
  );
}

/** The footer's import button's words: a snapshot of a running session, again for an imported one. */
export function importLabel(flow: ImportFlow): string {
  if (flow.job?.state === "running" || flow.starting) return t("imp.importing");
  if (isAgain(flow)) return t("imp.again");
  if (flow.preview?.live) return t("imp.snapshot");
  return t("imp.import");
}

/** Whether the import would be a second one of a session already imported: a new chat beside the first. */
export function isAgain(flow: ImportFlow): boolean {
  return !!(flow.already || flow.preview?.imported_as || (flow.selected && importedId(flow.selected)));
}

export function canImport(flow: ImportFlow): boolean {
  return !!flow.preview && canLand(flow.preview) && !flow.starting && (!flow.job || flow.job.state === "failed") && !flow.failure;
}

/** The project or chat the progress says it opens in. */
function destinationName(p: ImportPreview, makeProject: boolean): string {
  const d = p.destination;
  if (d.project) return d.project.name;
  return makeProject || d.kind === "new_project" ? d.cwd.split(/[\\/]/).filter(Boolean).pop() ?? "" : "";
}

/** A group heading of the list: its words, and whatever sits at its end. */
export function ListHead({ children, end }: { children: ReactNode; end?: ReactNode }) {
  return <div className="imp-lsec"><span>{children}</span><span className="grow" />{end}</div>;
}
