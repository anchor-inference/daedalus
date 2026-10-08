// Settings → Environments → browser sessions: the agent's browser as the operator runs it — which Chromium, whether its
// sandbox holds, what runs now and what it costs, the profiles that keep its logins, how many may run
// at once, how long an idle one lives, what is recorded, where the agent may act only while watched,
// which local sites and addresses on the local network it may be let into, the injection monitor, the model that reads
// a page for the agent and whether it may click at a point, and the site notes agents proposed.
//
// Everything here is the host's `[browser]` section; the host hands the daemon its part at once, so a
// cap typed here is the daemon's cap, not a number only the load bar believes. What cannot be undone —
// closing a browser, clearing or deleting a profile, deleting recordings — asks first, and says what
// goes with it.

import { useEffect, useRef, useState } from "react";
import type { BrowserEnv, BrowserList, BrowserProfile, BrowserRecordings, BrowserSettings, BrowserSiteNote, RunningBrowser, Settings, WorkloadsLoad } from "../api";
import { api } from "../api";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { size } from "../loadbar";
import { invalidate, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { Dropdown, Switch, timeAgo } from "../ui/components";
import { Row } from "../settingsrow";
import { ProcedureEditor } from "../browser/steps";

export const DEFAULT_BROWSER: BrowserSettings = {
  env: "auto",
  running_cap: 2,
  idle_close_minutes: 10,
  agent_wait_seconds: 60,
  control_wait_seconds: 20,
  local_sites: "ask",
  lan_allow: [],
  lan_sites: "ask",
  record_frames: false,
  record_takeover: false,
  record_retention_days: 7,
  record_max_mb: 500,
  watch_mode: false,
  watch_domains: [],
  injection_monitor: false,
  injection_monitor_preset: "",
  extract_preset: "",
  point_clicks: false,
};

/** A whole number within `lo`..`hi` from a draft, or null while it is not one. */
export function wholeIn(draft: string, lo: number, hi: number): number | null {
  const v = Number(draft.trim());
  return draft.trim() !== "" && Number.isInteger(v) && v >= lo && v <= hi ? v : null;
}

/** One entry a line, blanks and repeats dropped: how the lists of hosts and addresses are typed. */
export function lines(text: string): string[] {
  return [...new Set(text.split(/[\n,]/).map((x) => x.trim()).filter(Boolean))];
}

const RUNNING = "/api/browsers/running";
const PROFILES = "/api/browsers/profiles";
const RECORDINGS = "/api/browsers/recordings";
const NOTES = "/api/browsers/notes";

/** How long changes are gathered before they are saved as one: a toggle and a list edited a moment
 *  apart would otherwise race, the second sent against the revision the first had just replaced. */
const SAVE_GATHER_MS = 350;

export function BrowserSettingsTab({ s, save, toast, onDraft }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void>; toast: (text: string) => void; onDraft?: (cap: number | null) => void }) {
  const saved: BrowserSettings = { ...DEFAULT_BROWSER, ...(s.browser ?? {}) };
  const [draft, setDraft] = useState<Partial<BrowserSettings>>({});
  // What is shown is what was saved with what is about to be: a button answers at once.
  const b: BrowserSettings = { ...saved, ...draft };
  const latest = useRef({ saved, save });
  latest.current = { saved, save };
  const timer = useRef<number | null>(null);
  const gathered = useRef<Partial<BrowserSettings>>({});
  useEffect(() => () => {
    if (timer.current !== null) window.clearTimeout(timer.current);
  }, []);
  const set = (patch: Partial<BrowserSettings>) => {
    gathered.current = { ...gathered.current, ...patch };
    setDraft(gathered.current);
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      const changes = gathered.current;
      gathered.current = {};
      void latest.current.save({ browser: { ...latest.current.saved, ...changes } }).finally(() => setDraft(gathered.current));
    }, SAVE_GATHER_MS);
  };
  const listing = useQuery<BrowserList & { envs?: BrowserEnv[] }>("/api/browsers?status=open", { pollMs: 15000 });
  const envs = (listing.data?.envs ?? []).filter((e) => e.configured);
  if (listing.data && !envs.length) {
    return (
      <div className="card bs-none">
        <div className="section-title" style={{ marginTop: 0 }}>{t("bs.none.title")}</div>
        <div className="sub">{t("bs.none.body")}</div>
        <code className="bs-code">COMPOSE_PROFILES=browser</code>
      </div>
    );
  }
  return (
    <div className="bs">
      <Environments envs={envs} />
      <Running toast={toast} />
      <Limits b={b} set={set} onDraft={onDraft} />
      <Profiles toast={toast} />
      <Recording b={b} set={set} toast={toast} />
      <Watch b={b} set={set} />
      <Network b={b} set={set} />
      <Monitor b={b} set={set} presets={Object.keys(s.presets ?? {})} />
      <Reading b={b} set={set} presets={Object.keys(s.presets ?? {})} />
      <SiteNotes toast={toast} />
    </div>
  );
}

