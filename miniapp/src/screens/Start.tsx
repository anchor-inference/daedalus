// The screen you land on: a greeting and the composer, so a chat begins by typing it.
// New agent is this screen too, with the composer in Agent mode (?agent=1): the name in the field's
// head, the model and the project where a chat picks them, and the rare choices (a private chat, an
// own directory, a folder of the project, a loop, tools) under its +. A separate window used to ask
// the same things again.
// On a phone it is the home of the redesign: the composer at the bottom, the model in the title and
// only the live chats above it; the full list is the Chats page (Chats.tsx), at ?view=chats.

import { Fragment, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { api, AsrStatus, Preset, Project, SessionList, SessionSummary, Settings } from "../api";
import { AttachmentCard, PlusSheet, useComposerFocus } from "../composerbox";
import { layerOpen, Popover, Sheet } from "../ui/dialogs";
import { effortBody, effortOf, presetEffort, type Effort } from "../starteffort";
import { ModelChoice, ModelSelect } from "../modelselect";
import { MicButton, VoiceBar, VoiceNoteFailed, useVoiceNote } from "../voicebar";
import { landWords } from "../voicenote";
import { ChatsScreen } from "./Chats";
import { CHATS_PATH } from "../drawer";
import { Icon } from "../icons";
import { navigate, pathFor, sessionPath, useRoute } from "../router";
import { invalidate, useOffline, useQuery } from "../store";
import { useStreamUp } from "../events";
import { useSummary } from "../notifications";
import { agentsListingOf } from "../mode";
import { agentName, isProject, kindOf } from "../grouping";
import { relTime, shortModel, untilShort } from "../format";
import { Dot, ToolPicker, fmtInterval, statusWord } from "../ui/components";
import { screenTitle, useMedia } from "../ui/index";
import { Banner, IconButton, ListRow, SectionHeader, SheetRow, TopBar } from "../ui/phone";
import { enterSends, errorText } from "../ui";
import { t } from "../i18n";
import { HostMark, RunOn, RunOnChip, RunOnSelect, useRunOn } from "../runon";
import { PrivateChip, PrivateMark, isPrivate, privateBody } from "../privatechat";
import { agentFolders, folderName, offersFolderChoice, projectReachable } from "../folders";
import { ProjectSettingsSheet, useProjects } from "../projects";
import { ProjectsPage } from "./Projects";
import { PhoneImportCard, StartImportCard } from "../imports/ImportedChat";
import { ProjectPickList } from "../projectpick";
import { type ChatKind, type KindOffers, KindSelect, kindIcon, kindPlate, requestComputerSheet, useKindOffers, useNewChatKind } from "../chatkind";
import { USER_STATUS } from "../browser/yourchrome";
import { confirmAsync } from "../ui";
import { CodeField, type CodeFieldHandle } from "../codefield";

/** Every project a chat could start in, the most recently used first and the installation's own last;
 *  a scratch folder of a single chat is not one. */
function useChoosable(projects: Project[], listing: SessionList | null): Project[] {
  return useMemo(() => {
    const recent = new Map((listing?.projects ?? []).map((folder) => [folder.id, Date.parse(folder.last_message_at) || 0]));
    // The installation's own project goes last: it is where talking lands, rarely where one types.
    return projects.filter((p) => isProject(p))
      .sort((a, b) => Number(!!a.system) - Number(!!b.system) || (recent.get(b.id) ?? 0) - (recent.get(a.id) ?? 0));
  }, [projects, listing]);
}

export function StartScreen({ onOpen, toast, project = "", projects = [], onPickProject }: { onOpen: (id: string) => void; toast: (t: string) => void; project?: string; projects?: Project[]; onPickProject?: (id: string) => void }) {
  const phone = !useMedia("(min-width: 1024px)");
  const route = useRoute();
  const model = useStartModel();
  // New agent asked from a list or a menu shows the composer whatever page the address held.
  const agent = route.query.get("agent") === "1";
  // The projects page is opened from a desktop's column; a phone opens the projects sheet instead.
  // It stays mounted at any width all the same, so a window narrowed with a project's settings open
  // keeps the sheet and what was typed in it rather than swapping the page for the phone's home.
  if (!agent && route.query.get("view") === "projects") return <ProjectsPage toast={toast} onPickProject={onPickProject} />;
  if (!agent && phone && route.query.get("view") === "chats") return <ChatsScreen onOpen={onOpen} toast={toast} project={project} projects={projects} onPickProject={onPickProject} />;
  if (phone) return <PhoneHome onOpen={onOpen} toast={toast} project={project} projects={projects} model={model} />;
  return <DesktopStart onOpen={onOpen} toast={toast} project={project} projects={projects} model={model} />;
}

/** Agent mode lives in the address, so every "New agent" is a link and Back leaves it. Switching it
 *  here replaces the address and keeps the rest of it (the project asked for). */
function useAgentMode(): [boolean, (on: boolean) => void] {
  const route = useRoute();
  const on = route.query.get("agent") === "1";
  const set = (next: boolean) => {
    if (next === on) return;
    const query = Object.fromEntries(route.query.entries());
    navigate(pathFor("agents", null, { ...query, agent: next ? "1" : undefined }), { replace: true });
  };
  return [on, set];
}

/**
 * A desktop's new chat: the mark and the question, the composer in the middle of the window where the
 * eye already is, the projects it can start in under it, and what is live under those. The phone's
 * home holds the same three things in a column ending at the composer; a desktop keeps the composer
 * under the question, because the field at the bottom of a large window is where nobody looks.
 */
function DesktopStart({ onOpen, toast, project, projects, model }: { onOpen: (id: string) => void; toast: (t: string) => void; project: string; projects: Project[]; model: StartModel }) {
  const route = useRoute();
  const live = useStreamUp();
  const { data } = useQuery<SessionList>("/api/sessions?view=all", { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  const listing = useMemo(() => agentsListingOf(data), [data]);
  // Where the chat starts: the project the projects page asked for, else the one in view, else a chat
  // of its own. Picked here it stays here; the lens of the whole column is not this choice.
  const asked = route.query.get("in") ?? "";
  const [target, setTarget] = useState(asked || project);
  useEffect(() => setTarget(asked || project), [asked, project]);
  const choosable = useChoosable(projects, listing);
  const startable = useMemo(() => choosable.filter(projectReachable), [choosable]);
  const known = !target || startable.some((p) => p.id === target);
  const run = useRunOn(known ? target : "");
  const offers = useKindOffers();
  const [kind, setKind] = useNewChatKind(offers);
  const [agent, setAgent] = useAgentMode();
  return (
    <div className="start">
      <div className="start-hero">
        <div className="start-mark">
          <Icon name="logo" size={34} />
          <h1 className="start-greeting">{t(agent ? "start.greeting.agent" : "start.greeting")}</h1>
        </div>
        <StartComposer phone={false} project={known ? target : ""} toast={toast} model={model} run={run} kind={kind} onKind={setKind} offers={offers} agent={agent} onAgent={setAgent} />
        {startable.length > 0 && <StartWhere projects={startable} all={choosable} target={known ? target : ""} onPick={setTarget} />}
        <LiveNow listing={listing} project={project} onOpen={onOpen} />
        <StartImportCard />
      </div>
    </div>
  );
}

/** How many projects the start screen names under its composer before the rest go behind "N more". */
const WHERE = 3;

/** "Start in …": a chat of its own, or one of the projects, so a chat (or an agent) starts in a project
 *  with no form to fill. The chips are the recent few; "Choose a project" searches them all. The project
 *  picked stays in the row even when it was not one of the chips. */
function StartWhere({ projects, all, target, onPick }: { projects: Project[]; all: Project[]; target: string; onPick: (id: string) => void }) {
  const more = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  let shown = projects.slice(0, WHERE);
  const picked = projects.find((p) => p.id === target);
  if (picked && !shown.includes(picked)) shown = [...shown.slice(0, WHERE - 1), picked];
  const hidden = projects.filter((p) => !shown.includes(p));
  const chip = (id: string, name: string, icon: "bots" | "folder" | "mic") => (
    <button key={id || "chat"} type="button" className={`start-where-chip ${target === id ? "on" : ""}`} aria-pressed={target === id} data-project={id || undefined} onClick={() => onPick(id)}>
      <span className="start-where-tile"><Icon name={icon} size={12} /></span>
      <span className="truncate">{name}</span>
    </button>
  );
  return (
    <div className="start-where" role="group" aria-label={t("start.where.label")}>
      <span className="start-where-word">{t("start.where")}</span>
      {chip("", t("start.where.chat"), "bots")}
      {shown.map((p) => chip(p.id, p.name, p.system === "companion" ? "mic" : "folder"))}
      <button ref={more} type="button" className="start-where-chip more start-where-pick" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(!open)}
        title={hidden.length ? t("start.where.more", { n: hidden.length }) : undefined}>
        <Icon name="search" size={12} />{t("start.pick")}{hidden.length > 0 && <span className="start-where-n num">{hidden.length}</span>}<Icon name="chevron" size={12} />
      </button>
      {open && (
        <Popover anchor={more.current} onClose={() => setOpen(false)} className="start-where-menu" label={t("start.where.label")}>
          <ProjectPickList projects={all} target={target} onPick={(id) => { setOpen(false); onPick(id); }} />
        </Popover>
      )}
    </div>
  );
}

/**
 * "Active now" on a desktop: the phone home's rows, wider, with the project in the meta — the chats
 * that need the operator first, then the ones at work, then the loops; three, and the rest a click
 * away in place. A waiting chat's Reply is neutral, not the white primary: the question is out of its
 * context here, and the white button stays the composer's.
 */
function LiveNow({ listing, project, onOpen }: { listing: SessionList | null; project: string; onOpen: (id: string) => void }) {
  const [all, setAll] = useState(false);
  const rows = useMemo(() => {
    const rank = (s: SessionSummary) => ({ waiting: 0, working: 1, loop: 2, idle: 3 })[kindOf(s, false)];
    return (listing?.sessions ?? [])
      .filter((s) => !s.metadata?.subagent_of && !s.archived && (!project || s.project_id === project) && rank(s) < 3)
      .sort((a, b) => rank(a) - rank(b) || Date.parse(b.last_message_at) - Date.parse(a.last_message_at));
  }, [listing, project]);
  if (!rows.length) return null;
  const folders = new Map((listing?.projects ?? []).map((folder) => [folder.id, folder]));
  const shown = all ? rows : rows.slice(0, LIVE);
  return (
    <section className="start-live" aria-label={t("start.live")}>
      <div className="start-live-head">
        <span>{t("start.live")}</span>
        <span className="num">{rows.length}</span>
        <span className="grow" />
        {rows.length > LIVE && <button type="button" className="start-live-more" onClick={() => setAll(!all)}>{all ? t("start.live.fewer") : t("start.live.all", { n: rows.length })}</button>}
      </div>
      {shown.map((s) => {
        const folder = folders.get(s.project_id);
        const where = folder && isProject(folder) && folder.name !== agentName(s) ? folder.name : "";
        const plate = kindPlate(s);
        return (
          <div key={s.id} data-session={s.id} className={`start-live-row ${s.status}`} role="link" tabIndex={0} onClick={() => onOpen(s.id)}
            onKeyDown={(e) => { if (e.target !== e.currentTarget) return; if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(s.id); } }}>
            <span className={`start-live-plate ${plate.className}`} title={plate.title}><Icon name={s.metadata?.loop ? "loop" : kindIcon(plate.kind) ?? "bots"} size={15} />{s.status !== "idle" && <Dot status={s.status} />}</span>
            <span className="start-live-main">
              <span className="start-live-title truncate">{agentName(s)}</span>
              <span className="start-live-meta"><DesktopLiveMeta s={s} where={where} /></span>
            </span>
            <span className="start-live-time num">{relTime(s.last_message_at)}</span>
            {s.status === "waiting" && <button type="button" className="btn small start-live-reply" onClick={(e) => { e.stopPropagation(); onOpen(s.id); }}>{t("start.live.reply")}</button>}
          </div>
        );
      })}
    </section>
  );
}

function DesktopLiveMeta({ s, where }: { s: SessionSummary; where: string }) {
  const loop = s.metadata?.loop;
  const parts: ReactNode[] = [];
  if (s.status === "waiting" || s.status === "running" || s.status === "failed") parts.push(<span key="state" className={`start-live-state ${s.status}`}>{statusWord(s.status)}</span>);
  else if (loop) parts.push(<span key="loop">{[loop.mode === "interval" ? t("loop.every", { t: fmtInterval(loop.interval_seconds) }) : t("loop.selfpaced"), loop.next_run_at ? t("ph.loop.next", { t: untilShort(loop.next_run_at) }) : ""].filter(Boolean).join(" · ")}</span>);
  const mark = <Fragment key="marks"><HostMark env={s.env} /><PrivateMark on={isPrivate(s)} size={11} /></Fragment>;
  if (where) parts.push(<span key="where" className="start-live-where truncate">{where}</span>);
  if (s.model) parts.push(<span key="model" className="truncate">{shortModel(s.model, 28)}</span>);
  return <>{parts.map((part, i) => <Fragment key={i}>{i > 0 && <span className="sep">·</span>}{part}{i === 0 && mark}</Fragment>)}</>;
}

/** How many live chats the home shows above its composer; the rest are one tap away in Chats. */
const LIVE = 3;

/**
 * A phone's home is a new chat: the composer at the bottom, the model in the title, and above it only
 * what is live — the chats that need the operator, the ones at work, the loops — three at most. The
 * whole list is the Chats page (the drawer, or "See all"). Offline, the live rows stay, dimmed, with
 * the time they were last confirmed, and the composer keeps the draft until the bot is back.
 */
function PhoneHome({ onOpen, toast, project, projects, model }: { onOpen: (id: string) => void; toast: (t: string) => void; project: string; projects: Project[]; model: StartModel }) {
  const live = useStreamUp();
  const offline = useOffline();
  const summary = useSummary();
  const { data, updatedAt } = useQuery<SessionList>("/api/sessions?view=all", { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  // Where the chat starts, as on a desktop: the project in view at first, any project by the picker.
  // A phone used to start every chat in the lens or in a chat of its own, with nothing to choose.
  const choosable = useChoosable(projects, agentsListingOf(data));
  const asked = useRoute().query.get("in") ?? "";
  const [target, setTarget] = useState(asked || project);
  useEffect(() => setTarget(asked || project), [asked, project]);
  const [picking, setPicking] = useState(false);
  const [settingsOf, setSettingsOf] = useState<Project | null>(null);
  const picked = choosable.find((p) => p.id === target && projectReachable(p));
  const run = useRunOn(picked ? target : "");
  const offers = useKindOffers();
  const [kind, setKind] = useNewChatKind(offers);
  const [agent, setAgent] = useAgentMode();
  const rows = useMemo(() => {
    const listing = agentsListingOf(data);
    const rank = (s: SessionSummary) => ({ waiting: 0, working: 1, loop: 2, idle: 3 })[kindOf(s, false)];
    return (listing?.sessions ?? [])
      .filter((s) => !s.metadata?.subagent_of && !s.archived && (!project || s.project_id === project) && rank(s) < 3)
      .sort((a, b) => rank(a) - rank(b) || Date.parse(b.last_message_at) - Date.parse(a.last_message_at));
  }, [data, project]);
  const waiting = rows.filter((s) => s.status === "waiting").length;
  const shown = rows.slice(0, LIVE);
  const confirmed = updatedAt ? new Date(updatedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  return (
    <div className="ph-page ph-home">
      <TopBar
        center
        pip={waiting > 0}
        title={<>{model.label}<Icon name="chevron" size={14} /></>}
        onTitle={() => model.setOpen(true)}
        titleLabel={`${t("session.model.for")}: ${model.label}`}
        actions={<IconButton icon="bell" label={screenTitle("inbox")} href={pathFor("inbox")} badge={summary.unseen} />}
      />
      {offline && <Banner strip tone="bad" icon="offline">{t("app.offline")}</Banner>}
      <div className="ph-page-body">
        <div className={`ph-hero ${offline ? "offline" : ""}`}>
          <Icon name="logo" size={36} />
          <h1>{t(agent ? "start.greeting.agent" : "start.greeting")}</h1>
          <div className="ph-hero-picks">
            <button type="button" className="ph-btn sm ph-where" onClick={() => setPicking(true)} aria-haspopup="dialog" aria-label={`${t("start.where.label")}: ${picked ? picked.name : t("start.pick.none")}`}>
              <Icon name={picked ? (picked.system === "companion" ? "mic" : "folder") : "bots"} size={16} />
              <span className="truncate">{picked ? picked.name : t("start.pick.none")}</span>
              <Icon name="chevron" size={14} />
            </button>
            {/* The new chat's mode stands beside where it starts: at rest the phone's composer is one
                row with no room for it, and a computer chat begun unawares as a plain one is a wasted turn. */}
            <KindSelect kind={kind} offers={offers} onChoose={setKind} sheet className="ph-btn sm ph-kind" agent={agent} onAgent={setAgent} />
            <RunOnChip run={run} project={picked} onSettings={() => setSettingsOf(picked ?? null)} />
          </div>
        </div>
        {shown.length > 0 && (
          <>
            <SectionHeader count={offline && confirmed ? t("ph.live.confirmed", { time: confirmed }) : undefined}
              action={<button type="button" className="ph-link" onClick={() => navigate(CHATS_PATH)}>{t("ph.seeall")}</button>}>{t("ph.live")}</SectionHeader>
            <div className={`ph-list ph-live ${offline ? "stale" : ""}`}>
              {shown.map((s) => (
                <ListRow key={s.id} data={{ session: s.id }} title={agentName(s)} onOpen={() => onOpen(s.id)}
                  lead={<span className={`ph-sicon ${kindPlate(s).className}`} title={kindPlate(s).title}><Icon name={s.metadata?.loop ? "loop" : kindIcon(kindPlate(s).kind) ?? "bots"} size={18} />{s.status !== "idle" && <span className={`ph-st ${s.status}`} />}</span>}
                  meta={<LiveMeta s={s} />}
                  trail={relTime(s.last_message_at)} />
              ))}
            </div>
          </>
        )}
        <PhoneImportCard />
      </div>
      <StartComposer phone project={picked ? target : ""} toast={toast} model={model} run={run} kind={kind} onKind={setKind} offers={offers} agent={agent} onAgent={setAgent} />
      {picking && (
        <Sheet title={t("start.pick")} onClose={() => setPicking(false)} size="narrow">
          <ProjectPickList projects={choosable} target={picked ? target : ""} onPick={(id) => { setTarget(id); setPicking(false); }} />
        </Sheet>
      )}
      {settingsOf && <ProjectSettingsSheet project={settingsOf} onClose={() => setSettingsOf(null)} onRemoved={() => { setSettingsOf(null); setTarget(""); }} toast={toast} />}
    </div>
  );
}

function LiveMeta({ s }: { s: SessionSummary }) {
  if (s.status === "waiting" || s.status === "running" || s.status === "failed") {
    return <><span className={s.status}>{statusWord(s.status)}</span>{s.model && <><span className="ph-sep" /><span className="ph-ell">{shortModel(s.model, 28)}</span></>}</>;
  }
  const loop = s.metadata?.loop;
  const words = [t("ph.loop"), loop?.mode === "interval" ? t("loop.every", { t: fmtInterval(loop.interval_seconds) }) : t("loop.selfpaced"), loop?.next_run_at ? t("ph.loop.next", { t: untilShort(loop.next_run_at) }) : ""].filter(Boolean).join(" · ");
  return <span className="ph-ell">{words}</span>;
}

type Chosen = { preset: string } | { model: string };

/** The model and effort of the chat about to start: held above the composer because a phone picks
 *  the model from the page's title, a desktop from the composer's own control, and both open one list. */
type StartModel = ReturnType<typeof useStartModel>;

function useStartModel() {
  const settings = useQuery<Settings>("/api/settings", { staleMs: 60000 });
  const presets = settings.data?.presets ?? {};
  const defaultId = settings.data?.model?.preset ?? "";
  const [chosen, setChosen] = useState<Chosen | null>(null);
  const [open, setOpen] = useState(false);
  // Picked here and carried into the session it makes; null leaves the model's own.
  const [effort, setEffort] = useState<Effort | null>(null);
  const labelOf = (id: string) => {
    const item = presets[id] as Preset | undefined;
    return item ? item.label || `${item.provider}/${item.model}` : id;
  };
  const label = chosen
    ? "preset" in chosen ? labelOf(chosen.preset) : chosen.model
    : defaultId ? labelOf(defaultId) : t("start.model.default");
  // A model picked outside the presets keeps the thinking the session starts with, the default's.
  const presetId = chosen && "preset" in chosen ? chosen.preset : defaultId;
  const shownEffort = effort ?? presetEffort(presets[presetId] as Preset | undefined);
  function choose(choice: ModelChoice) {
    if ("clear" in choice) setChosen(null);
    else if ("preset" in choice) setChosen({ preset: choice.preset });
    else setChosen({ model: choice.model });
  }
  return { chosen, label, effort, setEffort, shownEffort, choose, open, setOpen };
}

/** What Agent mode adds to a new chat, beyond the model and the project the composer already asks. */
type AgentDraft = {
  name: string;
  ownDirectory: boolean;
  folder: string;
  loop: { on: boolean; text: string; mode: "interval" | "dynamic"; minutes: string; max: string };
  toolsOff: string[];
};

const NO_AGENT: AgentDraft = { name: "", ownDirectory: false, folder: "", loop: { on: false, text: "", mode: "interval", minutes: "10", max: "" }, toolsOff: [] };

function StartComposer({ phone, project, toast, model, run, kind, onKind, offers, agent, onAgent }: { phone: boolean; project: string; toast: (t: string) => void; model: StartModel; run: RunOn; kind: ChatKind; onKind: (kind: ChatKind) => void; offers: KindOffers; agent: boolean; onAgent: (on: boolean) => void }) {
  const { chosen, effort, setEffort, shownEffort, choose } = model;
  const modelLabel = model.label;
  const modelOpen = model.open;
  const setModelOpen = model.setOpen;
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const focus = useComposerFocus(phone);
  const focused = focus.focused;
  const offline = useOffline();
  const { data: asr } = useQuery<AsrStatus>("/api/asr", { staleMs: 60000 });
  const field = useRef<CodeFieldHandle>(null);
  const nameField = useRef<HTMLInputElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);
  const plusButton = useRef<HTMLButtonElement>(null);
  const [plusOpen, setPlusOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [foldersOpen, setFoldersOpen] = useState(false);
  // Asked here, before the first message, because the host fixes it when the chat is made.
  const [secret, setSecret] = useState(false);
  const [extra, setExtra] = useState<AgentDraft>(NO_AGENT);
  const edit = (patch: Partial<AgentDraft>) => setExtra((now) => ({ ...now, ...patch }));
  const editLoop = (patch: Partial<AgentDraft["loop"]>) => setExtra((now) => ({ ...now, loop: { ...now.loop, ...patch } }));
  const projects = useProjects();
  const chosenProject = (projects.data ?? []).find((p) => p.id === project);
  const folderChoice = agent && chosenProject && offersFolderChoice(chosenProject) ? agentFolders(chosenProject) : [];
  // Empty is the primary: the session then follows whichever folder is first, as one that named
  // none always has. Only another folder is named outright.
  const chosenFolder = folderChoice.find((f) => f.id === extra.folder && f.position !== 0);
  // A folder or an own directory belongs to the project it was picked in.
  useEffect(() => edit({ folder: "", ownDirectory: false }), [project]);

  // A desktop lands here to type; New agent lands on the name, its one new question. A phone keeps
  // the keyboard down until the field is touched: opening it over the list of chats hides the thing
  // the reader came to find. Nor does it take the focus from a dialog already open over this screen.
  useEffect(() => {
    if (phone || layerOpen()) return;
    if (agent) nameField.current?.focus();
    else field.current?.focus();
  }, [phone, agent]);

  async function send(words?: string) {
    const text = (words ?? draft).trim();
    const name = agent ? extra.name.trim() : "";
    // A computer or Chrome chat may begin with nothing typed: what it may control is chosen first, in
    // the sheet that opens on arrival, and the task is said after. An agent may too, once it is named:
    // it waits, idle, for its first task.
    if (busy || (!text && files.length === 0 && kind === "chat" && !name)) return;
    if (agent && extra.loop.on && !extra.loop.text.trim()) return;
    // The operator's Chrome is theirs, signed in as them: the same question the chat's own switch asks
    // before the first move to it, asked here because this chat starts there.
    if (kind === "chrome" && !(await confirmAsync(t("uc.switch.consent.title"), { body: t("uc.switch.consent.body"), action: t("uc.switch.consent.action"), danger: false }))) return;
    setBusy(true);
    let created: { id: string } | null = null;
    let sent = false;
    try {
      const loop = agent && extra.loop.on
        ? { instruction: extra.loop.text.trim(), mode: extra.loop.mode, interval_minutes: extra.loop.mode === "interval" ? Math.max(1, Number(extra.loop.minutes) || 10) : null, max_runs: extra.loop.max.trim() ? Math.max(1, Number(extra.loop.max) || 1) : null }
        : undefined;
      // The message goes out after the session exists, so a model picked here is the one that
      // reads it. Sending both in the create call would start the run on the default model.
      created = await api.post<{ id: string }>("/api/sessions", {
        // A name given is kept as given; otherwise the first message names the chat.
        autotitle: !name,
        title: name || text || files[0]?.name || (kind === "chat" ? undefined : t(`kind.title.${kind}`)),
        kind: kind === "chat" ? undefined : kind,
        preset: chosen && "preset" in chosen ? chosen.preset : undefined,
        project_id: project || undefined,
        ...(agent ? { folder_id: chosenFolder?.id, own_directory: !!project && extra.ownDirectory, tools_off: extra.toolsOff, loop } : {}),
        ...run.body,
        ...privateBody(secret),
      });
      // The model and the effort in one change, before the message: the first turn is the one
      // that has to think the way the operator asked.
      const model = chosen && !("preset" in chosen) ? { model: chosen.model } : null;
      if (model || effort) {
        await api.post(`/api/sessions/${created.id}/model`, { ...model, ...(effort ? effortBody(effort) : {}) });
      }
      // The consent the operator just gave, recorded on the chat as its own switch records it.
      if (kind === "chrome") await api.put(`${USER_STATUS}/sessions/${encodeURIComponent(created.id)}`, { env: "user", consent: true });
      if (!text && files.length === 0) {
        // Nothing to send yet: a computer or Chrome chat opened empty, or an idle agent.
      } else if (files.length) {
        const form = new FormData();
        if (text) form.append("text", text);
        for (const file of files) form.append("files", file);
        setProgress(0);
        await api.upload(`/api/sessions/${created.id}/upload`, form, setProgress);
      } else {
        await api.post(`/api/sessions/${created.id}/messages`, { text });
      }
      sent = true;
      setDraft("");
      setFiles([]);
      setEffort(null);
      setSecret(false);
      setExtra(NO_AGENT);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
      setProgress(null);
    }
    if (created && sent) {
      invalidate("/api/sessions");
      // A chat made without a first message starts no run, so no run event would bring it into its
      // project's list; read the projects again too.
      invalidate("/api/projects");
      if (kind === "computer") requestComputerSheet(created.id);
      if (kind === "chrome" && !offers.chromeConnected) toast(t("uc.switch.offline"));
      navigate(sessionPath(created.id));
    } else if (created) {
      // A 150 MB video refused mid-upload used to leave the screen for the new, empty session: the
      // words and the file were gone and the list held a conversation with nothing in it. The draft
      // stays here to send again, and the empty session goes.
      void api.delete(`/api/sessions/${created.id}`).catch(() => undefined).finally(() => invalidate("/api/sessions"));
    }
  }

  // The picked files are read before the input is cleared: read inside the state update, which runs
  // later, they came from a list the clearing had already emptied, and nothing attached ever showed.
  const addFiles = (incoming: Iterable<File>) => {
    const picked = Array.from(incoming);
    if (picked.length) setFiles((held) => [...held, ...picked]);
  };
  const openFile = (file: File) => {
    const url = URL.createObjectURL(file);
    window.open(url, "_blank", "noopener");
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  // The voice note, as the chat's composer has it: the words land after what is typed, or go at
  // once with ↑. Read at the moment they arrive, not when the recording began.
  const draftNow = useRef(draft);
  draftNow.current = draft;
  const note = useVoiceNote({
    sessionId: "",
    asr,
    onWords: (words, go) => {
      const landed = landWords(draftNow.current, words, go);
      if (landed.send) void send(landed.text);
      else {
        setDraft(landed.text);
        window.setTimeout(() => field.current?.focus(), 0);
      }
    },
    onAttach: (file) => addFiles([file]),
    toast,
  });
  const voiceBar = note.state.phase === "recording" || note.state.phase === "transcribing";
  const chooseEffort = (value: string) => setEffort(effortOf(value));
  const placeholder = secret ? t("private.placeholder") : agent ? t("start.agent.task") : kind === "chat" ? t("session.composer.idle") : t(`kind.placeholder.${kind}`);
  const input = (
    <CodeField
      hidden={voiceBar}
      ref={field}
      value={draft}
      placeholder={placeholder}
      ariaLabel={agent ? t("start.agent.task") : t("session.composer.idle")}
      onChange={setDraft}
      onPaste={(event) => {
        const pasted = Array.from(event.clipboardData?.items ?? []).filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter((file): file is File => !!file);
        if (!pasted.length) return;
        event.preventDefault();
        addFiles(pasted);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" && !event.shiftKey && enterSends()) {
          event.preventDefault();
          void send();
        }
      }}
    />
  );
  // The agent's name heads the field: the one question New agent asks that a chat does not. Enter in
  // it goes on to the task, so a name and a task are typed without the mouse.
  const nameInput = agent && !voiceBar && (
    <input ref={nameField} className="start-agent-name" value={extra.name} maxLength={128} placeholder={t("start.agent.name")} aria-label={t("start.agent.name.label")}
      onChange={(event) => edit({ name: event.target.value })}
      onKeyDown={(event) => {
        if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
        event.preventDefault();
        if ((event.ctrlKey || event.metaKey) && enterSends()) void send();
        else field.current?.focus();
      }} />
  );
  // The loop's own instruction, open under the field while the loop is on: what each wake-up is for.
  const loopFields = agent && extra.loop.on && (
    <div className="start-agent-loop" role="group" aria-label={t("newagent.loop")}>
      <label className="field" htmlFor="start-loop-text">{t("newagent.loop.label")}</label>
      <textarea id="start-loop-text" className="field" rows={2} value={extra.loop.text} onChange={(e) => editLoop({ text: e.target.value })} placeholder={t("newagent.loop.placeholder")} />
      <div className="start-agent-loop-row">
        <select className="field" value={extra.loop.mode} aria-label={t("newagent.loop")} onChange={(e) => editLoop({ mode: e.target.value as "interval" | "dynamic" })}>
          <option value="interval">{t("newagent.loop.interval")}</option>
          <option value="dynamic">{t("loop.selfpaced")}</option>
        </select>
        {extra.loop.mode === "interval" && <input className="field" type="number" min={1} value={extra.loop.minutes} onChange={(e) => editLoop({ minutes: e.target.value })} aria-label={t("newagent.loop.minutes")} />}
        <input className="field" type="number" min={1} placeholder={t("newagent.loop.max")} value={extra.loop.max} onChange={(e) => editLoop({ max: e.target.value })} aria-label={t("newagent.loop.max")} />
      </div>
      <div className="sub">{t("newagent.loop.note")}</div>
    </div>
  );
  const toolsSheet = toolsOpen && (
    <Sheet title={t("tools.title")} onClose={() => setToolsOpen(false)} size="narrow">
      <ToolPicker off={extra.toolsOff} onChange={(off) => edit({ toolsOff: off })} note={t("newagent.tools.note")} />
    </Sheet>
  );
  const foldersSheet = foldersOpen && (
    <Sheet title={t("newagent.folder")} onClose={() => setFoldersOpen(false)} size="narrow" className="start-agent-folders">
      {folderChoice.map((f) => (
        <SheetRow key={f.id} icon="folder" label={folderName(f)} hint={f.position === 0 ? t("start.agent.folder.primary") : undefined} disabled={!f.reachable}
          checked={(chosenFolder?.id ?? "") === (f.position === 0 ? "" : f.id)} onClick={() => { edit({ folder: f.position === 0 ? "" : f.id }); setFoldersOpen(false); }} />
      ))}
    </Sheet>
  );
  const folderWord = chosenFolder ? folderName(chosenFolder) : folderChoice[0] ? folderName(folderChoice[0]) : "";

  const empty = !draft.trim() && files.length === 0;
  // Send stays live on an empty field for a computer or Chrome chat, which may open with nothing typed,
  // and for an agent with a name, which is made idle.
  const idle = empty && kind === "chat" && !(agent && extra.name.trim());
  const blocked = agent && extra.loop.on && !extra.loop.text.trim();
  const sendLabel = agent ? t("start.agent.create") : t("session.send");
  const check = (on: boolean) => on ? <Icon name="check" size={16} /> : null;
  const agentItems = agent && (
    <>
      <div className="menu-heading sub">{t("kind.mode.agent")}</div>
      {project && <button type="button" role="menuitemcheckbox" aria-checked={extra.ownDirectory} data-option="own-directory" title={t("newagent.directory.own.hint")} onClick={() => edit({ ownDirectory: !extra.ownDirectory })}>
        <Icon name="folderplus" size={16} /><span className="grow">{t("newagent.directory.own")}</span>{check(extra.ownDirectory)}
      </button>}
      {folderChoice.length > 0 && <button type="button" role="menuitem" data-option="folder" onClick={() => { setPlusOpen(false); setFoldersOpen(true); }}>
        <Icon name="folder" size={16} /><span className="grow">{t("newagent.folder")}</span><span className="sub truncate">{folderWord}</span>
      </button>}
      <button type="button" role="menuitemcheckbox" aria-checked={extra.loop.on} data-option="loop" title={t("newagent.loop.hint")} onClick={() => { setPlusOpen(false); editLoop({ on: !extra.loop.on }); }}>
        <Icon name="loop" size={16} /><span className="grow">{t("newagent.loop")}</span>{check(extra.loop.on)}
      </button>
      <button type="button" role="menuitem" data-option="tools" onClick={() => { setPlusOpen(false); setToolsOpen(true); }}>
        <Icon name="wrench" size={16} /><span className="grow">{t("tools.title")}</span><span className="sub">{extra.toolsOff.length ? t("start.agent.tools.off", { n: extra.toolsOff.length }) : t("start.agent.tools.all")}</span>
      </button>
    </>
  );
  const plusItems = (
    <>
      <button type="button" role="menuitem" onClick={() => { setPlusOpen(false); fileInput.current?.click(); }}><Icon name="attach" size={16} />{t("session.attach")}</button>
      {asr?.configured && <button type="button" role="menuitem" disabled={!note.supported || note.state.phase !== "idle"} onClick={() => { setPlusOpen(false); void note.start(); }}><Icon name="mic" size={16} />{t("session.mic")}</button>}
      <button type="button" role="menuitemcheckbox" aria-checked={secret} data-private="" className={`private-item ${secret ? "on" : ""}`} title={`${t("private.menu.hint")}. ${t("private.newagent.hint")}`} onClick={() => { setPlusOpen(false); setSecret(!secret); }}>
        <Icon name="eyeoff" size={16} /><span className="grow">{t("private.menu")}</span>{secret && <Icon name="check" size={16} />}
      </button>
      {agentItems}
    </>
  );
  // What the agent will be, said once under its field: the choices made under + that show nowhere else.
  const chosenWords = agent ? [
    project && extra.ownDirectory ? t("newagent.directory.own") : "",
    chosenFolder ? folderName(chosenFolder) : "",
    extra.toolsOff.length ? t("start.agent.tools.off", { n: extra.toolsOff.length }) : "",
  ].filter(Boolean) : [];
  const chosenLine = chosenWords.length > 0 && !voiceBar && <div className="start-agent-chosen sub" data-agent-chosen="">{chosenWords.join(" · ")}</div>;

  if (!phone) {
    return (
      <div className="start-composer composer" data-primary="send" data-agent={agent ? "" : undefined} data-private={secret ? "" : undefined} onDragOver={(event) => { if (event.dataTransfer?.types.includes("Files")) event.preventDefault(); }} onDrop={(event) => { if (!event.dataTransfer?.files.length) return; event.preventDefault(); addFiles(event.dataTransfer.files); }}>
        <VoiceNoteFailed note={note} />
        <div className={`composer-box ${voiceBar ? "voicing" : ""}`}>
          {voiceBar && <VoiceBar note={note} />}
          {progress !== null && <div className="sub upload-progress" role="status">{t("upload.progress", { percent: Math.floor(progress * 100) })}</div>}
          {files.length > 0 && !voiceBar && (
            <div className="attachments" aria-label={t("session.attachments")}>
              {files.map((file, i) => (
                <AttachmentCard key={`${file.name}-${file.size}-${file.lastModified}-${i}`} file={file} onOpen={() => openFile(file)} onRemove={() => setFiles((held) => held.filter((_, j) => j !== i))}
                  onReplace={(marked) => setFiles((held) => held.map((one) => (one === file ? marked : one)))} />
              ))}
            </div>
          )}
          {nameInput}
          {input}
          {chosenLine}
          <div className="composer-row" hidden={voiceBar}>
            <input ref={fileInput} type="file" multiple hidden onChange={(event) => { addFiles(event.target.files ?? []); event.target.value = ""; }} />
            <button ref={plusButton} type="button" className="iconbtn flat plus" aria-haspopup="menu" aria-expanded={plusOpen} onClick={() => setPlusOpen(!plusOpen)} aria-label={t("composer.plus")} title={t("composer.plus")}><Icon name="plus" /></button>
            {plusOpen && <Popover anchor={plusButton.current} onClose={() => setPlusOpen(false)} className="plus-menu" label={t("composer.plus")}>{plusItems}</Popover>}
            {/* What the chat will be and where it runs read together on the left, as the chat's own
                composer has its mode there; the model and the circle keep the right. */}
            <KindSelect kind={kind} offers={offers} onChoose={onKind} sheet={false} agent={agent} onAgent={onAgent} />
            <RunOnSelect run={run} />
            <PrivateChip on={secret} onClear={() => setSecret(false)} />
            {agent && extra.loop.on && <button type="button" className="chip start-agent-chip" data-option="loop" onClick={() => editLoop({ on: false })} title={t("start.agent.loop.off")} aria-label={t("start.agent.loop.off")}><Icon name="loop" size={12} />{t("newagent.loop")}<Icon name="close" size={12} /></button>}
            <div className="composer-tools">
              <ModelSelect model={modelLabel} fallback={null} open={modelOpen} onOpenChange={setModelOpen} onChoose={choose} sheet={phone}
                effort={shownEffort.thinking ? shownEffort.effort : undefined} thinking={shownEffort.thinking} onChooseEffort={chooseEffort} />
              {asr?.configured && <MicButton note={note} />}
              {/* One white circle, as on the phone: Send, greyed while the field is empty. Voice mode is
                  not offered here: it is a beta with a chat of its own, and a new chat offers dictation. */}
              <button type="button" className="roundbtn primary" data-action="send" onClick={() => void send()} disabled={busy || idle || blocked} aria-label={sendLabel} title={sendLabel}><Icon name="up" /></button>
            </div>
          </div>
        </div>
        {loopFields}
        {toolsSheet}
        {foldersSheet}
      </div>
    );
  }

  // A phone: one 48 px row at rest, the text above a toolbar once the field has the reader or holds
  // something (ui/phone.css reads data-shape). The white circle is Send, greyed while the field is
  // empty; offline the draft waits here and Send waits with it. An agent's composer is always open:
  // its name sits above the field.
  const shape = agent || focused || !empty || progress !== null ? "open" : "idle";
  return (
    <div className="start-composer composer" data-primary="send" data-shape={shape} data-typed={draft.trim() ? "" : undefined} data-agent={agent ? "" : undefined} data-private={secret ? "" : undefined}
      onFocus={focus.onFocus}
      onBlur={focus.onBlur}>
      <VoiceNoteFailed note={note} />
      {loopFields}
      <div className={`composer-box ${voiceBar ? "voicing" : ""}`} onClick={(e) => { if (e.target === e.currentTarget) field.current?.focus(); }}>
        {voiceBar && <VoiceBar note={note} />}
        {progress !== null && <div className="sub upload-progress" role="status">{t("upload.progress", { percent: Math.floor(progress * 100) })}</div>}
        {files.length > 0 && !voiceBar && (
          <div className="attachments" aria-label={t("session.attachments")}>
            {files.map((file, i) => (
              <AttachmentCard key={`${file.name}-${file.size}-${file.lastModified}-${i}`} file={file} onOpen={() => openFile(file)} onRemove={() => setFiles((held) => held.filter((_, j) => j !== i))}
                onReplace={(marked) => setFiles((held) => held.map((one) => (one === file ? marked : one)))} />
            ))}
          </div>
        )}
        {nameInput}
        {input}
        {chosenLine}
        <div className="composer-row" hidden={voiceBar}>
          <input ref={fileInput} type="file" multiple hidden onChange={(event) => { addFiles(event.target.files ?? []); event.target.value = ""; }} />
          <input ref={photoInput} type="file" accept="image/*" capture="environment" hidden onChange={(event) => { addFiles(event.target.files ?? []); event.target.value = ""; }} />
          <button ref={plusButton} type="button" className="iconbtn flat plus" aria-haspopup="dialog" aria-expanded={plusOpen} onClick={() => setPlusOpen(!plusOpen)} aria-label={t("composer.plus")} title={t("composer.plus")}><Icon name="plus" /></button>
          <PrivateChip on={secret} onClear={() => setSecret(false)} />
          {agent && extra.loop.on && <button type="button" className="chip start-agent-chip" data-option="loop" onClick={() => editLoop({ on: false })} title={t("start.agent.loop.off")} aria-label={t("start.agent.loop.off")}><Icon name="loop" size={12} /><Icon name="close" size={12} /></button>}
          {offline && shape === "open" && <span className="grow ph-hint">{t("ph.offline.kept")}</span>}
          <div className="composer-tools">
            <ModelSelect model={modelLabel} fallback={null} open={modelOpen} onOpenChange={setModelOpen} onChoose={choose} sheet
              effort={shownEffort.thinking ? shownEffort.effort : undefined} thinking={shownEffort.thinking} onChooseEffort={chooseEffort} />
            {asr?.configured && <MicButton note={note} />}
            <button type="button" className="roundbtn primary" data-action="send" onClick={() => void send()} disabled={busy || offline || idle || blocked} aria-label={sendLabel} title={sendLabel}><Icon name="up" /></button>
          </div>
        </div>
      </div>
      {plusOpen && (
        <PlusSheet onClose={() => setPlusOpen(false)}
          onPhoto={() => photoInput.current?.click()}
          onFiles={() => fileInput.current?.click()}
          onRecord={asr?.configured && note.supported && note.state.phase === "idle" ? () => void note.start() : undefined}
          extra={<>
            <SheetRow icon="eyeoff" label={t("private.menu")} hint={`${t("private.menu.hint")}. ${t("private.newagent.hint")}`} checked={secret} data={{ private: "" }} onClick={() => { setSecret(!secret); setPlusOpen(false); }} />
            {!agent && <SheetRow icon={AGENT_ICON} label={t("start.agent.switch")} hint={t("kind.mode.agent.hint")} chevron data={{ option: "agent" }} onClick={() => { setPlusOpen(false); onAgent(true); }} />}
            {agent && project && <SheetRow icon="folderplus" label={t("newagent.directory.own")} hint={t("newagent.directory.own.hint")} checked={extra.ownDirectory} data={{ option: "own-directory" }} onClick={() => edit({ ownDirectory: !extra.ownDirectory })} />}
            {agent && folderChoice.length > 0 && <SheetRow icon="folder" label={t("newagent.folder")} value={folderWord} chevron data={{ option: "folder" }} onClick={() => { setPlusOpen(false); setFoldersOpen(true); }} />}
            {agent && <SheetRow icon="loop" label={t("newagent.loop")} hint={t("newagent.loop.hint")} checked={extra.loop.on} data={{ option: "loop" }} onClick={() => { setPlusOpen(false); editLoop({ on: !extra.loop.on }); }} />}
            {agent && <SheetRow icon="wrench" label={t("tools.title")} value={extra.toolsOff.length ? t("start.agent.tools.off", { n: extra.toolsOff.length }) : t("start.agent.tools.all")} chevron data={{ option: "tools" }} onClick={() => { setPlusOpen(false); setToolsOpen(true); }} />}
          </>} />
      )}
      {toolsSheet}
      {foldersSheet}
    </div>
  );
}

const AGENT_ICON = "user" as const;
