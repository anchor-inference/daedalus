// The Harnesses screen (M7): the command-line agents of each environment, their versions against the
// latest and against the range the adapter was tested with, whether they are signed in, the agents
// and models they offer, where their status comes from, and the one button each row needs — install,
// sign in, update. A table on a desktop, a card per CLI on a phone.
//
// Every operation answers at once and finishes in the background; the rows follow the manager's
// `harness.check` and `harness.updated` events. An update the host refuses because staff are working
// on that CLI says who, on the row, so the operator knows whom to release.

import { useState } from "react";
import { api, ApiError } from "../api";
import { useEvent } from "../events";
import { relTime, relTimeLong } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader, screenTitle, useMedia } from "../shell";
import { invalidate, prime, useQuery } from "../store";
import { HarnessBadge } from "../team/parts";
import type { Harness } from "../team/team";
import { errorText } from "../ui";
import { channelWords } from "../staff/model";
import { agentSource, checkSummary, type HarnessRow, type HarnessScreen, refusedStaff, rowState, signIn, updateCount, versionMark } from "../harnesses/model";

type Env = "container" | "host";
const key = (env: Env) => `/api/harnesses?env=${env}`;

export function HarnessesScreen({ toast }: { toast: (text: string) => void }) {
  const [env, setEnv] = useState<Env>("container");
  const [open, setOpen] = useState<string | null>(null);
  const [refused, setRefused] = useState<Record<string, string[]>>({});
  const [checking, setChecking] = useState(false);
  const wide = useMedia("(min-width: 1024px)");
  // An install or an update in progress is followed closely; otherwise the events and a slow poll do.
  const [pollMs, setPollMs] = useState(60000);
  const { data, error, loading, refresh } = useQuery<HarnessScreen>(key(env), { pollMs, staleMs: 3000 });
  const wanted = data?.rows.some((r) => r.operation) ? 5000 : 60000;
  if (wanted !== pollMs) setPollMs(wanted);
  useEvent(["harness."], () => invalidate("/api/harnesses"), []);
  const rows = data?.rows ?? [];
  const updates = updateCount(rows);
  const hostReachable = !!data?.environments.includes("host");

  async function run(what: string, path: string, done: string, label: string) {
    try {
      const answer = await api.post<Record<string, unknown>>(path, { env });
      setRefused((r) => ({ ...r, [what]: [] }));
      toast(t(done, { name: label }));
      const terminal = (answer?.operation as { terminal_id?: string } | undefined)?.terminal_id ?? (answer?.terminal_id as string | undefined);
      if (what.endsWith(":signin") && terminal) navigate(pathFor("terminals", terminal));
    } catch (e) {
      const names = e instanceof ApiError && e.status === 409 ? refusedStaff(e.data) : [];
      if (names.length) setRefused((r) => ({ ...r, [what]: names }));
      else toast(errorText(e));
    } finally {
      invalidate("/api/harnesses");
    }
  }
  async function check() {
    setChecking(true);
    try {
      const answer = await api.post<Pick<HarnessScreen, "rows" | "node">>("/api/harnesses/check", { env });
      if (data) prime(key(env), { ...data, rows: answer.rows, node: answer.node });
    } catch (e) {
      toast(errorText(e));
    } finally {
      setChecking(false);
      invalidate("/api/harnesses");
    }
  }
  const act = (row: HarnessRow) => {
    const state = rowState(row);
    if (state === "install") void run(`${row.harness}:install`, `/api/harnesses/${row.harness}/install`, "harness.installing", row.label);
    if (state === "signin") void run(`${row.harness}:signin`, `/api/harnesses/${row.harness}/login-terminal`, "harness.signingin", row.label);
    if (state === "update") void run(`${row.harness}:update`, `/api/harnesses/${row.harness}/update`, "harness.updating", row.label);
  };

  const subtitle = data
    ? [plural("harness.count", rows.length), plural("harness.updates", updates), data.checked_at ? t("harness.checked", { when: relTimeLong(data.checked_at) }) : t("harness.checked.never")].join(" · ")
    : undefined;
  return (
    <>
      <PageHeader
        title={screenTitle("harnesses")}
        subtitle={subtitle}
      >
        {/* Under the title rather than beside it: a phone has no room beside a title for three controls. */}
        <div className="harness-actions">
            <div className="segmented inline harness-env" role="group" aria-label={t("harness.env")}>
              <button className={env === "container" ? "on" : ""} aria-pressed={env === "container"} data-env="container" onClick={() => setEnv("container")}>{t("term.env.container")}</button>
              {(hostReachable || env === "host") && <button className={env === "host" ? "on" : ""} aria-pressed={env === "host"} data-env="host" onClick={() => setEnv("host")}>{t("term.env.host")}</button>}
            </div>
            <button className="btn small harness-check" disabled={checking} onClick={() => void check()}>
              <Icon name="reload" size={14} />
              <span>{checking ? t("harness.checking") : t("harness.check")}</span>
            </button>
            <button className="btn small primary harness-update-all" disabled={updates === 0} onClick={() => void run("all:update", "/api/harnesses/update-all", "harness.updating.all", "")}>
              <Icon name="download" size={14} />
              <span>{t("harness.update.all")}</span>
            </button>
          </div>
      </PageHeader>
      <div className="screen wide harness-screen">
        {loading && !data && <div className="empty calm">{t("common.loading")}</div>}
        {error && !data && (
          <div className="empty">
            <b>{t("harness.error")}</b>
            <div>{error}</div>
            <button className="btn primary" onClick={() => void refresh()}>{t("common.retry")}</button>
          </div>
        )}
        {refused["all:update"]?.length ? <Refused names={refused["all:update"]} /> : null}
        {data && wide && (
          <table className="harness-table">
            <thead>
              <tr>
                <th>{t("harness.col.harness")}</th>
                <th>{t("harness.col.version")}</th>
                <th>{t("harness.col.latest")}</th>
                <th>{t("harness.col.signin")}</th>
                <th>{t("harness.col.agents")}</th>
                <th>{t("harness.col.models")}</th>
                <th>{t("harness.col.channel")}</th>
                {/* A named column: the bare one read as an orphan next to seven labelled ones. */}
                <th className="harness-action">{t("harness.col.state")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <TableRow key={row.harness} row={row} open={open === row.harness} onToggle={() => setOpen((o) => (o === row.harness ? null : row.harness))} onAct={() => act(row)} refused={refused[`${row.harness}:update`] ?? []} />
              ))}
            </tbody>
          </table>
        )}
        {data && !wide && (
          <div className="harness-cards">
            {rows.map((row) => <Card key={row.harness} row={row} open={open === row.harness} onToggle={() => setOpen((o) => (o === row.harness ? null : row.harness))} onAct={() => act(row)} refused={refused[`${row.harness}:update`] ?? []} />)}
          </div>
        )}
        {data && env === "container" && (
          <div className="harness-node" data-installed={data.node.installed}>
            <span className="harness-node-name">Node</span>
            <span className="mono">{data.node.installed ? data.node.version : "—"}</span>
            <span className="sub">{t("harness.node.pinned", { version: data.node.pinned })}</span>
            {!data.node.installed && <button className="btn small" onClick={() => void run("node:install", "/api/harnesses/node/install", "harness.installing", "Node")}>{t("harness.node.install")}</button>}
          </div>
        )}
        {data && (
          <p className="harness-foot sub">
            <Icon name="alert" size={14} />
            <span>{t("harness.footnote")}</span>
          </p>
        )}
      </div>
    </>
  );
}

