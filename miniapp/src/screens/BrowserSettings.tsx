// The agent's browser as the operator runs it, over three sections of Settings. Environments: which
// Chromium, whether its sandbox holds, what runs now and what it costs, how many may run at once and
// how long an idle one lives. Browser: the operator's own Chrome, the profiles that keep its logins,
// what is recorded, the model that reads a page for the agent and whether it may click at a point,
// which WebGL the agent may turn on without asking, and the site notes agents proposed. Browser safety:
// where the agent may act only while watched, which local sites and addresses on the local network it
// may be let into, and the injection monitor.
//
// Everything here is the host's `[browser]` section; the host hands the daemon its part at once, so a
// cap typed here is the daemon's cap, not a number only the load bar believes. What cannot be undone —
// closing a browser, clearing or deleting a profile, deleting recordings — asks first, and says what
// goes with it.

import { useCallback, useEffect, useRef, useState } from "react";
import type { BrowserEnv, BrowserList, BrowserProfile, BrowserRecordings, BrowserSettings, BrowserSiteNote, RunningBrowser, Settings, WorkloadsLoad } from "../api";
import { api } from "../api";
import { plural, t } from "../i18n";
import { size } from "../loadbar";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";
import { timeAgo } from "../ui/components";
import { Button, confirmDialog, Dot, Select, Switch } from "../ui/primitives";
import { Row, SetBlock, SetFoot, SetGroup } from "../settingsrow";
import { ModelPicker, type ModelCatalogue } from "../modelitems";
import { WithEffort } from "../taskeffort";
import { ProcedureEditor } from "../browser/steps";
import { DEFAULT_USER, YourChrome } from "../browser/yourchrome";

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
  webgl_allow: "gpu",
  user: DEFAULT_USER,
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

export function BrowserSettingsTab({ s, save, toast, onDraft, part }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void>; toast: (text: string) => void; onDraft?: (cap: number | null) => void; part: "capacity" | "browser" | "safety" }) {
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
  const envs = (listing.data?.envs ?? []).filter((e) => e.configured && e.env !== "user");
  if (listing.data && !envs.length) {
    return (
      <SetGroup title={t("bs.none.title")} help={t("bs.none.body")} className="bs-none">
        <SetBlock><code className="bs-code">COMPOSE_PROFILES=browser</code></SetBlock>
      </SetGroup>
    );
  }
  if (part === "capacity") return (
    <>
      <Environments envs={envs} />
      <Running toast={toast} />
      <Limits b={b} set={set} onDraft={onDraft} />
    </>
  );
  if (part === "safety") return (
    <>
      <Watch b={b} set={set} />
      <Network b={b} set={set} />
      <Monitor b={b} set={set} catalogue={s} />
    </>
  );
  return (
    <>
      <YourChrome user={{ ...DEFAULT_USER, ...(b.user ?? {}) }} setUser={(patch) => set({ user: { ...DEFAULT_USER, ...(b.user ?? {}), ...patch } })} toast={toast} />
      <Profiles toast={toast} />
      <Recording b={b} set={set} toast={toast} />
      <Reading b={b} set={set} catalogue={s} />
      <Graphics b={b} set={set} envs={envs} />
      <SiteNotes toast={toast} />
    </>
  );
}

/** A whole number typed in place, kept in step with the saved one; a value outside `lo`..`hi` is
 *  put back when the field is left rather than sent. `onDraft` hands the page the number as it is
 *  typed (null while it is not one), for what draws from the draft. */
function WholeInput({ id, value, lo, hi, unit, onSave, onDraft }: { id: string; value: number; lo: number; hi: number; unit?: string; onSave: (v: number) => void; onDraft?: (v: number | null) => void }) {
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const parsed = wholeIn(draft, lo, hi);
  useEffect(() => onDraft?.(parsed), [onDraft, parsed]);
  return (
    <span className="settings-num">
      <input
        id={id}
        className="field"
        type="number"
        inputMode="numeric"
        min={lo}
        max={hi}
        value={draft}
        aria-invalid={parsed === null}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => (parsed === null ? setDraft(String(value)) : parsed !== value && onSave(parsed))}
        onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
      />
      <span className="settings-unit">{unit ?? ""}</span>
    </span>
  );
}

