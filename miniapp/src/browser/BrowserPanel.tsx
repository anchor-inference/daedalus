// The Browser tab of the right panel (a sheet on a phone, a page of its own at /app/browser/<group>):
// the agent's browser, live, with its cursor; the address and the tabs; the one button that decides who
// drives; and the log of what was done.
//
// Who drives is the whole of the design. While the agent does, the page is a picture: the address is
// read-only, the history buttons are off, and a click on the picture does nothing but say how to take
// over. "Take control" hands this window the page — the agent's calls then wait, and it sees nothing of
// the page while a person types — and "Give back" returns it with a note the agent is woken with.

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { BrowserActionRow, BrowserFrame, BrowserGroup } from "../api";
import { OverflowMenu, Popover, type MenuItem } from "../dialogs";
import { clock } from "../format";
import { plural, t } from "../i18n";
import { Icon, type IconName } from "../icons";
import { navigate, pathFor } from "../router";
import { confirmAsync, errorText, haptic } from "../ui";
import { answerDialog, closeBrowser, consumeTake, deleteRecording, deviceSaving, recordingMoved, resizeViewport, setControl, setRecording, takeHandoff, useActions, useLiveSnapshot, useLiveView, useRecording } from "./data";
import { frameOfRow, ReplayStage } from "./replay";
import type { LiveSnapshot, LiveView } from "./live";
import { actionWords, agentName, domainOf, driveState, mergeActions, needsOf, needWords, rowOfEvent, secure, type DriveState } from "./model";
import type { ActionEvent } from "./protocol";
import { BrowserViewer, focusViewer } from "./viewer";
import { PhoneDrive } from "./phone";
import { Favicon } from "./favicon";
import { attachBox } from "./geometry";
import type { ViewerChord } from "./keys";

/** The tab's body: the session's groups, the busiest one shown (a session rarely has more than one). */
export function BrowserTab({ groups, toast, phone }: { groups: BrowserGroup[]; toast: (text: string) => void; phone?: boolean }) {
  const open = groups.filter((g) => g.status !== "closed" && g.status !== "lost");
  const [chosen, setChosen] = useState<string | null>(null);
  const group = open.find((g) => g.id === chosen) ?? open[0] ?? null;
  if (!group) {
    return (
      <div className="bp-empty">
        <Icon name="globe" size={22} />
        <b>{t("browser.none.title")}</b>
        <span>{t("browser.none.body")}</span>
      </div>
    );
  }
  return <BrowserPanel key={group.id} group={group} groups={open} onGroup={setChosen} toast={toast} phone={phone} />;
}

type PanelProps = {
  group: BrowserGroup;
  groups?: BrowserGroup[];
  onGroup?: (id: string) => void;
  toast: (text: string) => void;
  phone?: boolean;
  /** The page of its own: no panel around it. The log sits beside the picture once it is opened. */
  full?: boolean;
}

const PAGE_MIN = 320;
const PAGE_MAX = 3840;
/** A resize settles, including the handoff that grows the corner card into this picture. */
const FILL_QUIET_MS = 240;

/**
 * The desktop picture asks the page to become the box it is drawn in. A phone does not: fitting its
 * sheet would reflow the page the agent is reading every time the sheet opens. Below a few pixels
 * the tab is hidden. The daemon's own bounds are 320–3840; a box outside them is clamped, and a
 * refusal leaves the letterbox rather than taking the tab down.
 */
function useFillPage(group: string, stage: { current: HTMLDivElement | null }, enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    const el = stage.current;
    if (!el) return;
    let last = "";
    let timer = 0;
    const send = () => {
      const rawW = Math.round(el.clientWidth);
      const rawH = Math.round(el.clientHeight);
      if (rawW < 64 || rawH < 64) return;
      const w = Math.min(PAGE_MAX, Math.max(PAGE_MIN, rawW));
      const h = Math.min(PAGE_MAX, Math.max(PAGE_MIN, rawH));
      const key = `${w}x${h}`;
      if (key === last) return;
      last = key;
      void resizeViewport(group, w, h).catch(() => {
        last = "";
      });
    };
    const watched = new ResizeObserver(() => {
      window.clearTimeout(timer);
      timer = window.setTimeout(send, FILL_QUIET_MS);
    });
    watched.observe(el);
    timer = window.setTimeout(send, FILL_QUIET_MS);
    return () => {
      window.clearTimeout(timer);
      watched.disconnect();
    };
  }, [enabled, group, stage]);
}