function Refused({ names }: { names: string[] }) {
  return (
    <div className="harness-refused" role="alert">
      <Icon name="lock" size={14} />
      <span>{t("harness.refused", { names: names.join(", ") })}</span>
    </div>
  );
}

function Version({ row }: { row: HarnessRow }) {
  const mark = versionMark(row);
  return (
    <span className="harness-version">
      <span className="mono">{row.installed ? row.installed_version || "?" : "—"}</span>
      {mark && <span className={`harness-mark ${mark}`} data-mark={mark} title={t("harness.tested", { low: row.tested_versions[0], high: row.tested_versions[1] })}>{t(`harness.mark.${mark}`)}</span>}
    </span>
  );
}

function Latest({ row }: { row: HarnessRow }) {
  return <span className={`mono ${row.update_available ? "harness-newer" : "sub"}`}>{row.latest_version || "—"}</span>;
}

function SignIn({ row }: { row: HarnessRow }) {
  const s = signIn(row);
  return <span className={`pill harness-signin ${s.tone}`} data-signin={row.logged_in}>{t(s.key, { detail: s.detail })}</span>;
}

function Models({ row }: { row: HarnessRow }) {
  if (!row.models.length) return <span className="sub">—</span>;
  const shown = row.models.slice(0, 4).join(" · ");
  return <span className="harness-models" title={row.models.join(", ")}>{shown}{row.models.length > 4 ? ` ${t("harness.more", { n: row.models.length - 4 })}` : ""}</span>;
}

function Action({ row, onAct }: { row: HarnessRow; onAct: () => void }) {
  const state = rowState(row);
  if (state === "busy") {
    const terminal = row.operation?.terminal_id;
    return (
      <span className="harness-busy" data-state="busy">
        <span className="spinner" aria-hidden />
        <span>{t(`harness.op.${row.operation!.kind}`)}</span>
        {terminal && <button className="btn small ghost" onClick={() => navigate(pathFor("terminals", terminal))}>{t("harness.op.watch")}</button>}
      </span>
    );
  }
  if (state === "current") return <span className="sub harness-current" data-state="current">{t("harness.state.current")}</span>;
  if (state === "blocked") return <span className="sub harness-blocked" data-state="blocked" title={row.install_problem || row.error || undefined}>{row.install_problem ? t("harness.state.needsnode") : t("harness.state.absent")}</span>;
  return (
    <button className={`btn small ${state === "update" ? "accent" : ""}`} data-state={state} onClick={onAct}>
      {t(`harness.state.${state}`)}
    </button>
  );
}

