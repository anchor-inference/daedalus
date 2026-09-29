// Settings → Notifications: where each kind of notification goes, when the machine stays quiet,
// which projects are muted, the devices that receive push, and a test through every channel.
//
// Every change is saved at once with the revision it was read at, so two windows cannot overwrite
// each other without noticing: a save the host calls stale reloads the section and says so, the way
// the other settings do. Changes made while a save is in flight are sent after it, as one.

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, telegram } from "../api";
import type { NotificationPreferences, NotificationPreferencesView, NotifyCell, NotifyChannel, Project } from "../api";
import { timeAgo } from "../components";
import { clock, shortDateTime } from "../format";
import { plural, t } from "../i18n";
import { categoryLabel } from "../notifications";
import { CELLS, CHANNELS, MUTE_ENDS, joinQuietHours, muteState, muteUntil, nextCell, preferencesDiff, splitQuietHours, testLines, withCell, withMute } from "../notifyprefs";
import { currentEndpoint, type PushDevice } from "../push";
import { setPopupsShown, usePopupsShown } from "../popups";
import { PushCard } from "../pushui";
import { useMedia } from "../shell";
import { invalidate, useQuery } from "../store";
import { errorText, numInput } from "../ui";

const QUIET_DEFAULT = { from: "23:00", to: "08:00" };

/** A category's name in the matrix: the longer one written for it, else the notification's own. */
function categoryName(category: string): string {
  const key = `nset.cat.${category}`;
  const name = t(key);
  return name === `[${key}]` ? categoryLabel(category) : name;
}