export function BrowserPanel({ group, groups = [group], onGroup, toast, phone = false, full = false }: PanelProps) {
  const saving = useMemo(() => deviceSaving(phone), [phone]);
  const stage = useRef<HTMLDivElement | null>(null);
  const live = useLiveView(group.id, "live", {
    box: () => {
      const el = stage.current;
      return attachBox(el?.clientWidth || 800, el?.clientHeight || 500, window.devicePixelRatio || 1, saving);
    },
  });
  const snap = useLiveSnapshot(live);
  const control = useControl(group, live, snap, toast);
  const agent = agentName(group);
  const drive = control.drive;
  const needs = needsOf(group.needs_you, snap);
  const viewing = snap.tabs.find((tab) => tab.id === (snap.viewing ?? snap.active)) ?? group.tabs.find((tab) => tab.active) ?? group.tabs[0] ?? null;
  const url = viewing?.url ?? "";
  const [logOpen, setLogOpen] = useState(false);
  useFillPage(group.id, stage, !phone);
  const recording = useRecording(group.id);
  const listed = useActions(group.id);
  // The replay frames an action's element from its row: the listing's, or the live event's while the
  // listing has not been read again since.
  const rows = useMemo(() => mergeActions(listed, snap.recent.map(rowOfEvent)), [listed, snap.recent]);
  /** The keyframe on screen in place of the live picture, or null: live. */
  const [replay, setReplay] = useState<number | null>(null);
  const frames = recording.frames;
  const replaying = replay !== null && frames.length > 0;
  // While recording, every action the live view reports has a keyframe coming: its row gets it.
  const latestAction = snap.recent[0]?.id;
  const recordingOn = recording.recording.frames;
  useEffect(() => {
    if (latestAction && recordingOn) recordingMoved(group.id);
  }, [latestAction, recordingOn, group.id]);

  // The corner preview grows into this picture: the stage starts where the card was and settles here.
  useEffect(() => {
    const from = takeHandoff(group.id);
    const el = stage.current;
    if (!from || !el || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const to = el.getBoundingClientRect();
    if (!to.width || !to.height) return;
    el.animate(
      [
        { transformOrigin: "top left", transform: `translate(${from.left - to.left}px, ${from.top - to.top}px) scale(${from.width / to.width}, ${from.height / to.height})`, opacity: 0.6, borderRadius: "12px" },
        { transformOrigin: "top left", transform: "none", opacity: 1, borderRadius: "0px" },
      ],
      { duration: 240, easing: "cubic-bezier(.2, .9, .3, 1)" },
    );
  }, [group.id]);

  // "Take control" pressed on the corner card: done here, once this view has a client id to name.
  useEffect(() => {
    if (snap.clientId && consumeTake(group.id)) void control.take();
  }, [snap.clientId, group.id, control]);

  // ⌘⇧K / Ctrl+Shift+K takes the page or gives it back, from anywhere while the tab is open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || !e.shiftKey || e.altKey || e.code !== "KeyK") return;
      e.preventDefault();
      if (drive === "you") void control.giveBack("");
      else void control.take();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drive, control]);

  const onChord = useCallback((chord: ViewerChord) => {
    if (chord === "address") stage.current?.closest(".bp")?.querySelector<HTMLInputElement>(".bp-address input")?.focus();
    else if (chord === "reload" || chord === "back" || chord === "forward") live?.input({ t: "nav", action: chord });
  }, [live]);

  const tabs = snap.tabs.length ? snap.tabs : group.tabs;
  const viewer = (
    <div className="bp-stage-wrap">
      <Banner drive={drive} agent={agent} needs={needs} state={snap.state.kind} control={control} viewers={snap.viewers} bare={phone} />
      <BrowserViewer
        live={live}
        snap={snap}
        tier="live"
        interactive={drive === "you" && !phone}
        agent={agent}
        saving={saving}
        onChord={onChord}
        stageRef={(el) => { stage.current = el; }}
        className="bp-viewer"
        style={phone ? { aspectRatio: `${group.viewport.w} / ${group.viewport.h}` } : undefined}
      >
        {drive !== "you" && !phone && snap.painted && (
          // A click on the picture must not take the page from the agent by accident: it points at the
          // button that does, and says so.
          <div className="bp-veil" onClick={(e) => pulse(e.currentTarget.closest(".bp"))}>
            <span className="bp-veil-hint"><Icon name="user" size={14} /> {t("browser.take.hint")}</span>
          </div>
        )}
      </BrowserViewer>
      {snap.dialog && <DialogChip group={group.id} tab={viewing?.id ?? null} dialog={snap.dialog} canAnswer={drive === "you"} toast={toast} />}
      {replaying && (
        <ReplayStage group={group.id} viewportW={group.viewport.w} frames={frames} index={replay} rows={rows} agent={agent} onIndex={setReplay} onLive={() => setReplay(null)} />
      )}
    </div>
  );

  return (
    <div className={`bp ${phone ? "phone" : ""} ${full ? "full" : ""} ${logOpen ? "log-open" : ""}`} data-group={group.id} data-drive={drive}>
      {phone ? (
        <PhoneBar group={group} url={url} tabs={tabs.length} control={control} />
      ) : (
        <Toolbar group={group} groups={groups} onGroup={onGroup} live={live} snap={snap} url={url} tabs={tabs} viewing={viewing?.id ?? null} drive={drive} control={control} toast={toast} full={full}
          recording={recording.recording.frames} frames={frames} onReplay={() => setReplay(frames.length - 1)} />
      )}
      <div className="bp-main">
        {viewer}
        {/* A phone's sheet is taller than the page's picture: the log fills what is left under it. */}
        <ActionLog group={group.id} recent={snap.recent} open={phone || logOpen} onToggle={() => setLogOpen((o) => !o)} onFocus={(a) => live?.focus(a)} agent={agent} page={phone}
          frames={frames} onReplay={(i) => setReplay(i)} />
      </div>
      {phone && drive === "you" && live && (
        createPortal(<PhoneDrive group={group} live={live} snap={snap} agent={agent} url={url} saving={saving} onGiveBack={(note) => void control.giveBack(note)} />, document.body)
      )}
    </div>
  );
}

