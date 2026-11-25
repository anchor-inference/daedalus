// Settings → Notifications: where each kind of notification goes, when the machine stays quiet,
// which projects are muted, the devices that receive push, and a test through every channel.
//
// Every change is saved at once with the revision it was read at, so two windows cannot overwrite
// each other without noticing: a save the host calls stale reloads the section and says so, the way
// the other settings do. Changes made while a save is in flight are sent after it, as one.

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api, telegram } from "../api";
import type { NotificationPreferences, NotificationPreferencesView, NotifyCell, NotifyChannel, Project } from "../api";
import { timeAgo } from "../ui/components";
import { NumInput, Row, SetBlock, SetGroup } from "../settingsrow";
import { clock, shortDateTime } from "../format";
import { plural, t } from "../i18n";
import { categoryLabel } from "../notifications";
import { CELLS, CHANNELS, MUTE_ENDS, joinQuietHours, muteState, muteUntil, nextCell, preferencesDiff, splitQuietHours, testLines, withCell, withMute } from "../notifyprefs";
import { currentEndpoint, type PushDevice } from "../push";
import { setPopupsShown, usePopupsShown } from "../popups";
import { PushCard } from "../pushui";
import { useMedia } from "../ui/index";
import { Badge, Button, Segmented, Select, Switch, confirmDialog } from "../ui/primitives";
import { Icon } from "../icons";
import { invalidate, useQuery } from "../store";
import { errorText } from "../ui";

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
    <SetBlock className="nmatrix-legend">
      {CELLS.map((c) => (
        <span key={c} className="set-hint">
          <span className="ncell static" data-cell={c}>{t(`nset.cell.${c}`)}</span> {t(`nset.legend.${c}`)}
        </span>
      ))}
    </SetBlock>
  );
  if (wide) {
    return (
      <>
        <SetBlock className="nmatrix-block">
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
        </SetBlock>
        {legend}
      </>
    );
  }
  // A phone has no room for four columns: each kind is a row that opens into one line per channel.
  return (
    <>
      <SetBlock className="nmatrix-block">
        <div className="nrows">
          {categories.map((category) => {
            const on = CHANNELS.filter((ch) => cellOf(category, ch) !== "off").map((ch) => t(`nset.channel.${ch}`) + (cellOf(category, ch) === "urgent" ? ` (${t("nset.cell.urgent").toLowerCase()})` : ""));
            const expanded = open === category;
            return (
              <div key={category} className={`nrow ${expanded ? "open" : ""}`} data-category={category}>
                <button className="nrow-head" aria-expanded={expanded} onClick={() => setOpen(expanded ? null : category)}>
                  <span className="nrow-text">
                    <b>{categoryName(category)}</b>
                    <span className="set-hint">{on.length ? on.join(" · ") : t("nset.row.none")}</span>
                  </span>
                  <Icon name="chevron" size={14} />
                </button>
                {expanded && CHANNELS.map((ch) => (
                  <div key={ch} className="nrow-line" data-channel={ch}>
                    <span>{t(`nset.channel.${ch}`)}</span>
                    {/* The short word, as the matrix pill and the legend name the same state; the long
                        "Urgent only" stays in the pill's title and the aria label. */}
                    <Segmented label={`${categoryName(category)} · ${t(`nset.channel.${ch}`)}`} value={cellOf(category, ch)} onChange={(c) => onCell(category, ch, c)} options={CELLS.map((c) => ({ id: c, label: t(`nset.cell.${c}`) }))} />
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      </SetBlock>
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
    <SetGroup title={t("nset.quiet.title")} help={t("nset.quiet.sub")}>
      <Row title={t("nset.quiet.toggle")} data-setting="notifications.quiet_hours" desc={<>{ends ? `${t("nset.quiet.urgent")} ` : ""}{zone ? t("nset.quiet.zone", { zone }) : t("nset.quiet.nozone")}</>}>
        <Switch checked={!!ends} onChange={(on) => change((p) => ({ ...p, quiet_hours: on ? joinQuietHours(draft.from, draft.to) || `${QUIET_DEFAULT.from}-${QUIET_DEFAULT.to}` : "" }))} label={t("nset.quiet.toggle")} />
      </Row>
      {ends && (
        <Row title={t("nset.quiet.when")} desc={same ? <span className="push-error">{t("nset.quiet.same")}</span> : undefined}>
          <span className="quiet-hours">
            <label>
              <span className="set-hint">{t("nset.quiet.from")}</span>
              <input className="field" type="time" value={draft.from} onChange={(e) => set("from", e.target.value)} />
            </label>
            <label>
              <span className="set-hint">{t("nset.quiet.to")}</span>
              <input className="field" type="time" value={draft.to} onChange={(e) => set("to", e.target.value)} />
            </label>
          </span>
        </Row>
      )}
      <Row title={t("nset.finished.label")} htmlFor="finished-after" desc={t("nset.finished.sub")} stack data-setting="notifications.finished_min_seconds">
        <NumInput id="finished-after" label={t("nset.finished.label")} value={prefs.finished_min_seconds} min={0} step={5} unit={t("nset.finished.unit")} onSave={(v) => Math.round(v) !== prefs.finished_min_seconds && change((p) => ({ ...p, finished_min_seconds: Math.round(v) }))} />
      </Row>
    </SetGroup>
  );
}

function Answering({ prefs, change }: { prefs: NotificationPreferences; change: (edit: (p: NotificationPreferences) => NotificationPreferences) => void }) {
  return (
    <SetGroup title={t("nset.answer.title")}>
      <Row title={t("nset.quick")} desc={t("nset.quick.sub")} data-setting="notifications.quick_actions">
        <Switch checked={prefs.quick_actions} onChange={(quick_actions) => change((p) => ({ ...p, quick_actions }))} label={t("nset.quick")} />
      </Row>
      <Row title={t("nset.cover")} desc={t("nset.cover.sub")} data-setting="notifications.telegram_covers_push">
        <Switch checked={prefs.telegram_covers_push} onChange={(telegram_covers_push) => change((p) => ({ ...p, telegram_covers_push }))} label={t("nset.cover")} />
      </Row>
    </SetGroup>
  );
}

/** The one switch that lives on this device rather than with the host (see `popups.ts`), so it is
 *  there even before the host's preferences have loaded. */
function PopupsCard() {
  const on = usePopupsShown();
  return (
    <SetGroup title={t("nset.popups.title")} data-popups-card="">
      <Row title={t("nset.popups")} desc={t("nset.popups.sub")} data-setting="notifications.popups">
        <Switch checked={on} onChange={setPopupsShown} label={t("nset.popups")} />
      </Row>
    </SetGroup>
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
  type Choice = "on" | "muted" | (typeof MUTE_ENDS)[number];
  const mute = (projectId: string, choice: Choice) => {
    if (choice === "muted") return;
    change((p) => withMute(p, projectId, choice === "on" ? null : muteUntil(choice, new Date()), new Date()));
  };
  return (
    <SetGroup title={t("nset.mute.title")} help={t("nset.mute.sub")} data-setting="notifications.muted_projects">
      {projects.data && list.length === 0 && <SetBlock><span className="set-hint">{t("nset.mute.none")}</span></SetBlock>}
      {list.map((project) => {
        const state = muteState(prefs, project.id, now);
        const muted = state.muted ? (state.until ? t("nset.mute.until", { time: muteEnd(state.until, now) }) : t("nset.mute.forever")) : t("nset.mute.off");
        return (
          <Row key={project.id} className="nmute" title={project.name} data-project={project.id} data-muted={String(state.muted)}>
            <Select<Choice>
              className="set-value"
              label={t("nset.mute.group", { name: project.name })}
              value={state.muted ? "muted" : "on"}
              onChange={(choice) => mute(project.id, choice)}
              options={[
                state.muted ? { id: "muted" as const, label: muted, disabled: true } : { id: "on" as const, label: muted },
                ...(state.muted ? [{ id: "on" as const, label: t("nset.mute.unmute") }] : []),
                ...MUTE_ENDS.map((end) => ({ id: end, label: t(`nset.mute.set.${end}`) })),
              ]}
            />
          </Row>
        );
      })}
    </SetGroup>
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
    const name = device.device || shortAgent(device.user_agent) || t("nset.devices.unnamed");
    if (!(await confirmDialog({ title: t("nset.devices.remove.title", { name }), body: t("nset.devices.remove.body"), action: t("common.remove"), danger: true }))) return;
    try {
      await api.delete(`/api/push/subscriptions/${device.id}`);
    } catch (e) {
      toast(errorText(e));
    } finally {
      invalidate("/api/push/subscriptions");
    }
  }
  const list = devices.data?.subscriptions ?? [];
  return (
    <SetGroup title={t("nset.devices.title")} help={t("nset.devices.sub")} data-setting="notifications.devices">
      {devices.error && <SetBlock><span className="push-error">{devices.error}</span></SetBlock>}
      {devices.data && list.length === 0 && <SetBlock><span className="set-hint">{t("push.test.nodevice")}</span></SetBlock>}
      {list.map((d) => {
        const own = mine !== null && d.endpoint === mine;
        return (
          <Row
            key={d.id}
            className="ndevice"
            data-device={String(d.id)}
            title={<span className="mtitle">{d.device || shortAgent(d.user_agent) || t("nset.devices.unnamed")}{own && <Badge tone="accent">{t("push.device")}</Badge>}</span>}
            desc={<>
              <span title={d.user_agent}>
                {[
                  d.last_ok_at ? t("nset.devices.lastok", { t: timeAgo(d.last_ok_at) }) : t("nset.devices.never"),
                  d.failures > 0 ? plural("nset.devices.failures", d.failures) : "",
                  shortAgent(d.user_agent),
                ].filter(Boolean).join(" · ")}
              </span>
              {d.failures > 0 && d.last_error && <span className="push-error"> {d.last_error}</span>}
            </>}
          >
            {own ? <span className="set-hint">{t("nset.devices.thishint")}</span> : <Button size="sm" onClick={() => void remove(d)}>{t("common.remove")}</Button>}
          </Row>
        );
      })}
    </SetGroup>
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
    <SetGroup data-setting="notifications.test">
      <Row title={t("push.test")} desc={t("push.test.sub")}>
        <Button size="sm" busy={testing} onClick={() => void test()}>{t("push.test.send")}</Button>
      </Row>
      {lines && (
        <SetBlock>
          <ul className="push-test-result">
            {lines.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        </SetBlock>
      )}
    </SetGroup>
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
          <SetGroup title={t("nset.matrix.title")} help={t("nset.matrix.sub")} data-setting="notifications.matrix">
            <Matrix prefs={view.preferences} categories={view.categories} onCell={(category, channel, cell) => change((p) => withCell(p, category, channel, cell))} />
          </SetGroup>
          <QuietHours prefs={view.preferences} zone={view.zone} change={change} />
          <Answering prefs={view.preferences} change={change} />
          <MutedProjects prefs={view.preferences} change={change} />
        </>
      ) : (
        <SetGroup><SetBlock><span className="set-hint">{error || t("common.loading")}</span></SetBlock></SetGroup>
      )}
      {/* Inside Telegram the bot is the push, and the app asks the host nothing about push there. */}
      {!telegram()?.initData && <Devices toast={toast} />}
      <TestCard toast={toast} />
    </>
  );
}