function Environments({ envs }: { envs: BrowserEnv[] }) {
  return (
    <SetGroup title={t("bs.env.title")} className="bs-envs">
      {envs.map((e) => (
        <div key={e.env} className="bs-env" data-env={e.env} data-available={e.available}>
          <Row
            title={<span className="bs-env-head"><Dot status={e.available ? "ok" : "error"} />{t(`bs.env.${e.env === "host" ? "host" : "container"}`)}</span>}
            desc={e.available ? undefined : e.detail || e.reason}
          >
            {e.available && <span className="settings-value">{t("bs.env.version", { version: e.version || "?" })}</span>}
          </Row>
          {e.available && (
            <>
              <Row title={t("bs.env.chromium")}>
                {/* Each half keeps its words together, so a narrow screen breaks the line between the
                    version and where it came from, not inside "installed with Daedalus". */}
                <span className="settings-value">
                  <span className="kv-part mono">{e.chromium?.version || t("bs.env.unknown")}</span>
                  {e.chromium?.kind ? <> · <span className="kv-part">{t(`bs.env.kind.${e.chromium.kind === "system" ? "system" : "bundled"}`)}</span></> : null}
                </span>
              </Row>
              <Row title={t("bs.env.sandbox")}>
                <span className={`settings-value ${e.sandbox === "ok" ? "" : "bs-warn"}`}>{e.sandbox === "ok" ? t("bs.env.sandbox.ok") : e.sandbox === "unknown" || !e.sandbox ? t("bs.env.sandbox.unknown") : e.sandbox}</span>
              </Row>
              <Row title={t("bs.env.walls")}>
                <span className="settings-value">{t(e.env === "host" ? "bs.env.walls.host" : "bs.env.walls.container")}</span>
              </Row>
            </>
          )}
        </div>
      ))}
    </SetGroup>
  );
}

function Running({ toast }: { toast: (text: string) => void }) {
  const running = useQuery<{ browsers: RunningBrowser[] }>(RUNNING, { pollMs: 10000 });
  const list = running.data?.browsers ?? [];
  async function close(r: RunningBrowser) {
    const owners = r.groups.map((g) => g.owner.label).filter(Boolean).join(", ");
    if (!(await confirmDialog({ title: t("bs.running.close.title"), body: t("bs.running.close.body", { owners: owners || r.profile }), action: t("bs.running.close"), danger: true }))) return;
    try {
      await api.post(`${RUNNING}/${encodeURIComponent(r.env)}/${encodeURIComponent(r.id)}/close`, {});
      invalidate(RUNNING);
      invalidate("/api/browsers");
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <SetGroup title={t("bs.running.title")} className="bs-running">
      {list.length === 0 ? (
        <SetBlock><span className="set-hint">{t("bs.running.none")}</span></SetBlock>
      ) : list.map((r) => (
        <Row
          key={`${r.env}/${r.id}`}
          className="bs-row"
          data-browser={r.id}
          title={r.groups.map((g) => g.owner.label).filter(Boolean).join(", ") || r.profile}
          desc={[r.profile, plural("bs.tabs", r.tabs), r.groups[0]?.title || r.groups[0]?.url].filter(Boolean).join(" · ")}
        >
          <span className="bs-row-figure" title={t("bs.running.memory")}>{size(r.rss_bytes)}</span>
          <Button size="sm" variant="danger" onClick={() => void close(r)}>{t("bs.running.close")}</Button>
        </Row>
      ))}
    </SetGroup>
  );
}

function Limits({ b, set, onDraft }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void; onDraft?: (cap: number | null) => void }) {
  const [capValid, setCapValid] = useState(true);
  const onCap = useCallback((cap: number | null) => {
    setCapValid(cap !== null);
    onDraft?.(cap);
  }, [onDraft]);
  const [idle, setIdle] = useState<number | null>(b.idle_close_minutes);
  const load = useQuery<WorkloadsLoad>("/api/workloads/load", { pollMs: 10000 });
  return (
    <SetGroup title={t("bs.limits.title")} help={t("bs.limits.sub")} className="bs-limits">
      <Row title={t("bs.limits.cap")} htmlFor="browser-cap" data-setting="browser.running_cap" desc={load.data?.browsers ? plural("bs.limits.running", load.data.browsers.running) : undefined}>
        <WholeInput id="browser-cap" value={b.running_cap} lo={1} hi={32} onSave={(running_cap) => set({ running_cap })} onDraft={onCap} />
      </Row>
      {!capValid && <SetBlock><span className="set-hint push-error">{t("bs.limits.cap.invalid")}</span></SetBlock>}
      <Row title={t("bs.limits.idle")} htmlFor="browser-idle" desc={t("bs.limits.idle.sub")} data-setting="browser.idle_close_minutes">
        <WholeInput id="browser-idle" value={b.idle_close_minutes} lo={0} hi={1440} unit={idle === 0 ? t("bs.limits.idle.never") : t("bs.limits.idle.unit")} onSave={(idle_close_minutes) => set({ idle_close_minutes })} onDraft={setIdle} />
      </Row>
    </SetGroup>
  );
}

