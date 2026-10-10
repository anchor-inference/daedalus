// The phone's navigation: one drawer from the left in place of the five tabs and their More sheet.
// It holds the switch between the two modes, which was invisible before (a tab you happened to have
// last used), every global destination, the mode's own lists (Projects and Recents, or the main
// orchestrator and the orchestrated projects), and the one accent creation pill. Settings is the
// round button beside the pill. Every screen's hamburger opens it (ui/phone.tsx, openDrawer).

import { useEffect, useMemo, useState } from "react";
import type { Project, SessionList } from "./api";
import { SelfDevMode, screenTag, visibleScreens } from "./capabilities";
import { relTime } from "./format";
import { agentName, arrange, splitPinned } from "./grouping";
import { plural, t } from "./i18n";
import { Icon, type IconName } from "./icons";
import { useMain } from "./main/data";
import { type Mode, agentsListingOf, orchestratedProjects } from "./mode";
import { ORCHESTRATION, ORCHESTRATION_LIST, type Screen, pathFor, projectHome, sessionPath, useRoute } from "./router";
import { type Counts, ICONS, countFor, screenTitle } from "./shell";
import { useQuery } from "./store";
import { useStreamUp } from "./events";
import { LangPicker } from "./ui/components";
import { BottomSheet, Drawer, IconButton, NewChatPill, SegmentedControl, SheetRow, closeDrawer, navigateFromDrawer, useDrawerOpen } from "./ui/phone";

/** Where Chats lives: the agents screen with its list open, since /app/agents/<id> is a session. */
export const CHATS_PATH = pathFor("agents", null, { view: "chats" });

/** The daily destinations of each mode, in the drawer's order; the rest wait in More. */
const AGENTS_PLACES: Screen[] = ["inbox", "terminals", "board", "voice", "usage"];
const ORCHESTRATION_PLACES: Screen[] = ["inbox", "terminals", "usage"];
const MORE: Screen[] = ["calendar", "diagrams", "changes", "schedules", "services", "memory", "harnesses", "health"];
const BETA: Screen[] = ["voice"];
const RECENTS = 8;
const PROJECTS = 5;

/** Initials for a project's avatar: the first letters of its first two words. */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] ?? "") + (words[1]?.[0] ?? words[0]?.[1] ?? "")).toUpperCase();
}

const STAFF_COLOURS = ["orange", "green", "blue", "violet", "rose", "teal", "amber", "slate"];
export function colourOf(id: string): string {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `var(--staff-${STAFF_COLOURS[h % STAFF_COLOURS.length]})`;
}

export type AppDrawerProps = {
  mode: Mode;
  counts: Counts;
  /** What waits for the operator in orchestration mode. */
  waiting: number;
  selfdev: SelfDevMode;
  /** Agents mode's projects (none with an orchestrator), for the project lens. */
  projects: Project[];
  onPickProject: (id: string) => void;
  /** The project switcher: every project, adding one, the archived ones. */
  onProjects: () => void;
};