/** Draw the eye to the button that takes the page. */
function pulse(root: Element | null): void {
  const button = root?.querySelector<HTMLElement>(".bp-control");
  if (!button) return;
  button.classList.remove("pulse");
  void button.offsetWidth;
  button.classList.add("pulse");
  window.setTimeout(() => button.classList.remove("pulse"), 900);
}

// ── who drives ───────────────────────────────────────────────────────────────────────────────

export type ControlApi = {
  drive: DriveState;
  busy: boolean;
  take: () => Promise<void>;
  giveBack: (note: string) => Promise<void>;
  pause: () => Promise<void>;
  resume: () => Promise<void>;
};

function useControl(group: BrowserGroup, live: LiveView | null, snap: LiveSnapshot, toast: (text: string) => void): ControlApi {
  const [busy, setBusy] = useState(false);
  const drive = driveState({ ...group, needs_you: needsOf(group.needs_you, snap) }, snap.control);
  const run = useCallback(async (what: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await what();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }, [toast]);
  return useMemo(() => ({
    drive,
    busy,
    take: () => run(async () => {
      const client = live?.connection.clientId;
      if (!client) throw new Error(t("browser.take.notyet"));
      await setControl(group.id, { owner: "human", client_id: client });
      haptic("medium");
      // The keyboard goes to the page at once: a person who took control is about to type.
      window.setTimeout(() => focusViewer(document.querySelector(`.bp[data-group="${CSS.escape(group.id)}"]`)), 60);
    }),
    giveBack: (note: string) => run(async () => {
      await setControl(group.id, { owner: "agent", note: note.trim() || undefined });
      toast(t("browser.given"));
    }),
    pause: () => run(() => setControl(group.id, { owner: "paused", reason: t("browser.paused.reason") })),
    resume: () => run(() => setControl(group.id, { owner: "agent" })),
  }), [drive, busy, run, live, group.id, toast]);
}

