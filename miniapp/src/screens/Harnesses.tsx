// The Harnesses screen (M7): the command-line agents of each environment, their versions against the
// latest and against the range the adapter was tested with, whether they are signed in, the agents
// and models they offer, where their status comes from, and the one button each row needs — install,
// sign in, update. A list of rows with the open one beside it on a desktop; on a phone the open one
// is a sheet.
//
// Every operation answers at once and finishes in the background; the rows follow the manager's
// `harness.check` and `harness.updated` events. An update the host refuses because staff are working
// on that CLI says who, on the row, so the operator knows whom to release.
//
// A row's details also hold which of its models are offered: the operator ticks the few the hiring
// form and the orchestrator should present, by model and version, and every other one stays usable.

import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { useEvent } from "../events";
import { relTime, relTimeLong } from "../format";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { navigate, pathFor } from "../router";
import { PageHeader, SelectionList, screenTitle, useMedia } from "../ui/index";
import { Button, Dot, IconButton, Segmented, Sheet } from "../ui/primitives";
import { invalidate, prime, useQuery } from "../store";
import { HarnessBadge } from "../team/parts";
import type { Harness } from "../team/team";
import { errorText } from "../ui";
import { channelWords } from "../staff/model";
import { agentSource, checkSummary, type HarnessRow, type HarnessScreen, offeredDraft, offeredToSend, refusedStaff, rowState, signIn, updateCount, versionMark } from "../harnesses/model";

type Env = "container" | "host";
const key = (env: Env) => `/api/harnesses?env=${env}`;