const SCOPES: Record<string, string> = { project: "bs.scope.project", session: "bs.scope.session", staff: "bs.scope.staff", ephemeral: "bs.scope.ephemeral" };

function Profiles({ toast }: { toast: (text: string) => void }) {
  const profiles = useQuery<{ profiles: BrowserProfile[] }>(PROFILES, { pollMs: 30000 });
  const list = profiles.data?.profiles ?? [];
  async function act(p: BrowserProfile, action: "clear" | "delete") {
    const ok = await confirmDialog({ title: t(`bs.profile.${action}.title`), body: t(`bs.profile.${action}.body`, { id: p.id }), action: t(`bs.profile.${action}`), danger: action === "delete" });
    if (!ok) return;
    try {
      const path = `${PROFILES}/${encodeURIComponent(p.env)}/${encodeURIComponent(p.id)}`;
      if (action === "clear") await api.post(`${path}/clear`, {});
      else await api.delete(path);
      invalidate(PROFILES);
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <SetGroup title={t("bs.profiles.title")} help={t("bs.profiles.sub")} className="bs-profiles" data-setting="browser.profiles">
      {list.length === 0 ? (
        <SetBlock><span className="set-hint">{t("bs.profiles.none")}</span></SetBlock>
      ) : list.map((p) => (
        <Row
          key={`${p.env}/${p.id}`}
          className="bs-row"
          data-profile={p.id}
          title={<span className="mono">{p.id}</span>}
          desc={[t(SCOPES[p.scope] ?? "bs.scope.session"), t("bs.profiles.used", { when: timeAgo(p.last_used_at) }), p.running ? t("bs.profiles.running") : ""].filter(Boolean).join(" · ")}
        >
          <span className="bs-row-figure">{size(p.size_bytes)}</span>
          <Button size="sm" disabled={p.running} title={p.running ? t("bs.profiles.busy") : undefined} onClick={() => void act(p, "clear")}>{t("bs.profile.clear")}</Button>
          <Button size="sm" variant="danger" disabled={p.running} title={p.running ? t("bs.profiles.busy") : undefined} onClick={() => void act(p, "delete")}>{t("bs.profile.delete")}</Button>
        </Row>
      ))}
    </SetGroup>
  );
}

function Recording({ b, set, toast }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void; toast: (text: string) => void }) {
  const recordings = useQuery<BrowserRecordings>(RECORDINGS, { pollMs: 30000 });
  const used = (recordings.data?.envs ?? []).reduce((n, e) => n + e.bytes, 0);
  const groups = (recordings.data?.envs ?? []).reduce((n, e) => n + e.groups.length, 0);
  async function deleteAll() {
    if (!(await confirmDialog({ title: t("bs.rec.delete.title"), body: t("bs.rec.delete.body"), action: t("bs.rec.delete"), danger: true }))) return;
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
    <SetGroup title={t("bs.rec.title")} help={t("bs.rec.sub")} className="bs-recording">
      <Row title={t("bs.rec.default")} desc={t("bs.rec.default.sub")} data-setting="browser.record_frames">
        <Switch checked={b.record_frames} onChange={(record_frames) => set({ record_frames })} label={t("bs.rec.default")} />
      </Row>
      <Row title={t("bs.rec.takeover")} desc={t("bs.rec.takeover.sub")} data-setting="browser.record_takeover">
        <Switch checked={b.record_takeover} onChange={(record_takeover) => set({ record_takeover })} label={t("bs.rec.takeover")} />
      </Row>
      <Row title={t("bs.rec.days")} htmlFor="browser-rec-days" data-setting="browser.record_retention_days">
        <WholeInput id="browser-rec-days" value={b.record_retention_days} lo={1} hi={365} unit={t("settings.unit.days")} onSave={(record_retention_days) => set({ record_retention_days })} />
      </Row>
      <Row title={t("bs.rec.mb")} htmlFor="browser-rec-mb" data-setting="browser.record_max_mb" desc={plural("bs.rec.used", groups, { used: size(used), max: size(b.record_max_mb * 1024 * 1024) })}>
        <WholeInput id="browser-rec-mb" value={b.record_max_mb} lo={10} hi={100000} unit={t("settings.unit.mb")} onSave={(record_max_mb) => set({ record_max_mb })} />
      </Row>
      {groups > 0 && (
        <SetFoot className="bs-rec-used">
          <Button size="sm" variant="danger" onClick={() => void deleteAll()}>{t("bs.rec.delete")}</Button>
        </SetFoot>
      )}
    </SetGroup>
  );
}