/** The primary button: what this window can do about who drives. */
export function ControlButton({ control, compact }: { control: ControlApi; compact?: boolean }) {
  const [giving, setGiving] = useState<HTMLElement | null>(null);
  const { drive, busy } = control;
  if (drive === "closed") return null;
  if (drive === "you") {
    return (
      <>
        <button type="button" className="btn small bp-control give" disabled={busy} onClick={(e) => setGiving(e.currentTarget)} aria-haspopup="dialog">
          <Icon name="undo" size={14} />
          {t("browser.give")}
        </button>
        {giving && <GiveBack anchor={giving} onClose={() => setGiving(null)} onGive={(note) => { setGiving(null); void control.giveBack(note); }} />}
      </>
    );
  }
  if (drive === "paused") {
    return (
      <button type="button" className="btn small bp-control resume" disabled={busy} onClick={() => void control.resume()}>
        <Icon name="play" size={14} />
        {t("browser.resume")}
      </button>
    );
  }
  return (
    <button type="button" className={`btn small bp-control take ${drive === "needs" ? "needs" : ""}`} disabled={busy} onClick={() => void control.take()} title={`${t("browser.take")} (${navigator.platform?.includes("Mac") ? "⌘⇧K" : "Ctrl+Shift+K"})`}>
      <Icon name="user" size={14} />
      {compact ? t("browser.take.short") : drive === "other" ? t("browser.takeover") : t("browser.take")}
    </button>
  );
}

function GiveBack({ anchor, onClose, onGive }: { anchor: HTMLElement; onClose: () => void; onGive: (note: string) => void }) {
  const [note, setNote] = useState("");
  const field = useRef<HTMLTextAreaElement>(null);
  useEffect(() => field.current?.focus(), []);
  return (
    <Popover anchor={anchor} onClose={onClose} align="right" className="bp-give" label={t("browser.give")}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onGive(note);
        }}
      >
        <label className="bp-give-label" htmlFor="bp-give-note">{t("browser.give.note")}</label>
        <textarea
          id="bp-give-note"
          ref={field}
          className="bp-give-note"
          rows={3}
          value={note}
          maxLength={2000}
          placeholder={t("browser.give.placeholder")}
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              onGive(note);
            }
          }}
        />
        <div className="bp-give-foot">
          <span className="sub">{t("browser.give.why")}</span>
          <button type="submit" className="btn small primary">{t("browser.give")}</button>
        </div>
      </form>
    </Popover>
  );
}

// ── the toolbar ──────────────────────────────────────────────────────────────────────────────