export function AppDrawer({ mode, counts, waiting, selfdev, projects, onPickProject, onProjects }: AppDrawerProps) {
  const open = useDrawerOpen();
  const route = useRoute();
  // The switch shows a mode's lists without leaving the page; a destination inside it goes there.
  const [shown, setShown] = useState<Mode>(mode);
  const [more, setMore] = useState(false);
  useEffect(() => { if (open) setShown(mode); }, [open, mode]);
  const live = useStreamUp();
  const listing = useQuery<SessionList>("/api/sessions", { pollMs: live ? 60000 : 15000, staleMs: 5000 });
  const { data: main } = useMain();
  const agents = useMemo(() => agentsListingOf(listing.data), [listing.data]);
  // The pinned block heads the menu as it heads the desktop's sidebar, and what is in it is not
  // listed again under Projects or Recents.
  const pinned = useMemo(() => (agents ? splitPinned(arrange(agents.sessions, agents.projects).folders).pinned : []), [agents]);
  const pinnedIds = useMemo(() => new Set(pinned.map((f) => f.key)), [pinned]);
  const recents = useMemo(() => [...(agents?.sessions ?? [])]
    .filter((s) => !s.metadata?.subagent_of && !s.archived && !pinnedIds.has(s.project_id))
    .sort((a, b) => Date.parse(b.last_message_at) - Date.parse(a.last_message_at))
    .slice(0, RECENTS), [agents, pinnedIds]);
  const orchestrated = useMemo(() => orchestratedProjects(Array.isArray(listing.data?.projects) ? listing.data!.projects : []), [listing.data]);
  const agentsWaiting = (agents?.sessions ?? []).filter((s) => s.status === "waiting").length;
  const folderOf = (id: string) => agents?.projects.find((p) => p.id === id);
  const moreScreens = visibleScreens(MORE, selfdev);
  const moreCount = moreScreens.reduce((n, s) => n + countFor(s, counts), 0);
  const here = (s: Screen) => route.screen === s && !route.session;

  const place = (s: Screen) => {
    const n = countFor(s, counts);
    const tag = screenTag(s, selfdev, BETA);
    return (
      <a key={s} className={`ph-nrow ${here(s) ? "on" : ""}`} href={pathFor(s)} data-screen={s} aria-current={here(s) ? "page" : undefined} onClick={(e) => { e.preventDefault(); navigateFromDrawer(pathFor(s)); }}>
        <Icon name={ICONS[s]} size={20} />
        <span className="ph-nrow-l">{screenTitle(s)}</span>
        {tag && <span className="ph-tag">{t(tag)}</span>}
        {n > 0 && <span className="ph-badge">{n > 99 ? "99+" : n}</span>}
      </a>
    );
  };
  const nav = (key: string, icon: IconName, label: string, path: string, active: boolean, trail?: React.ReactNode) => (
    <a key={key} className={`ph-nrow ${active ? "on" : ""}`} href={path} data-nav={key} aria-current={active ? "page" : undefined} onClick={(e) => { e.preventDefault(); navigateFromDrawer(path); }}>
      <Icon name={icon} size={20} />
      <span className="ph-nrow-l">{label}</span>
      {trail}
    </a>
  );
  const onChats = route.screen === "agents" && !route.session && route.query.get("view") === "chats";

  const footer = (
    <>
      {shown === "agents"
        ? <NewChatPill label={t("ph.newchat")} onClick={() => navigateFromDrawer(pathFor("agents"))} />
        : <NewChatPill label={t("ph.newproject")} icon="plus" onClick={() => { closeDrawer(); onProjects(); }} />}
      <span className="grow" />
      <button type="button" className="ph-round" aria-label={screenTitle("settings")} title={screenTitle("settings")} data-screen="settings" onClick={() => navigateFromDrawer(pathFor("settings"))}>
        <Icon name="settings" size={20} />
      </button>
    </>
  );

  return (
    <>
      <Drawer open={open} onClose={closeDrawer} label={t("nav.menu")} footer={footer}>
        <div className="ph-brand">
          <span className="ph-brand-name"><Icon name="logo" size={22} />Daedalus</span>
          <IconButton icon="search" label={t("search.label")} onClick={() => navigateFromDrawer(pathFor("agents", null, { view: "chats", search: "1" }))} />
        </div>
        <SegmentedControl<Mode>
          label={t("ph.mode")}
          value={shown}
          onChange={setShown}
          options={[
            { id: "agents", label: t("mode.agents"), badge: shown !== "agents" && agentsWaiting > 0 ? <span className="ph-badge quiet">{agentsWaiting}</span> : undefined },
            { id: "orchestration", label: t("mode.orchestration"), badge: shown !== "orchestration" && waiting > 0 ? <span className="ph-badge warn" data-waiting={waiting}>{waiting > 99 ? "99+" : waiting}</span> : undefined },
          ]}
        />
        {shown === "agents" ? (
          <>
            {nav("chats", "bots", t("ph.chats"), CHATS_PATH, onChats, agents ? <span className="ph-cnt">{agents.sessions.filter((s) => !s.metadata?.subagent_of).length}</span> : undefined)}
            {visibleScreens(AGENTS_PLACES, selfdev).map(place)}
            <button type="button" className="ph-nrow" onClick={() => setMore(true)} aria-haspopup="dialog" data-nav="more">
              <Icon name="grid" size={20} />
              <span className="ph-nrow-l">{t("nav.more")}</span>
              {moreCount > 0 ? <span className="ph-badge">{moreCount}</span> : <span className="ph-cnt">{moreScreens.length}</span>}
            </button>
            {pinned.length > 0 && <div className="ph-dsec"><span className="ph-pinhead"><Icon name="pushpin" size={14} />{t("side.pinned")}</span></div>}
            {pinned.map((f) => {
              const s = f.chat ? f.rows[0]?.s : undefined;
              return s ? (
                <button key={f.key} type="button" className={`ph-drow ${route.session === s.id ? "on" : ""}`} data-pinned="" data-session={s.id} onClick={() => navigateFromDrawer(sessionPath(s.id))}>
                  <span className={`ph-dot ${s.status}`} aria-hidden />
                  <span className="ph-drow-t">{agentName(s)}</span>
                  <span className="ph-drow-tm">{relTime(s.last_message_at)}</span>
                </button>
              ) : (
                <button key={f.key} type="button" className="ph-drow" data-pinned="" data-project={f.key} onClick={() => { onPickProject(f.key); navigateFromDrawer(CHATS_PATH); }}>
                  <Icon name={f.system ? "mic" : "folder"} size={18} />
                  <span className="ph-drow-t">{f.name}</span>
                  <span className="ph-drow-tm">{plural("agents.count", f.total)}</span>
                </button>
              );
            })}
            {/* The project switcher (every project, adding one, the archived ones) is "See all"
                here, even with no project yet: it is where the first one is added. */}
            <div className="ph-dsec">{t("shell.projects")}<button type="button" className="ph-dsec-act" data-nav="projects" onClick={() => { closeDrawer(); onProjects(); }}>{t(projects.length ? "ph.seeall" : "shell.projects.add")}</button></div>
            {projects.length > 0 && (
              <>
                {projects.filter((p) => !pinnedIds.has(p.id)).slice(0, PROJECTS).map((p) => {
                  const folder = folderOf(p.id);
                  return (
                    <button key={p.id} type="button" className="ph-drow" data-project={p.id} onClick={() => { onPickProject(p.id); navigateFromDrawer(CHATS_PATH); }}>
                      <Icon name="folder" size={18} />
                      <span className="ph-drow-t">{p.name}</span>
                      {folder && <span className="ph-drow-tm">{plural("agents.count", folder.total)}</span>}
                    </button>
                  );
                })}
              </>
            )}
            {recents.length > 0 && <div className="ph-dsec">{t("ph.recents")}</div>}
            {recents.map((s) => (
              <button key={s.id} type="button" className={`ph-drow ${route.session === s.id ? "on" : ""}`} data-session={s.id} onClick={() => navigateFromDrawer(sessionPath(s.id))}>
                <span className={`ph-dot ${s.status}`} aria-hidden />
                <span className="ph-drow-t">{agentName(s)}</span>
                <span className="ph-drow-tm">{relTime(s.last_message_at)}</span>
              </button>
            ))}
          </>
        ) : (
          <>
            {nav("main", "conductor", t("main.title"), ORCHESTRATION, route.screen === "orchestration" && !route.project && route.detail !== "projects",
              main && waiting > 0 ? <span className="ph-badge warn">{waiting > 99 ? "99+" : waiting}</span> : undefined)}
            {visibleScreens(ORCHESTRATION_PLACES, selfdev).map(place)}
            <button type="button" className="ph-nrow" onClick={() => setMore(true)} aria-haspopup="dialog" data-nav="more">
              <Icon name="grid" size={20} />
              <span className="ph-nrow-l">{t("nav.more")}</span>
              {moreCount > 0 ? <span className="ph-badge">{moreCount}</span> : <span className="ph-cnt">{moreScreens.length}</span>}
            </button>
            <div className="ph-dsec">{t("orch.projects")}<button type="button" className="ph-dsec-act" data-nav="orchestration-list" onClick={() => navigateFromDrawer(ORCHESTRATION_LIST)}>{t("ph.seeall")}</button></div>
            {orchestrated.map((p) => {
              const o = p.orchestrator!;
              const meta = [p.last_message_at ? relTime(p.last_message_at) : "", plural("team.count.staff", o.staff), o.working > 0 ? plural("team.count.working", o.working) : ""].filter(Boolean).join(" · ");
              return (
                <button key={p.id} type="button" className={`ph-drow tall ${route.project === p.id ? "on" : ""}`} data-project={p.id} onClick={() => navigateFromDrawer(projectHome(p.id))}>
                  <span className="ph-avatar" style={{ ["--c" as string]: colourOf(p.id) }} aria-hidden>{initials(p.name)}</span>
                  <span className="ph-drow-main">
                    <span className="ph-drow-t">{p.name}</span>
                    <span className="ph-drow-m">{meta}</span>
                  </span>
                  {o.needs_you > 0 && <span className="ph-badge warn" aria-label={plural("focus.entry.needs", o.needs_you)}>{o.needs_you}</span>}
                </button>
              );
            })}
            {listing.data && orchestrated.length === 0 && <div className="ph-dnote">{t("orch.empty.touch")}</div>}
          </>
        )}
      </Drawer>
      {more && (
        <BottomSheet title={t("nav.more")} onClose={() => setMore(false)} className="ph-more-sheet">
          {moreScreens.map((s) => {
            const n = countFor(s, counts);
            const tag = screenTag(s, selfdev, BETA);
            return <SheetRow key={s} data={{ screen: s }} icon={ICONS[s]} label={screenTitle(s)} hint={tag ? t(tag) : undefined} value={n > 0 ? <span className="ph-badge">{n}</span> : undefined} onClick={() => { setMore(false); navigateFromDrawer(pathFor(s)); }} />;
          })}
          <div className="ph-msep" />
          {/* The language is changed from here as well as from Settings: a reader who cannot read the
              page cannot be asked to find a section of Settings first. */}
          <SheetRow icon="globe" label={t("lang.menu")}><LangPicker /></SheetRow>
        </BottomSheet>
      )}
    </>
  );
}