/** The preferences, with one queue of saves in front of the host. */
function usePreferences(toast: (text: string) => void) {
  const [view, setView] = useState<NotificationPreferencesView | null>(null);
  const [error, setError] = useState("");
  const current = useRef<NotificationPreferences | null>(null);
  const saved = useRef<NotificationPreferences | null>(null);
  const revision = useRef("");
  const pending = useRef(false);
  const flushing = useRef(false);

  const load = useCallback(async () => {
    try {
      const fresh = await api.get<NotificationPreferencesView>("/api/notifications/preferences");
      current.current = fresh.preferences;
      saved.current = fresh.preferences;
      revision.current = fresh.revision;
      setView(fresh);
      setError("");
    } catch (e) {
      setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const flush = useCallback(async () => {
    if (flushing.current) return;
    flushing.current = true;
    try {
      while (pending.current && current.current && saved.current) {
        pending.current = false;
        const next = current.current;
        if (preferencesDiff(saved.current, next).length === 0) continue;
        try {
          const answer = await api.put<NotificationPreferencesView>("/api/notifications/preferences", { preferences: next, base_revision: revision.current });
          revision.current = answer.revision;
          saved.current = answer.preferences;
          // A change made while this one travelled stays on the page and goes out next.
          if (!pending.current) current.current = answer.preferences;
          setView({ ...answer, preferences: current.current ?? answer.preferences });
        } catch (e) {
          pending.current = false;
          await load();
          toast(e instanceof ApiError && e.status === 409 ? t("settings.validation.stale") : errorText(e));
        }
      }
    } finally {
      flushing.current = false;
    }
  }, [load, toast]);

  const change = useCallback(
    (edit: (prefs: NotificationPreferences) => NotificationPreferences) => {
      if (!current.current) return;
      current.current = edit(current.current);
      setView((v) => (v && current.current ? { ...v, preferences: current.current } : v));
      pending.current = true;
      void flush();
    },
    [flush],
  );

  return { view, error, change };
}

function CellButton({ cell, label, onClick }: { cell: NotifyCell; label: string; onClick: () => void }) {
  return (
    <button className="ncell" data-cell={cell} aria-label={t("nset.cell.aria", { what: label, state: t(`nset.cell.${cell}.long`) })} title={t(`nset.cell.${cell}.long`)} onClick={onClick}>
      {t(`nset.cell.${cell}`)}
    </button>
  );
}

function Matrix({ prefs, categories, onCell }: { prefs: NotificationPreferences; categories: string[]; onCell: (category: string, channel: NotifyChannel, cell: NotifyCell) => void }) {
  const wide = useMedia("(min-width: 1024px)");
  const [open, setOpen] = useState<string | null>(null);
  const cellOf = (category: string, channel: NotifyChannel): NotifyCell => prefs.matrix[category]?.[channel] ?? "off";
  const legend = (
    <div className="nmatrix-legend sub">
      {CELLS.map((c) => (
        <span key={c}>
          <span className="ncell static" data-cell={c}>{t(`nset.cell.${c}`)}</span> {t(`nset.legend.${c}`)}
        </span>
      ))}
    </div>
  );
  if (wide) {
    return (
      <>
        <table className="nmatrix">
          <thead>
            <tr>
              <th scope="col">{t("nset.matrix.kind")}</th>
              {CHANNELS.map((ch) => <th key={ch} scope="col">{t(`nset.channel.${ch}`)}</th>)}
            </tr>
          </thead>
          <tbody>
            {categories.map((category) => (
              <tr key={category} data-category={category}>
                <th scope="row">{categoryName(category)}</th>
                {CHANNELS.map((ch) => (
                  <td key={ch} data-channel={ch}>
                    <CellButton cell={cellOf(category, ch)} label={`${categoryName(category)} · ${t(`nset.channel.${ch}`)}`} onClick={() => onCell(category, ch, nextCell(cellOf(category, ch)))} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {legend}
      </>
    );
  }
  // A phone has no room for four columns: each kind is a row that opens into one line per channel.
  return (
    <>
      <div className="nrows">
        {categories.map((category) => {
          const on = CHANNELS.filter((ch) => cellOf(category, ch) !== "off").map((ch) => t(`nset.channel.${ch}`) + (cellOf(category, ch) === "urgent" ? ` (${t("nset.cell.urgent").toLowerCase()})` : ""));
          const expanded = open === category;
          return (
            <div key={category} className={`nrow ${expanded ? "open" : ""}`} data-category={category}>
              <button className="nrow-head" aria-expanded={expanded} onClick={() => setOpen(expanded ? null : category)}>
                <span className="nrow-text">
                  <b>{categoryName(category)}</b>
                  <span className="sub">{on.length ? on.join(" · ") : t("nset.row.none")}</span>
                </span>
                <span className="chev">›</span>
              </button>
              {expanded && CHANNELS.map((ch) => (
                <div key={ch} className="nrow-line" data-channel={ch}>
                  <span>{t(`nset.channel.${ch}`)}</span>
                  <div className="segmented inline" role="radiogroup" aria-label={`${categoryName(category)} · ${t(`nset.channel.${ch}`)}`}>
                    {CELLS.map((c) => (
                      <button key={c} role="radio" aria-checked={cellOf(category, ch) === c} className={cellOf(category, ch) === c ? "on" : ""} onClick={() => onCell(category, ch, c)}>
                        {/* The short word, as the matrix pill and the legend name the same state; the
                            long "Urgent only" stays in the pill's title and the aria label. */}
                        {t(`nset.cell.${c}`)}
                      </button>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          );
        })}
      </div>
      {legend}
    </>
  );
}

function QuietHours({ prefs, zone, change }: { prefs: NotificationPreferences; zone: string; change: (edit: (p: NotificationPreferences) => NotificationPreferences) => void }) {
  const spec = prefs.quiet_hours;
  const ends = splitQuietHours(spec);
  const [draft, setDraft] = useState(ends ?? QUIET_DEFAULT);
  useEffect(() => {
    const saved = splitQuietHours(spec);
    if (saved) setDraft(saved);
  }, [spec]);
  const same = draft.from === draft.to;
  function set(part: "from" | "to", value: string) {
    const next = { ...draft, [part]: value };
    setDraft(next);
    const joined = joinQuietHours(next.from, next.to);
    if (joined) change((p) => ({ ...p, quiet_hours: joined }));
  }
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("nset.quiet.title")}</div>
      <div className="sub">{t("nset.quiet.sub")}</div>
      <div className="btnrow">
        <button className={`btn small ${ends ? "primary" : ""}`} aria-pressed={!!ends} onClick={() => change((p) => ({ ...p, quiet_hours: ends ? "" : joinQuietHours(draft.from, draft.to) || `${QUIET_DEFAULT.from}-${QUIET_DEFAULT.to}` }))}>
          {t("nset.quiet.toggle", { state: t(ends ? "common.on" : "common.off") })}
        </button>
      </div>
      {ends && (
        <>
          <div className="quiet-hours">
            <label>
              <span className="sub">{t("nset.quiet.from")}</span>
              <input className="field" type="time" value={draft.from} onChange={(e) => set("from", e.target.value)} />
            </label>
            <label>
              <span className="sub">{t("nset.quiet.to")}</span>
              <input className="field" type="time" value={draft.to} onChange={(e) => set("to", e.target.value)} />
            </label>
          </div>
          {same && <div className="sub push-error">{t("nset.quiet.same")}</div>}
          <div className="sub">{t("nset.quiet.urgent")}</div>
        </>
      )}
      <div className="sub faint">{zone ? t("nset.quiet.zone", { zone }) : t("nset.quiet.nozone")}</div>
      <label className="field" htmlFor="finished-after">{t("nset.finished.label")}</label>
      <div className="terminal-cap-row">
        <input
          id="finished-after"
          className="field terminal-cap-input"
          type="number"
          min={0}
          step={5}
          defaultValue={prefs.finished_min_seconds}
          key={prefs.finished_min_seconds}
          onBlur={(e) => {
            const v = numInput(e.target.value, 0);
            if (v !== null && Math.round(v) !== prefs.finished_min_seconds) change((p) => ({ ...p, finished_min_seconds: Math.round(v) }));
          }}
        />
        <span className="sub">{t("nset.finished.unit")}</span>
      </div>
      <div className="sub">{t("nset.finished.sub")}</div>
    </div>
  );
}

function Answering({ prefs, change }: { prefs: NotificationPreferences; change: (edit: (p: NotificationPreferences) => NotificationPreferences) => void }) {
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("nset.answer.title")}</div>
      <div className="btnrow">
        <button className={`btn small ${prefs.quick_actions ? "primary" : ""}`} aria-pressed={prefs.quick_actions} onClick={() => change((p) => ({ ...p, quick_actions: !p.quick_actions }))}>
          {t("nset.quick", { state: t(prefs.quick_actions ? "common.on" : "common.off") })}
        </button>
      </div>
      <div className="sub">{t("nset.quick.sub")}</div>
      <div className="btnrow">
        <button className={`btn small ${prefs.telegram_covers_push ? "primary" : ""}`} aria-pressed={prefs.telegram_covers_push} onClick={() => change((p) => ({ ...p, telegram_covers_push: !p.telegram_covers_push }))}>
          {t("nset.cover", { state: t(prefs.telegram_covers_push ? "common.on" : "common.off") })}
        </button>
      </div>
      <div className="sub">{t("nset.cover.sub")}</div>
    </div>
  );
}

/** The one switch that lives on this device rather than with the host (see `popups.ts`), so it is
 *  there even before the host's preferences have loaded. */
function PopupsCard() {
  const on = usePopupsShown();
  return (
    <div className="card" data-popups-card>
      <div className="section-title" style={{ marginTop: 0 }}>{t("nset.popups.title")}</div>
      <div className="btnrow">
        <button className={`btn small ${on ? "primary" : ""}`} aria-pressed={on} onClick={() => setPopupsShown(!on)}>
          {t("nset.popups", { state: t(on ? "common.on" : "common.off") })}
        </button>
      </div>
      <div className="sub">{t("nset.popups.sub")}</div>
    </div>
  );
}

/** When a mute ends, in words: a time today, a date and time otherwise. */
function muteEnd(until: Date, now: Date): string {
  return until.toDateString() === now.toDateString() ? clock(until) : shortDateTime(until);
}

function MutedProjects({ prefs, change }: { prefs: NotificationPreferences; change: (edit: (p: NotificationPreferences) => NotificationPreferences) => void }) {
  const projects = useQuery<Project[]>("/api/projects", { staleMs: 20000 });
  // Re-rendered each minute so a mute that runs out is shown as ended without a reload.
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 60000);
    return () => window.clearInterval(timer);
  }, []);
  const list = (projects.data ?? []).filter((p) => !p.system);
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("nset.mute.title")}</div>
      <div className="sub">{t("nset.mute.sub")}</div>
      {projects.data && list.length === 0 && <div className="sub" style={{ marginTop: 8 }}>{t("nset.mute.none")}</div>}
      <div className="mlist">
        {list.map((project) => {
          const state = muteState(prefs, project.id, now);
          return (
            <div key={project.id} className="mrow nmute" data-project={project.id} data-muted={state.muted}>
              <div className="mline noradio">
                <div className="mmain">
                  <span className="mtitle">{project.name}</span>
                  <span className="mmeta">{state.muted ? (state.until ? t("nset.mute.until", { time: muteEnd(state.until, now) }) : t("nset.mute.forever")) : t("nset.mute.off")}</span>
                </div>
                <div className="mactions">
                  {state.muted ? (
                    <button className="btn small" onClick={() => change((p) => withMute(p, project.id, null, new Date()))}>{t("nset.mute.unmute")}</button>
                  ) : (
                    <div className="segmented inline" role="group" aria-label={t("nset.mute.group", { name: project.name })}>
                      {MUTE_ENDS.map((end) => (
                        <button key={end} onClick={() => change((p) => withMute(p, project.id, muteUntil(end, new Date()), new Date()))}>{t(`nset.mute.${end}`)}</button>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** "Chrome on Android …" cut to what tells two devices apart. */
function shortAgent(ua: string): string {
  const cut = ua.replace(/^Mozilla\/5\.0\s*/, "").replace(/\s*AppleWebKit.*$/, "");
  return cut.length > 60 ? `${cut.slice(0, 59)}…` : cut;
}

function Devices({ toast }: { toast: (text: string) => void }) {
  const devices = useQuery<{ subscriptions: PushDevice[] }>("/api/push/subscriptions");
  const [mine, setMine] = useState<string | null>(null);
  useEffect(() => {
    void currentEndpoint().then(setMine);
  }, [devices.data]);
  async function remove(device: PushDevice) {
    try {
      await api.delete(`/api/push/subscriptions/${device.id}`);
      toast(t("nset.devices.removed"));
    } catch (e) {
      toast(errorText(e));
    } finally {
      invalidate("/api/push/subscriptions");
    }
  }
  const list = devices.data?.subscriptions ?? [];
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("nset.devices.title")}</div>
      <div className="sub">{t("nset.devices.sub")}</div>
      {devices.error && <div className="sub push-error">{devices.error}</div>}
      {devices.data && list.length === 0 && <div className="sub" style={{ marginTop: 8 }}>{t("push.test.nodevice")}</div>}
      <div className="mlist">
        {list.map((d) => {
          const own = mine !== null && d.endpoint === mine;
          return (
            <div key={d.id} className="mrow ndevice" data-device={d.id}>
              <div className="mline noradio">
                <div className="mmain">
                  <span className="mtitle">
                    {d.device || shortAgent(d.user_agent) || t("nset.devices.unnamed")}
                    {own && <span className="chip accent">{t("push.device")}</span>}
                  </span>
                  <span className="mmeta" title={d.user_agent}>
                    {[
                      d.last_ok_at ? t("nset.devices.lastok", { t: timeAgo(d.last_ok_at) }) : t("nset.devices.never"),
                      d.failures > 0 ? plural("nset.devices.failures", d.failures) : "",
                      shortAgent(d.user_agent),
                    ].filter(Boolean).join(" · ")}
                  </span>
                  {d.failures > 0 && d.last_error && <span className="mmeta push-error">{d.last_error}</span>}
                </div>
                <div className="mactions">
                  {own ? (
                    <span className="sub">{t("nset.devices.thishint")}</span>
                  ) : (
                    <button className="btn small" onClick={() => void remove(d)}>{t("common.remove")}</button>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function TestCard({ toast }: { toast: (text: string) => void }) {
  const [testing, setTesting] = useState(false);
  const [lines, setLines] = useState<string[] | null>(null);
  async function test() {
    setTesting(true);
    try {
      const r = await api.post<{ delivered: Record<string, unknown> }>("/api/notifications/test");
      setLines(testLines(r.delivered));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setTesting(false);
    }
  }
  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("push.test")}</div>
      <div className="sub">{t("push.test.sub")}</div>
      <div className="btnrow">
        <button className="btn small" disabled={testing} onClick={() => void test()}>{t("push.test.send")}</button>
      </div>
      {lines && (
        <ul className="push-test-result">
          {lines.map((line, i) => <li key={i}>{line}</li>)}
        </ul>
      )}
    </div>
  );
}

export function NotificationSettings({ toast }: { toast: (text: string) => void }) {
  const { view, error, change } = usePreferences(toast);
  return (
    <>
      <PushCard />
      <PopupsCard />
      {view ? (
        <>
          <div className="card nmatrix-card">
            <div className="section-title" style={{ marginTop: 0 }}>{t("nset.matrix.title")}</div>
            <div className="sub">{t("nset.matrix.sub")}</div>
            <Matrix prefs={view.preferences} categories={view.categories} onCell={(category, channel, cell) => change((p) => withCell(p, category, channel, cell))} />
          </div>
          <QuietHours prefs={view.preferences} zone={view.zone} change={change} />
          <Answering prefs={view.preferences} change={change} />
          <MutedProjects prefs={view.preferences} change={change} />
        </>
      ) : (
        <div className="card"><div className="sub">{error || t("common.loading")}</div></div>
      )}
      {/* Inside Telegram the bot is the push, and the app asks the host nothing about push there. */}
      {!telegram()?.initData && <Devices toast={toast} />}
      <TestCard toast={toast} />
    </>
  );
}