function Toolbar({ group, groups, onGroup, live, snap, url, tabs, viewing, drive, control, toast, full, recording, frames, onReplay }: {
  group: BrowserGroup;
  groups: BrowserGroup[];
  onGroup?: (id: string) => void;
  live: LiveView | null;
  snap: LiveSnapshot;
  url: string;
  tabs: { id: string; url: string; title: string; favicon_url: string; loading: boolean }[];
  viewing: string | null;
  drive: DriveState;
  control: ControlApi;
  toast: (text: string) => void;
  full: boolean;
  recording: boolean;
  frames: BrowserFrame[];
  onReplay: () => void;
}) {
  const driving = drive === "you";
  const record = async (on: boolean) => {
    try {
      await setRecording(group.id, on);
      toast(t(on ? "browser.record.on" : "browser.record.off"));
    } catch (e) {
      toast(errorText(e));
    }
  };
  const nav = (action: "back" | "forward" | "reload") => live?.input({ t: "nav", action });
  const items: MenuItem[] = [
    ...(drive === "paused" ? [] : drive === "you" ? [] : [{ label: t("browser.pause"), icon: "pause" as IconName, onSelect: () => void control.pause() }]),
    ...(full ? [] : [{ label: t("browser.window"), icon: "external" as IconName, onSelect: () => window.open(pathFor("browser", group.id), "_blank", "noopener") }]),
    // The recording is the operator's alone: the agent has no tool that switches it.
    { label: t(recording ? "browser.record.stop" : "browser.record.start"), icon: "dot" as IconName, checked: recording, onSelect: () => void record(!recording) },
    ...(frames.length ? [
      { label: t("browser.replay.open", { n: frames.length }), icon: "image" as IconName, onSelect: onReplay },
      {
        label: t("browser.record.delete"), icon: "trash" as IconName,
        onSelect: async () => {
          if (!(await confirmAsync(t("browser.record.delete.title"), { body: t("browser.record.delete.body"), action: t("browser.record.delete") }))) return;
          try {
            await deleteRecording(group.id);
          } catch (e) {
            toast(errorText(e));
          }
        },
      },
    ] : []),
    ...(groups.length > 1 && onGroup ? ["-" as const, ...groups.map((g) => ({ label: `${domainOf(g.tabs.find((x) => x.active)?.url ?? "")} · ${g.profile}`, icon: "globe" as IconName, checked: g.id === group.id, onSelect: () => onGroup(g.id) }))] : []),
    "-",
    {
      label: t("browser.close"), icon: "trash", danger: true,
      onSelect: async () => {
        if (!(await confirmAsync(t("browser.close.title"), { body: t("browser.close.body"), action: t("browser.close") }))) return;
        try {
          await closeBrowser(group.id);
          if (full) navigate(pathFor("agents"));
        } catch (e) {
          toast(errorText(e));
        }
      },
    },
  ];
  return (
    <div className="bp-toolbar panel-toolbar">
      <button className="iconbtn small" disabled={!driving} onClick={() => nav("back")} aria-label={t("panel.back")} title={driving ? t("panel.back") : t("browser.nav.locked")}><Icon name="back" size={16} /></button>
      <button className="iconbtn small" disabled={!driving} onClick={() => nav("forward")} aria-label={t("panel.forward")} title={driving ? t("panel.forward") : t("browser.nav.locked")}><Icon name="forward" size={16} /></button>
      <button className="iconbtn small" disabled={!driving} onClick={() => nav("reload")} aria-label={t("panel.reload")} title={driving ? t("panel.reload") : t("browser.nav.locked")}><Icon name="reload" size={16} /></button>
      <Address url={url} editable={driving} loading={tabs.find((x) => x.id === viewing)?.loading ?? false} onGo={(next) => live?.input({ t: "nav", action: "url", url: next })} />
      {tabs.length > 1 && <TabStrip tabs={tabs} viewing={viewing} active={snap.active} onPick={(id) => live?.view({ tab: id })} />}
      <ControlButton control={control} />
      <OverflowMenu label={t("browser.menu")} small items={items} />
    </div>
  );
}

function Address({ url, editable, loading, onGo }: { url: string; editable: boolean; loading: boolean; onGo: (url: string) => void }) {
  const [text, setText] = useState(url);
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    if (!editing) setText(url);
  }, [url, editing]);
  const locked = secure(url);
  return (
    <form
      className={`bp-address ${editable ? "editable" : ""} ${loading ? "loading" : ""}`}
      onSubmit={(e) => {
        e.preventDefault();
        const v = text.trim();
        if (!v) return;
        onGo(/^[a-z][a-z0-9+.-]*:/i.test(v) ? v : `https://${v}`);
        setEditing(false);
        (e.currentTarget.querySelector("input") as HTMLInputElement | null)?.blur();
      }}
      title={editable ? undefined : t("browser.address.locked")}
    >
      <span className={`bp-address-lock ${locked ? "ok" : "plain"}`} aria-hidden="true"><Icon name={locked ? "lock" : "globe"} size={12} /></span>
      <input
        value={editing ? text : url}
        readOnly={!editable}
        aria-label={t("browser.address")}
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        onFocus={(e) => {
          if (!editable) return;
          setEditing(true);
          setText(url);
          e.currentTarget.select();
        }}
        onBlur={() => setEditing(false)}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            setEditing(false);
            e.currentTarget.blur();
          }
        }}
      />
      {loading && <span className="bp-address-busy" aria-hidden="true" />}
    </form>
  );
}