/** What unfolds under a row: the agents with where each is defined, the last self-check step by step,
 *  and why staff cannot run on it, when they cannot. */
function Details({ row }: { row: HarnessRow }) {
  const summary = checkSummary(row.self_check);
  const steps = "steps" in row.self_check && Array.isArray(row.self_check.steps) ? row.self_check.steps : [];
  return (
    <div className="harness-details">
      {row.version_guard === "unverified" && <div className="harness-guard" role="note">{t("harness.guard", { version: row.installed_version, low: row.tested_versions[0], high: row.tested_versions[1] })}</div>}
      {row.unavailable && row.installed && <div className="harness-unavailable sub">{row.unavailable}</div>}
      <div className="harness-details-label">{t("harness.agents")}</div>
      {row.agents.length === 0 ? (
        <div className="sub">{t("harness.agents.none")}</div>
      ) : (
        <ul className="harness-agents">
          {row.agents.map((a) => (
            <li key={`${a.source}:${a.name}`} className="harness-agent">
              <span className="mono">{a.name}</span>
              <span className="sub">{t(agentSource(a.source))}</span>
            </li>
          ))}
        </ul>
      )}
      <div className="harness-details-label">{t("harness.selfcheck")}</div>
      <div className={`harness-check-line ${summary.key === "harness.check.failed" ? "failed" : ""}`}>
        {t(summary.key, { step: summary.step, n: summary.skipped })}
        {"at" in row.self_check && row.self_check.at ? <span className="sub"> · {relTime(row.self_check.at)}</span> : null}
      </div>
      {steps.length > 0 && (
        <ol className="harness-steps">
          {steps.map((s) => (
            <li key={s.name} className={`harness-step ${s.skipped ? "skipped" : s.ok ? "ok" : "failed"}`} data-step={s.name}>
              <Icon name={s.skipped ? "dot" : s.ok ? "check" : "close"} size={12} />
              <span className="mono">{s.name}</span>
              {s.detail && <span className="sub truncate">{s.detail}</span>}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

type RowProps = { row: HarnessRow; open: boolean; onToggle: () => void; onAct: () => void; refused: string[] };

function TableRow({ row, open, onToggle, onAct, refused }: RowProps) {
  return (
    <>
      <tr className={`harness-row ${open ? "open" : ""}`} data-harness={row.harness} data-state={rowState(row)}>
        <td>
          <button className="harness-name" onClick={onToggle} aria-expanded={open}>
            <HarnessBadge harness={row.harness as Harness} />
            <b>{row.label}</b>
            <Icon name="chevron" size={14} />
          </button>
        </td>
        <td><Version row={row} /></td>
        <td><Latest row={row} /></td>
        <td><SignIn row={row} /></td>
        <td>{row.installed ? row.agents.length : "—"}</td>
        <td><Models row={row} /></td>
        <td className="sub">{channelWords(row)}</td>
        <td className="harness-action"><Action row={row} onAct={onAct} /></td>
      </tr>
      {refused.length > 0 && (
        <tr className="harness-row-refused"><td colSpan={8}><Refused names={refused} /></td></tr>
      )}
      {open && (
        <tr className="harness-row-details"><td colSpan={8}><Details row={row} /></td></tr>
      )}
    </>
  );
}

function Card({ row, open, onToggle, onAct, refused }: RowProps) {
  return (
    <article className="harness-card" data-harness={row.harness} data-state={rowState(row)}>
      <div className="harness-card-head">
        <HarnessBadge harness={row.harness as Harness} />
        <b className="grow">{row.label}</b>
        <Action row={row} onAct={onAct} />
      </div>
      <dl className="harness-card-facts">
        <dt>{t("harness.col.version")}</dt><dd><Version row={row} /> <span className="sub">→</span> <Latest row={row} /></dd>
        <dt>{t("harness.col.signin")}</dt><dd><SignIn row={row} /></dd>
        <dt>{t("harness.col.models")}</dt><dd><Models row={row} /></dd>
        <dt>{t("harness.col.channel")}</dt><dd>{channelWords(row)}</dd>
      </dl>
      {refused.length > 0 && <Refused names={refused} />}
      <button className="harness-card-more" onClick={onToggle} aria-expanded={open}>
        {plural("harness.agents.count", row.agents.length)}
        <Icon name="chevron" size={14} />
      </button>
      {open && <Details row={row} />}
    </article>
  );
}