function Environments({ envs }: { envs: BrowserEnv[] }) {
  return (
    <div className="card bs-envs">
      <div className="section-title" style={{ marginTop: 0 }}>{t("bs.env.title")}</div>
      {envs.map((e) => (
        <div key={e.env} className="bs-env" data-env={e.env} data-available={e.available}>
          <div className="bs-env-head">
            <span className={`dot ${e.available ? "ok" : "error"}`} aria-hidden="true" />
            <b>{t(`bs.env.${e.env === "host" ? "host" : "container"}`)}</b>
            <span className="sub">{e.available ? t("bs.env.version", { version: e.version || "?" }) : e.detail || e.reason}</span>
          </div>
          {e.available && (
            <>
              <div className="kv"><span>{t("bs.env.chromium")}</span><b className="mono">
                {/* Each half keeps its words together, so a narrow screen breaks the line between the
                    version and where it came from, not inside "installed with Daedalus". */}
                <span className="kv-part">{e.chromium?.version || t("bs.env.unknown")}</span>
                {e.chromium?.kind ? <> · <span className="kv-part">{t(`bs.env.kind.${e.chromium.kind === "system" ? "system" : "bundled"}`)}</span></> : null}
              </b></div>
              <div className="kv"><span>{t("bs.env.sandbox")}</span><b className={e.sandbox === "ok" ? "" : "bs-warn"}>{e.sandbox === "ok" ? t("bs.env.sandbox.ok") : e.sandbox === "unknown" || !e.sandbox ? t("bs.env.sandbox.unknown") : e.sandbox}</b></div>
              <div className="kv"><span>{t("bs.env.walls")}</span><b>{t(e.env === "host" ? "bs.env.walls.host" : "bs.env.walls.container")}</b></div>
            </>
          )}
        </div>
      ))}
    </div>
  );
}