/** A list of hosts or addresses, one a line: a block of the card with its label over the field. */
function ListBlock({ id, setting, label, hint, rows, placeholder, text, setText, onCommit }: { id: string; setting: string; label: string; hint?: string; rows: number; placeholder?: string; text: string; setText: (text: string) => void; onCommit: () => void }) {
  return (
    <SetBlock data-setting={setting}>
      <label className="field-label" htmlFor={id}>{label}</label>
      <textarea id={id} className="field bs-list" rows={rows} value={text} spellCheck={false} placeholder={placeholder} onChange={(e) => setText(e.target.value)} onBlur={onCommit} />
      {hint && <span className="set-hint">{hint}</span>}
    </SetBlock>
  );
}

function Watch({ b, set }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void }) {
  const [text, setText] = useState(b.watch_domains.join("\n"));
  useEffect(() => setText(b.watch_domains.join("\n")), [b.watch_domains]);
  return (
    <SetGroup title={t("bs.watch.title")} help={t("bs.watch.sub")} className="bs-watch">
      <Row title={t("bs.watch.mode")} data-setting="browser.watch_mode">
        <Switch checked={b.watch_mode} onChange={(watch_mode) => set({ watch_mode })} label={t("bs.watch.mode")} />
      </Row>
      <ListBlock id="browser-watch" setting="browser.watch_domains" label={t("bs.watch.domains")} hint={t("bs.watch.hint")} rows={6} text={text} setText={setText} onCommit={() => {
        const next = lines(text);
        if (next.join("\n") !== b.watch_domains.join("\n")) set({ watch_domains: next });
      }} />
    </SetGroup>
  );
}