function TabStrip({ tabs, viewing, active, onPick }: { tabs: { id: string; url: string; title: string; favicon_url: string }[]; viewing: string | null; active: string | null; onPick: (id: string) => void }) {
  return (
    <div className="bp-tabs" role="tablist" aria-label={t("browser.tabs")}>
      {tabs.map((tab) => (
        <button key={tab.id} role="tab" type="button" aria-selected={tab.id === viewing} className={`bp-tabchip ${tab.id === viewing ? "on" : ""} ${tab.id === active ? "agent" : ""}`} onClick={() => onPick(tab.id)} title={tab.title || tab.url}>
          <Favicon url={tab.favicon_url} page={tab.url} />
          <span className="truncate">{tab.title || domainOf(tab.url)}</span>
        </button>
      ))}
    </div>
  );
}

function PhoneBar({ group, url, tabs, control }: { group: BrowserGroup; url: string; tabs: number; control: ControlApi }) {
  const active = group.tabs.find((x) => x.active) ?? group.tabs[0];
  return (
    <div className="bp-phonebar">
      <Favicon url={active?.favicon_url ?? ""} page={url} size={16} />
      <span className="bp-phonebar-domain truncate">{domainOf(url) || t("browser.blank")}</span>
      {tabs > 1 && <span className="bp-phonebar-tabs">{plural("browser.tabs.n", tabs)}</span>}
      <ControlButton control={control} />
    </div>
  );
}

// ── the banner over the picture ──────────────────────────────────────────────────────────────

const BANNER_ICON: Record<DriveState, IconName> = { acting: "bolt", idle: "globe", you: "user", other: "user", paused: "pause", needs: "alert", closed: "stop" };

function Banner({ drive, agent, needs, state, control, viewers, bare }: { drive: DriveState; agent: string; needs: { reason: string; what?: string } | null; state: string; control: ControlApi; viewers: LiveSnapshot["viewers"]; bare?: boolean }) {
  const offline = state === "reconnecting" || state === "proxy-blocked";
  let text: ReactNode;
  let action: ReactNode = null;
  if (offline) text = t("browser.banner.offline");
  else if (state === "unavailable") text = t("browser.state.gone");
  else if (drive === "you") {
    text = t("browser.banner.you", { name: agent });
  } else if (drive === "other") {
    text = t("browser.banner.other");
    action = <button type="button" className="bp-banner-act" onClick={() => void control.take()}>{t("browser.takeover")}</button>;
  } else if (drive === "needs") {
    text = <><b>{t("browser.banner.needs")}</b> {needWords(needs)}</>;
    action = <button type="button" className="bp-banner-act" onClick={() => void control.take()}>{t("browser.take")}</button>;
  } else if (drive === "paused") {
    text = t("browser.banner.paused", { name: agent });
    action = <button type="button" className="bp-banner-act" onClick={() => void control.resume()}>{t("browser.resume")}</button>;
  } else if (drive === "closed") {
    text = t("browser.banner.closed");
  } else {
    text = drive === "acting" ? t("browser.banner.acting", { name: agent }) : t("browser.banner.idle", { name: agent });
  }
  // A phone's bar above already holds the one button, and has no room for who else watches.
  if (bare) action = null;
  const others = bare ? [] : viewers.others.filter((o) => o.label);
  return (
    <div className={`bp-banner ${offline ? "offline" : drive}`} role="status" data-banner={offline ? "offline" : drive}>
      <span className="bp-banner-icon"><Icon name={offline ? "reload" : BANNER_ICON[drive]} size={13} /></span>
      <span className="bp-banner-text truncate">{text}</span>
      {others.length > 0 && <span className="bp-banner-viewers" title={others.map((o) => o.label).join(", ")}><Icon name="eye" size={12} />{others.map((o) => o.label).join(", ")}</span>}
      {action}
    </div>
  );
}

function DialogChip({ group, tab, dialog, canAnswer, toast }: { group: string; tab: string | null; dialog: { type: string; message: string }; canAnswer: boolean; toast: (text: string) => void }) {
  const answer = async (accept: boolean) => {
    try {
      await answerDialog(group, tab, accept);
    } catch (e) {
      toast(errorText(e));
    }
  };
  return (
    <div className="bp-dialog" role="alertdialog" aria-label={t("browser.dialog")}>
      <Icon name="question" size={14} />
      <span className="bp-dialog-text"><b>{t("browser.dialog")}</b> {dialog.message}</span>
      {canAnswer ? (
        <span className="bp-dialog-actions">
          <button type="button" className="btn small" onClick={() => void answer(false)}>{t("browser.dialog.dismiss")}</button>
          <button type="button" className="btn small primary" onClick={() => void answer(true)}>{t("browser.dialog.accept")}</button>
        </span>
      ) : (
        <span className="sub">{t("browser.dialog.take")}</span>
      )}
    </div>
  );
}