function Running({ toast }: { toast: (text: string) => void }) {
  const running = useQuery<{ browsers: RunningBrowser[] }>(RUNNING, { pollMs: 10000 });
  const list = running.data?.browsers ?? [];
  async function close(r: RunningBrowser) {
    const owners = r.groups.map((g) => g.owner.label).filter(Boolean).join(", ");
    if (!(await confirmAsync(t("bs.running.close.title"), { body: t("bs.running.close.body", { owners: owners || r.profile }), action: t("bs.running.close") }))) return;
    try {
      await api.post(`${RUNNING}/${encodeURIComponent(r.env)}/${encodeURIComponent(r.id)}/close`, {});
      invalidate(RUNNING);
      invalidate("/api/browsers");
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <div className="card bs-running">
      <div className="section-title" style={{ marginTop: 0 }}>{t("bs.running.title")}</div>
      {list.length === 0 ? (
        <div className="sub">{t("bs.running.none")}</div>
      ) : (
        <ul className="bs-rows">
          {list.map((r) => (
            <li key={`${r.env}/${r.id}`} className="bs-row" data-browser={r.id}>
              <Icon name="globe" size={16} />
              <span className="bs-row-main">
                <b className="truncate">{r.groups.map((g) => g.owner.label).filter(Boolean).join(", ") || r.profile}</b>
                <span className="sub truncate">{[r.profile, plural("bs.tabs", r.tabs), r.groups[0]?.title || r.groups[0]?.url].filter(Boolean).join(" · ")}</span>
              </span>
              <span className="bs-row-figure" title={t("bs.running.memory")}>{size(r.rss_bytes)}</span>
              <button type="button" className="btn small danger" onClick={() => void close(r)}>{t("bs.running.close")}</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Limits({ b, set, onDraft }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void; onDraft?: (cap: number | null) => void }) {
  const [cap, setCap] = useState(String(b.running_cap));
  const [idle, setIdle] = useState(String(b.idle_close_minutes));
  useEffect(() => setCap(String(b.running_cap)), [b.running_cap]);
  useEffect(() => setIdle(String(b.idle_close_minutes)), [b.idle_close_minutes]);
  const capValue = wholeIn(cap, 1, 32);
  useEffect(() => onDraft?.(capValue), [onDraft, capValue]);
  const idleValue = wholeIn(idle, 0, 1440);
  const load = useQuery<WorkloadsLoad>("/api/workloads/load", { pollMs: 10000 });
  return (
    <div className="card bs-limits">
      <div className="section-title" style={{ marginTop: 0 }}>{t("bs.limits.title")}</div>
      <div className="sub">{t("bs.limits.sub")}</div>
      <Row title={t("bs.limits.cap")} htmlFor="browser-cap" desc={load.data?.browsers ? plural("bs.limits.running", load.data.browsers.running) : undefined}>
        <span className="settings-num">
          <input
            id="browser-cap"
            className="field"
            type="number"
            inputMode="numeric"
            min={1}
            max={32}
            value={cap}
            aria-invalid={capValue === null}
            onChange={(e) => setCap(e.target.value)}
            onBlur={() => (capValue === null ? setCap(String(b.running_cap)) : capValue !== b.running_cap && set({ running_cap: capValue }))}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          />
          <span className="settings-unit" />
        </span>
      </Row>
      {capValue === null && <div className="sub push-error">{t("bs.limits.cap.invalid")}</div>}
      <Row title={t("bs.limits.idle")} htmlFor="browser-idle" desc={t("bs.limits.idle.sub")} stack>
        <span className="settings-num">
          <input
            id="browser-idle"
            className="field"
            type="number"
            inputMode="numeric"
            min={0}
            max={1440}
            value={idle}
            aria-invalid={idleValue === null}
            onChange={(e) => setIdle(e.target.value)}
            onBlur={() => (idleValue === null ? setIdle(String(b.idle_close_minutes)) : idleValue !== b.idle_close_minutes && set({ idle_close_minutes: idleValue }))}
            onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          />
          <span className="settings-unit">{idleValue === 0 ? t("bs.limits.idle.never") : t("bs.limits.idle.unit")}</span>
        </span>
      </Row>
    </div>
  );
}

const SCOPES: Record<string, string> = { project: "bs.scope.project", session: "bs.scope.session", staff: "bs.scope.staff", ephemeral: "bs.scope.ephemeral" };

function Profiles({ toast }: { toast: (text: string) => void }) {
  const profiles = useQuery<{ profiles: BrowserProfile[] }>(PROFILES, { pollMs: 30000 });
  const list = profiles.data?.profiles ?? [];
  async function act(p: BrowserProfile, action: "clear" | "delete") {
    const ok = await confirmAsync(t(`bs.profile.${action}.title`), { body: t(`bs.profile.${action}.body`, { id: p.id }), action: t(`bs.profile.${action}`), danger: action === "delete" });
    if (!ok) return;
    try {
      const path = `${PROFILES}/${encodeURIComponent(p.env)}/${encodeURIComponent(p.id)}`;
      if (action === "clear") await api.post(`${path}/clear`, {});
      else await api.delete(path);
      invalidate(PROFILES);
      toast(t(`bs.profile.${action}.done`, { id: p.id }));
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <div className="card bs-profiles">
      <div className="section-title" style={{ marginTop: 0 }}>{t("bs.profiles.title")}</div>
      <div className="sub">{t("bs.profiles.sub")}</div>
      {list.length === 0 ? (
        <div className="sub">{t("bs.profiles.none")}</div>
      ) : (
        <ul className="bs-rows">
          {list.map((p) => (
            <li key={`${p.env}/${p.id}`} className="bs-row" data-profile={p.id}>
              <Icon name="key" size={16} />
              <span className="bs-row-main">
                <b className="truncate mono">{p.id}</b>
                <span className="sub truncate">{[t(SCOPES[p.scope] ?? "bs.scope.session"), t("bs.profiles.used", { when: timeAgo(p.last_used_at) }), p.running ? t("bs.profiles.running") : ""].filter(Boolean).join(" · ")}</span>
              </span>
              <span className="bs-row-figure">{size(p.size_bytes)}</span>
              <button type="button" className="btn small" disabled={p.running} title={p.running ? t("bs.profiles.busy") : undefined} onClick={() => void act(p, "clear")}>{t("bs.profile.clear")}</button>
              <button type="button" className="btn small danger" disabled={p.running} title={p.running ? t("bs.profiles.busy") : undefined} onClick={() => void act(p, "delete")}>{t("bs.profile.delete")}</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Recording({ b, set, toast }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void; toast: (text: string) => void }) {
  const recordings = useQuery<BrowserRecordings>(RECORDINGS, { pollMs: 30000 });
  const [days, setDays] = useState(String(b.record_retention_days));
  const [mb, setMb] = useState(String(b.record_max_mb));
  useEffect(() => setDays(String(b.record_retention_days)), [b.record_retention_days]);
  useEffect(() => setMb(String(b.record_max_mb)), [b.record_max_mb]);
  const daysValue = wholeIn(days, 1, 365);
  const mbValue = wholeIn(mb, 10, 100000);
  const used = (recordings.data?.envs ?? []).reduce((n, e) => n + e.bytes, 0);
  const groups = (recordings.data?.envs ?? []).reduce((n, e) => n + e.groups.length, 0);
  async function deleteAll() {
    if (!(await confirmAsync(t("bs.rec.delete.title"), { body: t("bs.rec.delete.body"), action: t("bs.rec.delete"), danger: true }))) return;
    try {
      for (const env of recordings.data?.envs ?? []) {
        for (const g of env.groups) await api.delete(`/api/browsers/${encodeURIComponent(g.group_id)}/recording`);
      }
      invalidate(RECORDINGS);
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <div className="card bs-recording">
      <div className="section-title" style={{ marginTop: 0 }}>{t("bs.rec.title")}</div>
      <div className="sub">{t("bs.rec.sub")}</div>
      <Row title={t("bs.rec.default")} desc={t("bs.rec.default.sub")}>
        <Switch checked={b.record_frames} onChange={(record_frames) => set({ record_frames })} label={t("bs.rec.default")} />
      </Row>
      <Row title={t("bs.rec.takeover")} desc={t("bs.rec.takeover.sub")}>
        <Switch checked={b.record_takeover} onChange={(record_takeover) => set({ record_takeover })} label={t("bs.rec.takeover")} />
      </Row>
      <Row title={t("bs.rec.days")} htmlFor="browser-rec-days" stack>
        <span className="settings-num">
          <input id="browser-rec-days" className="field" type="number" min={1} max={365} value={days} aria-invalid={daysValue === null} onChange={(e) => setDays(e.target.value)} onBlur={() => (daysValue === null ? setDays(String(b.record_retention_days)) : daysValue !== b.record_retention_days && set({ record_retention_days: daysValue }))} />
          <span className="settings-unit">{t("settings.unit.days")}</span>
        </span>
      </Row>
      <Row title={t("bs.rec.mb")} htmlFor="browser-rec-mb" desc={plural("bs.rec.used", groups, { used: size(used), max: size(b.record_max_mb * 1024 * 1024) })} stack>
        <span className="settings-num">
          <input id="browser-rec-mb" className="field" type="number" min={10} value={mb} aria-invalid={mbValue === null} onChange={(e) => setMb(e.target.value)} onBlur={() => (mbValue === null ? setMb(String(b.record_max_mb)) : mbValue !== b.record_max_mb && set({ record_max_mb: mbValue }))} />
          <span className="settings-unit">{t("settings.unit.mb")}</span>
        </span>
      </Row>
      {groups > 0 && (
        <div className="btnrow bs-rec-used">
          <button type="button" className="btn small danger" onClick={() => void deleteAll()}>{t("bs.rec.delete")}</button>
        </div>
      )}
    </div>
  );
}

function Watch({ b, set }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void }) {
  const [text, setText] = useState(b.watch_domains.join("\n"));
  useEffect(() => setText(b.watch_domains.join("\n")), [b.watch_domains]);
  return (
    <div className="card bs-watch">
      <div className="section-title" style={{ marginTop: 0 }}>{t("bs.watch.title")}</div>
      <div className="sub">{t("bs.watch.sub")}</div>
      <Row title={t("bs.watch.mode")}>
        <Switch checked={b.watch_mode} onChange={(watch_mode) => set({ watch_mode })} label={t("bs.watch.mode")} />
      </Row>
      <label className="field" htmlFor="browser-watch">{t("bs.watch.domains")}</label>
      <textarea id="browser-watch" className="field bs-list" rows={6} value={text} spellCheck={false} onChange={(e) => setText(e.target.value)} onBlur={() => {
        const next = lines(text);
        if (next.join("\n") !== b.watch_domains.join("\n")) set({ watch_domains: next });
      }} />
      <div className="sub">{t("bs.watch.hint")}</div>
    </div>
  );
}

function Network({ b, set }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void }) {
  const [text, setText] = useState(b.lan_allow.join("\n"));
  useEffect(() => setText(b.lan_allow.join("\n")), [b.lan_allow]);
  return (
    <div className="card bs-network">
      <div className="section-title" style={{ marginTop: 0 }}>{t("bs.local.title")}</div>
      <div className="sub">{t("bs.local.sub")}</div>
      <Row title={t("bs.local.title")} stack>
        <Dropdown id="browser-local-sites" label={t("bs.local.title")} value={b.local_sites} onChange={(local_sites) => set({ local_sites })} options={[
          { id: "services", label: t("bs.local.services") },
          { id: "ask", label: t("bs.local.ask") },
          { id: "allow", label: t("bs.local.allow") },
        ]} />
      </Row>
      <div className="section-title">{t("bs.lan.title")}</div>
      <div className="sub">{t("bs.lan.sub")}</div>
      <Row title={t("bs.lan.title")} stack>
        <Dropdown id="browser-lan-sites" label={t("bs.lan.title")} value={b.lan_sites} onChange={(lan_sites) => set({ lan_sites })} options={[
          { id: "ask", label: t("bs.lan.ask") },
          { id: "listed", label: t("bs.lan.listed") },
        ]} />
      </Row>
      <label className="field" htmlFor="browser-lan">{t("bs.lan.label")}</label>
      <textarea id="browser-lan" className="field bs-list" rows={3} value={text} spellCheck={false} placeholder="10.0.5.20" onChange={(e) => setText(e.target.value)} onBlur={() => {
        const next = lines(text);
        if (next.join("\n") !== b.lan_allow.join("\n")) set({ lan_allow: next });
      }} />
    </div>
  );
}

function Monitor({ b, set, presets }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void; presets: string[] }) {
  return (
    <div className="card bs-monitor">
      <div className="section-title" style={{ marginTop: 0 }}>{t("bs.monitor.title")}</div>
      <div className="sub">{t("bs.monitor.sub")}</div>
      <Row title={t("bs.monitor.mode")}>
        <Switch checked={b.injection_monitor} onChange={(injection_monitor) => set({ injection_monitor })} label={t("bs.monitor.mode")} />
      </Row>
      <Row title={t("bs.monitor.model")} stack>
        <Dropdown id="browser-monitor-model" label={t("bs.monitor.model")} value={b.injection_monitor_preset} onChange={(injection_monitor_preset) => set({ injection_monitor_preset })} options={[{ id: "", label: t("bs.monitor.model.auto") }, ...presets.map((p) => ({ id: p, label: p }))]} />
      </Row>
    </div>
  );
}

function Reading({ b, set, presets }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void; presets: string[] }) {
  return (
    <div className="card bs-reading">
      <div className="section-title" style={{ marginTop: 0 }}>{t("bs.agent.title")}</div>
      <div className="sub">{t("bs.agent.sub")}</div>
      <Row title={t("bs.agent.extract")} stack>
        <Dropdown id="browser-extract-model" label={t("bs.agent.extract")} value={b.extract_preset} onChange={(extract_preset) => set({ extract_preset })} options={[{ id: "", label: t("bs.monitor.model.auto") }, ...presets.map((p) => ({ id: p, label: p }))]} />
      </Row>
      <Row title={t("bs.agent.point")} desc={t("bs.agent.point.sub")}>
        <Switch checked={b.point_clicks} onChange={(point_clicks) => set({ point_clicks })} label={t("bs.agent.point")} />
      </Row>
    </div>
  );
}

/** The notes agents proposed about sites: the waiting ones to approve or discard, the approved ones to
 *  take back. Nothing here is shown to an agent until it is approved. */
function SiteNotes({ toast }: { toast: (text: string) => void }) {
  const notes = useQuery<{ notes: BrowserSiteNote[] }>(NOTES, { pollMs: 30000 });
  const list = notes.data?.notes ?? [];
  const [editing, setEditing] = useState<string | null>(null);
  const [whole, setWhole] = useState<Set<string>>(() => new Set());
  async function approve(n: BrowserSiteNote) {
    try {
      await api.post(`${NOTES}/${encodeURIComponent(n.id)}/approve`, {});
      invalidate(NOTES);
      toast(t("bs.notes.approved", { host: n.host }));
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function remove(n: BrowserSiteNote) {
    // A waiting note was never shown to anyone: discarding it needs no second thought.
    if (n.status === "active" && !(await confirmAsync(t("bs.notes.remove.title"), { body: t("bs.notes.remove.body", { host: n.host }), action: t("bs.notes.remove"), danger: true }))) return;
    try {
      await api.delete(`${NOTES}/${encodeURIComponent(n.id)}`);
      invalidate(NOTES);
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <div className="card bs-notes">
      <div className="section-title" style={{ marginTop: 0 }}>{t("bs.notes.title")}</div>
      <div className="sub">{t("bs.notes.sub")}</div>
      {list.length === 0 ? (
        <div className="sub">{t("bs.notes.none")}</div>
      ) : (
        <ul className="bs-rows">
          {list.map((n) => n.kind === "procedure" && editing === n.id ? (
            <li key={n.id} className="bs-row bs-note bs-proc" data-note={n.id} data-status={n.status} data-kind={n.kind}>
              <ProcedureEditor draft={{ note: n }} toast={toast} onDone={() => { setEditing(null); invalidate(NOTES); }} />
            </li>
          ) : (
            <li key={n.id} className={`bs-row bs-note ${n.kind === "procedure" ? "bs-proc" : ""}`} data-note={n.id} data-status={n.status} data-kind={n.kind}>
              <Icon name={n.kind === "procedure" ? "skill" : "globe"} size={16} />
              <span className="bs-row-main">
                <b className="truncate mono">{n.host}</b>
                {n.kind === "procedure" && <b className="bs-proc-title">{n.title}</b>}
                {n.kind === "procedure" ? (
                  <>
                    <pre className={`bs-proc-text ${whole.has(n.id) ? "whole" : ""}`}>{n.text}</pre>
                    <button type="button" className="bs-proc-more" onClick={() => setWhole((w) => { const next = new Set(w); if (next.has(n.id)) next.delete(n.id); else next.add(n.id); return next; })}>
                      {t(whole.has(n.id) ? "bs.notes.less" : "bs.notes.more")}
                    </button>
                  </>
                ) : (
                  <span className="bs-note-text">{n.text}</span>
                )}
                <span className="sub truncate">{[...(n.kind === "procedure" ? [t("bs.notes.procedure")] : []), t(n.status === "proposed" ? "bs.notes.waiting" : "bs.notes.active"), n.project || t("bs.notes.everyone"), timeAgo(new Date((n.approved_at || n.proposed_at) * 1000).toISOString())].join(" · ")}</span>
              </span>
              {n.kind === "procedure" && <button type="button" className="btn small" onClick={() => setEditing(n.id)}>{t("bs.notes.edit")}</button>}
              {n.status === "proposed" && <button type="button" className="btn small primary" onClick={() => void approve(n)}>{t("bs.notes.approve")}</button>}
              <button type="button" className="btn small danger" onClick={() => void remove(n)}>{t(n.status === "proposed" ? "bs.notes.discard" : "bs.notes.remove")}</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