function Network({ b, set }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void }) {
  const [text, setText] = useState(b.lan_allow.join("\n"));
  useEffect(() => setText(b.lan_allow.join("\n")), [b.lan_allow]);
  return (
    <>
      <SetGroup title={t("bs.local.title")} help={t("bs.local.sub")} className="bs-network">
        <Row title={t("bs.local.rule")} htmlFor="browser-local-sites" data-setting="browser.local_sites">
          <Select id="browser-local-sites" label={t("bs.local.title")} value={b.local_sites} onChange={(local_sites) => set({ local_sites })} options={[
            { id: "services", label: t("bs.local.services") },
            { id: "ask", label: t("bs.local.ask") },
            { id: "allow", label: t("bs.local.allow") },
          ]} />
        </Row>
      </SetGroup>
      <SetGroup title={t("bs.lan.title")} help={t("bs.lan.sub")}>
        <Row title={t("bs.lan.rule")} htmlFor="browser-lan-sites" data-setting="browser.lan_sites">
          <Select id="browser-lan-sites" label={t("bs.lan.title")} value={b.lan_sites} onChange={(lan_sites) => set({ lan_sites })} options={[
            { id: "ask", label: t("bs.lan.ask") },
            { id: "listed", label: t("bs.lan.listed") },
          ]} />
        </Row>
        <ListBlock id="browser-lan" setting="browser.lan_allow" label={t("bs.lan.label")} rows={3} placeholder="10.0.5.20" text={text} setText={setText} onCommit={() => {
          const next = lines(text);
          if (next.join("\n") !== b.lan_allow.join("\n")) set({ lan_allow: next });
        }} />
      </SetGroup>
    </>
  );
}

function Monitor({ b, set, catalogue }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void; catalogue: ModelCatalogue }) {
  return (
    <SetGroup title={t("bs.monitor.title")} help={t("bs.monitor.sub")} className="bs-monitor">
      <Row title={t("bs.monitor.mode")} data-setting="browser.injection_monitor">
        <Switch checked={b.injection_monitor} onChange={(injection_monitor) => set({ injection_monitor })} label={t("bs.monitor.mode")} />
      </Row>
      <SetBlock data-setting="browser.injection_monitor_preset">
        <label className="field-label" htmlFor="browser-monitor-model">{t("bs.monitor.model")}</label>
        <WithEffort id="browser-monitor-effort" value={b.injection_monitor_effort} onChange={(injection_monitor_effort) => set({ injection_monitor_effort })} fallback="off">
          <ModelPicker id="browser-monitor-model" label={t("bs.monitor.model")} catalogue={catalogue} value={b.injection_monitor_preset} onChange={(injection_monitor_preset) => set({ injection_monitor_preset })} defaultOption={{ label: t("bs.monitor.model.auto") }} />
        </WithEffort>
      </SetBlock>
    </SetGroup>
  );
}

function Reading({ b, set, catalogue }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void; catalogue: ModelCatalogue }) {
  return (
    <SetGroup title={t("bs.agent.title")} help={t("bs.agent.sub")} className="bs-reading">
      <SetBlock data-setting="browser.extract_preset">
        <label className="field-label" htmlFor="browser-extract-model">{t("bs.agent.extract")}</label>
        <WithEffort id="browser-extract-effort" value={b.extract_effort} onChange={(extract_effort) => set({ extract_effort })} fallback="off">
          <ModelPicker id="browser-extract-model" label={t("bs.agent.extract")} catalogue={catalogue} value={b.extract_preset} onChange={(extract_preset) => set({ extract_preset })} defaultOption={{ label: t("bs.monitor.model.auto") }} />
        </WithEffort>
      </SetBlock>
      <Row title={t("bs.agent.point")} desc={t("bs.agent.point.sub")} data-setting="browser.point_clicks">
        <Switch checked={b.point_clicks} onChange={(point_clicks) => set({ point_clicks })} label={t("bs.agent.point")} />
      </Row>
    </SetGroup>
  );
}

/** WebGL in the agent's browser: off until the agent asks and the operator says yes on the approval
 *  card, or until a level is allowed here beforehand. Whether this installation can draw on a GPU is
 *  the daemon's word, shown beside it, because the GPU mode needs the GPU passed into the service. */