export function HarnessesScreen({ toast }: { toast: (text: string) => void }) {
  const [env, setEnv] = useState<Env>("container");
  const [picked, setPicked] = useState<string | null>(null);
  const [refused, setRefused] = useState<Record<string, string[]>>({});
  const [checking, setChecking] = useState(false);
  const split = useMedia("(min-width: 1280px)");
  const phone = !useMedia("(min-width: 1024px)");
  // An install or an update in progress is followed closely; otherwise the events and a slow poll do.
  const [pollMs, setPollMs] = useState(60000);
  const { data, error, loading, refresh } = useQuery<HarnessScreen>(key(env), { pollMs, staleMs: 3000 });
  const wanted = data?.rows.some((r) => r.operation) ? 5000 : 60000;
  if (wanted !== pollMs) setPollMs(wanted);
  useEvent(["harness."], () => invalidate("/api/harnesses"), []);
  const rows = data?.rows ?? [];
  const updates = updateCount(rows);
  const hostReachable = !!data?.environments.includes("host");

  async function run(what: string, path: string) {
    try {
      const answer = await api.post<Record<string, unknown>>(path, { env });
      setRefused((r) => ({ ...r, [what]: [] }));
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
  async function offer(row: HarnessRow, models: string[] | null) {
    try {
      const answer = await api.put<{ row: HarnessRow }>(`/api/harnesses/${row.harness}/models`, { env, models });
      if (data && answer?.row) prime(key(env), { ...data, rows: data.rows.map((r) => (r.harness === row.harness ? answer.row : r)) });
    } catch (e) {
      toast(errorText(e));
    } finally {
      invalidate("/api/harnesses");
    }
  }
  const act = (row: HarnessRow) => {
    const state = rowState(row);
    if (state === "install") void run(`${row.harness}:install`, `/api/harnesses/${row.harness}/install`);
    if (state === "signin") void run(`${row.harness}:signin`, `/api/harnesses/${row.harness}/login-terminal`);
    if (state === "update") void run(`${row.harness}:update`, `/api/harnesses/${row.harness}/update`);
  };

  const current = rows.find((r) => r.harness === picked) ?? (split ? rows[0] : undefined);
  const pane = current && <HarnessPane key={current.harness} row={current} onAct={() => act(current)} onOffer={(models) => offer(current, models)} refused={refused[`${current.harness}:update`] ?? []} />;
  const subtitle = data
    ? [plural("harness.count", rows.length), plural("harness.updates", updates), data.checked_at ? t("harness.checked", { when: relTimeLong(data.checked_at) }) : t("harness.checked.never")].join(" · ")
    : undefined;
  const envOptions: { id: Env; label: string }[] = [{ id: "container", label: t("term.env.container") }, ...(hostReachable || env === "host" ? [{ id: "host" as Env, label: t("term.env.host") }] : [])];
  return (
    <>
      <PageHeader
        title={screenTitle("harnesses")}
        subtitle={subtitle}
        ruled
        filters={<Segmented className="harness-env" label={t("harness.env")} value={env} options={envOptions} onChange={(id) => { setEnv(id); setPicked(null); }} />}
        actions={
          phone ? (
            <>
              <IconButton className="harness-check" icon="reload" label={checking ? t("harness.checking") : t("harness.check")} disabled={checking} onClick={() => void check()} />
              <IconButton className="harness-update-all" icon="download" label={t("harness.update.all")} disabled={updates === 0} onClick={() => void run("all:update", "/api/harnesses/update-all")} />
            </>
          ) : (
            <>
              <Button className="harness-check" icon="reload" disabled={checking} onClick={() => void check()}>{checking ? t("harness.checking") : t("harness.check")}</Button>
              <Button className="harness-update-all" variant="primary" icon="download" disabled={updates === 0} onClick={() => void run("all:update", "/api/harnesses/update-all")}>{t("harness.update.all")}</Button>
            </>
          )
        }
      />
      {loading && !data && <div className="empty calm">{t("common.loading")}</div>}
      {error && !data && (
        <div className="empty">
          <b>{t("harness.error")}</b>
          <div>{error}</div>
          <button className="btn primary" onClick={() => void refresh()}>{t("common.retry")}</button>
        </div>
      )}
      {data && (
        <div className="pg-split harness-screen">
          <section className={`pg-list ${split ? "" : "alone"}`} aria-label={t("harness.list")}>
            {refused["all:update"]?.length ? <Refused names={refused["all:update"]} /> : null}
            {rows.map((row) => (
              <HarnessItem key={row.harness} row={row} on={current?.harness === row.harness} onPick={() => setPicked(row.harness)} onAct={() => act(row)} refused={refused[`${row.harness}:update`] ?? []} />
            ))}
            {env === "container" && (
              <div className="harness-node" data-installed={data.node.installed}>
                <span className="harness-node-name">Node</span>
                <span className="mono">{data.node.installed ? data.node.version : "—"}</span>
                <span className="sub">{t("harness.node.pinned", { version: data.node.pinned })}</span>
                {!data.node.installed && <button className="btn small" onClick={() => void run("node:install", "/api/harnesses/node/install")}>{t("harness.node.install")}</button>}
              </div>
            )}
            <p className="harness-foot sub">
              <Icon name="alert" size={14} />
              <span>{t("harness.footnote")}</span>
            </p>
          </section>
          {split ? <section className="pg-detail" aria-label={t("harness.open")}>{pane}</section> : picked && current && <Sheet title={current.label} size="md" onClose={() => setPicked(null)}>{pane}</Sheet>}
        </div>
      )}
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
  return <span className={`harness-signin ${s.tone}`} data-signin={row.logged_in}>{t(s.key, { detail: s.detail })}</span>;
}

/** How many models the row offers, and of how many when the operator chose a few; the names are in the open row. */
function Models({ row }: { row: HarnessRow }) {
  if (!row.models.length) return <span className="harness-models" data-chosen={false}>{t("harness.models.none")}</span>;
  return (
    <span className="harness-models" title={row.models.join(", ")} data-chosen={row.models_chosen}>
      {plural("harness.models.count", row.models.length)}
      {row.models_chosen && <span className="harness-models-of"> · {t("harness.models.of", { n: row.models.length, total: row.all_models.length })}</span>}
    </span>
  );
}

/** The one thing a row offers: a button to install, sign in or update; the word for a CLI that is current
 *  or cannot be installed here; a spinner and a way to watch while something runs on it. */
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
  if (state === "current") return <span className="pg-state live harness-current" data-state="current"><Dot tone="live" />{t("harness.state.current")}</span>;
  if (state === "blocked") return <span className={`pg-state ${row.install_problem ? "attention" : "idle"} harness-blocked`} data-state="blocked" title={row.install_problem || row.error || undefined}><Dot tone={row.install_problem ? "attention" : "idle"} />{row.install_problem ? t("harness.state.needsnode") : t("harness.state.absent")}</span>;
  return (
    <button className="btn small" data-state={state} onClick={onAct}>
      {t(`harness.state.${state}`)}
    </button>
  );
}

/** The row's "Models to offer": every model the CLI listed, the chosen ones ticked. Saving sends the
 *  ticked ones; ticking none (or all) is no choice, and offers every model again. */
function OfferedModels({ row, onOffer }: { row: HarnessRow; onOffer: (models: string[] | null) => Promise<void> }) {
  const all = row.all_models ?? [];
  const initial = offeredDraft(row);
  const [draft, setDraft] = useState<string[]>(initial);
  const [busy, setBusy] = useState(false);
  // A save, an event or another window changes the row; the ticks follow it.
  const current = initial.join("\n");
  useEffect(() => setDraft(current ? current.split("\n") : []), [current]);
  if (all.length === 0) return <div className="sub">{t("harness.models.offer.none")}</div>;
  const sending = offeredToSend(draft, all);
  const changed = JSON.stringify(sending) !== JSON.stringify(row.models_chosen ? offeredToSend(initial, all) : null);
  async function save(models: string[] | null) {
    setBusy(true);
    try {
      await onOffer(models);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="harness-offer" data-harness={row.harness}>
      <div className="sub">{t("harness.models.offer.hint")}</div>
      {/* A CLI lists its models as "family/model" ids, often dozens of them; they are grouped by the
          family before the slash and searchable, where a wall of ticks cut each id short. */}
      <SelectionList
        label={t("harness.models.offer")}
        items={all.map((m) => ({ id: m, label: m.includes("/") ? m.slice(m.indexOf("/") + 1) : m, group: m.includes("/") ? m.slice(0, m.indexOf("/")) : undefined, keywords: m }))}
        value={draft}
        onChange={setDraft}
        disabled={busy}
        chosenLabel={t("harness.models.offered")}
        emptyChosen={t("harness.models.offered.all")}
      />
      <div className="harness-offer-actions">
        <button
          className="btn small"
          data-offer="all"
          disabled={busy || (!row.models_chosen && draft.length === 0)}
          onClick={() => (row.models_chosen ? void save(null) : setDraft([]))}
        >
          {t("harness.models.all")}
        </button>
        <button className="btn small primary" data-offer="save" disabled={busy || !changed} onClick={() => void save(sending)}>{t("common.save")}</button>
      </div>
    </div>
  );
}

/** The pane's sections: the agents with where each is defined, which models are offered, and the
 *  last self-check step by step. */
function Details({ row, onOffer }: { row: HarnessRow; onOffer: (models: string[] | null) => Promise<void> }) {
  const summary = checkSummary(row.self_check);
  const steps = "steps" in row.self_check && Array.isArray(row.self_check.steps) ? row.self_check.steps : [];
  return (
    <>
      <div className="pg-label"><span>{t("harness.agents")}</span></div>
      <div className="pg-card">
        {row.agents.length === 0 ? (
          <div className="pg-row sub">{t("harness.agents.none")}</div>
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
      </div>
      <div className="pg-label"><span>{t("harness.models.offer")}</span></div>
      <div className="pg-card harness-offer-card"><OfferedModels row={row} onOffer={onOffer} /></div>
      <div className="pg-label"><span>{t("harness.selfcheck")}</span></div>
      <div className="pg-card">
        <div className={`pg-row harness-check-line ${summary.key === "harness.check.failed" ? "failed" : ""}`}>
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
    </>
  );
}

/** One CLI in the list: its name and version in mono, whether it is signed in, how many models it
 *  offers, and the one button it needs. The row opens the CLI beside the list. */
function HarnessItem({ row, on, onPick, onAct, refused }: { row: HarnessRow; on: boolean; onPick: () => void; onAct: () => void; refused: string[] }) {
  const state = rowState(row);
  return (
    <>
      <div className={`pg-item harness-row ${on ? "on" : ""}`} data-harness={row.harness} data-state={state}>
        <button type="button" className="pg-item-main harness-name" aria-current={on} onClick={onPick}>
          <HarnessBadge harness={row.harness as Harness} />
          <span className="pg-item-body">
            <span className="pg-item-top"><span className="pg-item-name">{row.label}</span></span>
            <span className="pg-item-sub harness-versions">{row.installed ? <><Version row={row} />{row.update_available && <><span className="harness-arrow"> → </span><Latest row={row} /></>}</> : <>{t("harness.col.latest")} <Latest row={row} /></>}</span>
            <span className="pg-item-sub"><SignIn row={row} /> · <Models row={row} /></span>
          </span>
        </button>
        <div className="pg-item-trail harness-action"><Action row={row} onAct={onAct} /></div>
      </div>
      {refused.length > 0 && <div className="harness-row-refused"><Refused names={refused} /></div>}
    </>
  );
}

/** The open CLI: its facts, the agents it offers and where each is defined, the models the operator
 *  chose to offer, and its last self-check step by step. */
function HarnessPane({ row, onAct, onOffer, refused }: { row: HarnessRow; onAct: () => void; onOffer: (models: string[] | null) => Promise<void>; refused: string[] }) {
  return (
    <div className="pg-pane harness-details" data-pane="harness">
      <div className="pg-pane-head">
        <HarnessBadge harness={row.harness as Harness} />
        <span className="grow" />
        <Action row={row} onAct={onAct} />
      </div>
      <h2 className="pg-pane-title">{row.label}</h2>
      {refused.length > 0 && <Refused names={refused} />}
      {row.version_guard === "unverified" && <div className="harness-guard" role="note">{t("harness.guard", { version: row.installed_version, low: row.tested_versions[0], high: row.tested_versions[1] })}</div>}
      {row.unavailable && row.installed && <div className="harness-unavailable sub">{row.unavailable}</div>}
      <div className="pg-facts">
        <div className="pg-fact"><span className="pg-fact-key">{t("harness.col.version")}</span><span className="pg-fact-value"><Version row={row} /></span></div>
        <div className="pg-fact"><span className="pg-fact-key">{t("harness.col.latest")}</span><span className="pg-fact-value"><Latest row={row} /></span></div>
        <div className="pg-fact"><span className="pg-fact-key">{t("harness.col.signin")}</span><span className="pg-fact-value"><SignIn row={row} /></span></div>
        <div className="pg-fact"><span className="pg-fact-key">{t("harness.col.models")}</span><span className="pg-fact-value">{row.models.length ? row.models.slice(0, 4).join(" · ") + (row.models.length > 4 ? ` ${t("harness.more", { n: row.models.length - 4 })}` : "") : "—"}</span></div>
        <div className="pg-fact"><span className="pg-fact-key">{t("harness.col.channel")}</span><span className="pg-fact-value">{channelWords(row)}</span></div>
      </div>
      <Details row={row} onOffer={onOffer} />
    </div>
  );
}