// ── the action log ───────────────────────────────────────────────────────────────────────────

const ACTOR_ICON: Record<string, IconName> = { agent: "bolt", operator: "user", page: "globe", system: "shield" };

export function ActionLog({ group, recent, open, onToggle, onFocus, agent, page = false, frames = [], onReplay }: { group: string; recent: ActionEvent[]; open: boolean; onToggle: () => void; onFocus: (a: ActionEvent) => void; agent: string; page?: boolean; frames?: BrowserFrame[]; onReplay?: (index: number) => void }) {
  const listed = useActions(group);
  const rows = useMemo(() => mergeActions(listed, recent.map(rowOfEvent)), [listed, recent]);
  const latest = rows[0];
  const [focused, setFocused] = useState<string | null>(null);
  const pick = (row: BrowserActionRow) => {
    setFocused(row.id);
    // With a recording, a row opens the picture its action left, the element framed on it.
    const kept = frameOfRow(frames, row);
    if (kept >= 0 && onReplay) return onReplay(kept);
    if (!row.point && !row.box) return;
    onFocus({ type: "action", id: row.id, group, tab: row.tab, actor: row.actor === "operator" ? "operator" : "agent", kind: row.kind, point: row.point ?? null, box: row.box ?? null, name: row.name, element: row.element, at: Date.parse(row.at) });
  };
  return (
    <section className={`bp-log ${open ? "open" : ""} ${page ? "page" : ""}`} aria-label={t("browser.log")}>
      {!page && (
        <button type="button" className="bp-log-head" onClick={onToggle} aria-expanded={open}>
          <Icon name="journal" size={14} />
          <span className="bp-log-title">{t("browser.log")}</span>
          {latest && !open && <span className="bp-log-latest truncate"><ActionText row={latest} agent={agent} /></span>}
          {rows.length > 0 && <span className="bp-log-count">{rows.length}</span>}
          <span className={`chev ${open ? "open" : ""}`}><Icon name="chevron" size={14} /></span>
        </button>
      )}
      {page && (
        <div className="bp-log-head static">
          <Icon name="journal" size={14} />
          <span className="bp-log-title">{t("browser.log")}</span>
          {rows.length > 0 && <span className="bp-log-count">{rows.length}</span>}
        </div>
      )}
      {/* In the page either way: a closed drawer hides them, and a wide tab shows them as a column only while open. */}
      <ol className="bp-log-rows">
        {rows.length === 0 && <li className="bp-log-empty sub">{t("browser.log.empty")}</li>}
        {rows.map((row) => (
          <li key={row.id}>
            <button type="button" className={`bp-log-row ${focused === row.id ? "on" : ""} ${row.ok === false ? "failed" : ""}`} onClick={() => pick(row)} data-action={row.id}>
              <span className="bp-log-time">{clock(row.at)}</span>
              <span className={`bp-log-actor ${row.actor}`} title={row.actor === "operator" ? t("browser.actor.you") : row.actor === "page" ? t("browser.actor.page") : agent}><Icon name={ACTOR_ICON[row.actor] ?? "bolt"} size={12} /></span>
              <span className="bp-log-text"><ActionText row={row} agent={agent} /></span>
              {frameOfRow(frames, row) >= 0 && <span className="bp-log-frame" title={t("browser.replay.row")}><Icon name="image" size={12} /></span>}
              {row.sensitive && <span className={`bp-log-flag ${row.sensitive.decision}`}>{t(`browser.sensitive.${row.sensitive.decision}`)}</span>}
            </button>
          </li>
        ))}
      </ol>
    </section>
  );
}

function ActionText({ row, agent }: { row: BrowserActionRow; agent: string }) {
  const words = actionWords(row);
  const vars = { ...words.vars };
  if (typeof vars.what === "string" && !vars.what) vars.what = t("browser.act.something");
  return <>{t(words.key, { ...vars, name: agent })}</>;
}