function Graphics({ b, set, envs }: { b: BrowserSettings; set: (patch: Partial<BrowserSettings>) => void; envs: BrowserEnv[] }) {
  const reporting = envs.filter((e) => e.available && e.webgl);
  return (
    <SetGroup title={t("bs.webgl.title")} help={t("bs.webgl.sub")} className="bs-webgl">
      <Row title={t("bs.webgl.allow")} htmlFor="browser-webgl-allow" data-setting="browser.webgl_allow">
        <Select
          id="browser-webgl-allow"
          label={t("bs.webgl.allow")}
          value={b.webgl_allow}
          onChange={(webgl_allow) => set({ webgl_allow: webgl_allow as BrowserSettings["webgl_allow"] })}
          options={[
            { id: "ask", label: t("bs.webgl.allow.ask") },
            { id: "software", label: t("bs.webgl.allow.software") },
            { id: "gpu", label: t("bs.webgl.allow.gpu") },
          ]}
        />
      </Row>
      {reporting.map((e) => (
        <Row key={e.env} data-env={e.env} title={`${t("bs.webgl.gpu")}${reporting.length > 1 ? ` · ${t(`bs.env.${e.env === "host" ? "host" : "container"}`)}` : ""}`}>
          <span className={`settings-value ${e.webgl?.gpu ? "" : "bs-warn"}`}>{e.webgl?.gpu ? t("bs.webgl.gpu.yes") : t("bs.webgl.gpu.no", { reason: e.webgl?.gpu_reason ?? "" })}</span>
        </Row>
      ))}
    </SetGroup>
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
    } catch (e) {
      toast(errorText(e));
    }
  }
  async function remove(n: BrowserSiteNote) {
    // A waiting note was never shown to anyone: discarding it needs no second thought.
    if (n.status === "active" && !(await confirmDialog({ title: t("bs.notes.remove.title"), body: t("bs.notes.remove.body", { host: n.host }), action: t("bs.notes.remove"), danger: true }))) return;
    try {
      await api.delete(`${NOTES}/${encodeURIComponent(n.id)}`);
      invalidate(NOTES);
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <SetGroup title={t("bs.notes.title")} help={t("bs.notes.sub")} className="bs-notes" data-setting="browser.notes">
      {list.length === 0 ? (
        <SetBlock><span className="set-hint">{t("bs.notes.none")}</span></SetBlock>
      ) : list.map((n) => n.kind === "procedure" && editing === n.id ? (
        <div key={n.id} className="bs-row bs-note bs-proc" data-note={n.id} data-status={n.status} data-kind={n.kind}>
          <ProcedureEditor draft={{ note: n }} toast={toast} onDone={() => { setEditing(null); invalidate(NOTES); }} />
        </div>
      ) : (
        // A note is a sentence or a page of steps, not one title and one control, so it is a row of its own.
        <div key={n.id} className={`bs-row bs-note ${n.kind === "procedure" ? "bs-proc" : ""}`} data-note={n.id} data-status={n.status} data-kind={n.kind}>
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
            <span className="set-hint truncate">{[...(n.kind === "procedure" ? [t("bs.notes.procedure")] : []), t(n.status === "proposed" ? "bs.notes.waiting" : "bs.notes.active"), n.project || t("bs.notes.everyone"), timeAgo(new Date((n.approved_at || n.proposed_at) * 1000).toISOString())].join(" · ")}</span>
          </span>
          {n.kind === "procedure" && <Button size="sm" onClick={() => setEditing(n.id)}>{t("bs.notes.edit")}</Button>}
          {n.status === "proposed" && <Button size="sm" variant="primary" onClick={() => void approve(n)}>{t("bs.notes.approve")}</Button>}
          <Button size="sm" variant="danger" onClick={() => void remove(n)}>{t(n.status === "proposed" ? "bs.notes.discard" : "bs.notes.remove")}</Button>
        </div>
      ))}
    </SetGroup>
  );
}
