import { useContextActions } from "../ui/context-menu";
import { Component, Fragment, createContext, memo, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import type { ReactElement, ReactNode } from "react";
import { api, ApiError, AsrStatus, type AutopilotMark, type AutopilotStatus, ModelFallback, ProviderUsage, Schedule, SessionCheckpoints, SlashCommand, SkillEntry, MessageView, RunOutcome, SessionDetail, Compacting, type SessionFolder, type Question } from "../api";
import { Chevron, Dot, Status, copyText, fmtInt, statusWord, timeAgo } from "../ui/components";
import { MenuItem, OverflowMenu, confirmDialog, Overlay } from "../ui/dialogs";
import { absDate, clock, commandPreview, duration, plainPreview, shortDateTime } from "../format";
import { HostMark } from "../runon";
import { isProject } from "../grouping";
import { readPrefs, type Prefs } from "../appearance";
import { EVIDENCE_EVENT, EvidenceRequest, codeBlock, renderCached, renderMarkdown } from "../md";
import { confirmAsync, errorText, fmtBytes, haptic } from "../ui";
import { Icon, IconName } from "../icons";
import { AuthImg, FilePreview, PreviewSource, canPreview, downloadHref, fileGlyph, previewKind, sessionBase } from "../preview";
import { followSession } from "../sessionstream";
import { Activity, LiveStore, Segment, SummaryItem, SystemNote, ToolItem, Turn, activitySummary, applyLive, buildTurns, createLiveStore, familyCounts, isOlderPage, liveAfter, liveBase, prepend, producedFiles, reconcile, splitActivity } from "../turns";
import { Explorer } from "../explorerpanel";
import { DiffView } from "../previewparts";
import { looksLikeDiff } from "../diff";
import { KeptFiles, TurnFiles } from "../artifact";
import { placeOf, turnFiles, turnPages } from "../turnfiles";
import { PageCards } from "../pagecard";
import { WidgetFrames } from "../widgetframe";
import { drawnWidgets } from "../widget";
import { useReveal } from "../reveal";
import { withoutAttachedList } from "../keptfiles";
import { FileGallery, InlineMedia, mediaCopyText, splitMediaAnswer } from "../media";
import { conversationMedia, galleryFor, toolSource, viewerSource, type HostSource } from "../mediaroute";
import { Answer, Composer, ComposerHandle } from "../composerbox";
import { Approval, QueuedMessage, pendingApproval, queuedAfter, questionsKey, readQueued } from "../composer";
import { ModelChoice } from "../modelselect";
import { AUTO_MODE, ModeInfo, modeName } from "../modeselect";
import { effortWord } from "../effortselect";
import { DepthChip } from "../depthchip";
import { MoveSessionSheet } from "../projects";
import { MakeProjectSheet, makeProjectCommand } from "../chatproject";
import { panelShortcut } from "../panel";
import { Panel, usePanel, usePanelWidth } from "../panelhost";
import { SessionDetails } from "../details";
import { HandoffLine, ImportBanner } from "../imports/ImportedChat";
import { harnessMeta } from "../imports/model";
import { JobsTab } from "../jobs";
import { markAnchor, navigate, pathFor, projectHome, projectSessionPath, sessionPath, useAnchor, useRoute } from "../router";
import { turnFor, withOlder } from "../anchor";
import { ChatSearch } from "../chatsearch";
import { useMedia } from "../ui/index";
import { SCROLL_KEYS, Windowed, keepOpened, openedLine, stillAtEnd, type OpenedLine, type WindowedHandle } from "../virtual";
import { DICT, plural, t } from "../i18n";
import { groupDetail, groupName, searchedGroup } from "../toolgroups";
import { usePresenceScope } from "../presence";
import { endsTerminals, TerminalButton, TerminalDock, TerminalFull, TerminalSheet, useSessionTerminals, useTerminalDock } from "../terminal/dock";
import { dockShortcut, insideTerminal, isMac } from "../terminal/keys";
import { tabsFor, type PanelTab } from "../panel";
import { EventCard, FocusChat, FocusChatContext, MessageFate, ReplyChip, ReplyQuote, StepsCard, currentReply, setReply, stepDescription, useAnsweredMark, useFocusChat } from "../project/chat";
import { GoalStrip } from "../project/goalstrip";
import { excerptOf, messageBody, uploadText } from "../project/goalmodel";
import { QuestionsLine, QuestionsPanel } from "../questions/QuestionsPanel";
import { AskRecord } from "../questions/AskRecord";
import { useQuestions, type QuestionScope } from "../questions/data";
import { StaffHeader, StaffMessages, useMember } from "../project/staff";
import { BriefPage, FoldersPage, WakeupsPage } from "../project/pages";
import { worktreesKey, type Listing as WorktreeListing } from "../project/ProjectWorktrees";
import { worktreeFolders } from "../project/worktreefolders";
import { ProjectBoard } from "../board/ProjectBoard";
import { useMain } from "../main/data";
import { ShareSheet } from "../share";
import { deviceSaving, useBrowsers } from "../browser/data";
import { BrowserTab } from "../browser/BrowserPanel";
import { BrowserPip } from "../browser/pip";
import { BrowserHeadButton, BrowserNeedsRow, useFirstOpenToast } from "../browser/phone";
import { BrowserLoginCard, loginAsk } from "../browser/login";
import { pipGroup } from "../browser/model";
import { orchestrationPathOf } from "../mode";
import { FullResult, ToolResultView } from "../toolresult";
import { SettingsChangeCard, settingsChangeIds } from "../settingschange";
import { useQuery } from "../store";
import { sentMessageReachedTranscript } from "../pending-message";
import { Banner, IconButton } from "../ui/phone";
import { SecretChip } from "../secrets";
import { RawHtml } from "../rawhtml";
import { RenameSheet, SearchSheet, SessionMenuSheet, SessionTopBar, StateSub, type SessionRow, type SessionTile } from "./SessionPhone";
import { SubagentsTab, subagentIdFor } from "../subagents";
import { PromptCoach } from "../coach/PromptCoach";
import { SideQuestion } from "../btw/SideQuestion";
import { COMPOSE_EVENT, ComposeTarget, type ComposeRequest } from "../annotate/compose";
import { noticeFromEvent, raiseFromError, raiseSettingNotice, SettingNoticeLines, settingRef } from "../settinglink";

/**
 * Markdown parsed once per text. `cacheKey` names a message that will never change again, so its
 * rendering survives the component: a turn scrolled out of the window and back in is not re-parsed.
 */
const Md = memo(function Md({ text, className, cacheKey }: { text: string; className?: string; cacheKey?: string }) {
  const html = useMemo(() => (cacheKey ? renderCached(cacheKey, text) : renderMarkdown(text)), [text, cacheKey]);
  return <RawHtml className={className} html={html} />;
});

/** How much of the end of the conversation a run's event is answered with. */
const TAIL_AFTER_EVENT = 24;
/** Events come in bursts; one read answers the burst. */
const EVENT_COALESCE_MS = 150;
/** How many older messages a page holds when the reader scrolls past the top. */
const OLDER_PAGE = 200;
/** How many older pages a link to one message may read to reach it: four thousand messages back.
 *  Further than that the chat stops and says so, rather than reading a whole history for one link. */
const ANCHOR_PAGES = 20;
/** Where the message a link opens is put: a little below the top of the screen, not under its edge. */
const ANCHOR_MARGIN_PX = 12;
/** How long that message stays lit, so the eye finds it among the turns around it. */
const ANCHOR_LIT_MS = 2600;
/**
 * Less than this left below the fold and the newest-message button is not offered. It was 160 px,
 * which is three or four lines of an answer: the end of a reply sat under the composer, cut mid-
 * sentence, with nothing on screen to say there was more.
 */
const AT_END_PX = 32;

// ── screen ────────────────────────────────────────────────────────────────────────────────

export type SessionScreenProps = {
  id: string;
  onBack: () => void;
  onOpen?: (id: string) => void;
  toast: (t: string) => void;
  /** Two sessions side by side (wide screens): which half this one is. */
  pane?: "left" | "right";
  /** Open another session beside this one; absent when the screen cannot split. */
  onSplit?: () => void;
  /** Inside a project's focus mode: the project, and whether this is its orchestrator's chat or a
   *  session of the project (a staff member's, or anyone else's working there). */
  focus?: { projectId: string; kind: "orchestrator" | "member" };
  /** Under the header, above the conversation: a project's waiting request on a phone. */
  banner?: ReactNode;
  /** In the conversation's flow, after its latest turn: the main chat's open questions and the work
   *  under way, which scroll with the chat rather than standing over it. */
  flow?: ReactNode;
  /** The composer's words while nothing runs, when the chat is not an ordinary agent's. */
  placeholder?: string;
  /** The main chat: its panel leads with every orchestrated project's questions. */
  main?: boolean;
  /** Opened by its plain address in Agents mode: a session that belongs to orchestration mode (the
   *  main chat, anything of a project with an orchestrator) moves there as soon as it is known to. */
  leaveForOrchestration?: boolean;
  /** Drawn inside another page (the companion's): the side panel starts closed whatever was last
   *  open elsewhere, and never writes the address, which belongs to the page around it. */
  embedded?: boolean;
  /** Drawn inside a small window that has a title bar of its own (the prompt coach's): no header, no
   *  terminals, no panel, and none of the chat's own tools in the composer — only the conversation. */
  bare?: boolean;
};

export function SessionScreen({ id, onBack, onOpen, toast, pane, onSplit, focus, banner, flow, placeholder, main = false, leaveForOrchestration = false, embedded = false, bare = false }: SessionScreenProps) {
  // Each pane says which session it shows, so a split view reports both and the voice screen's
  // embedded session reports itself, without anybody reading the address.
  usePresenceScope({ session: id || undefined });
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const { data: sessionDiagrams } = useQuery<{ id: string; title: string }[]>(`/api/diagrams?session_id=${encodeURIComponent(id)}`, { pollMs: 5000, staleMs: 0 });
  const [sendingMessages, setSendingMessages] = useState<{ id: string; text: string }[]>([]);
  // The streaming turn's state is not React state: a token must repaint the turn it belongs to,
  // not the screen. The components that show it subscribe; everything else never hears about it.
  const liveRef = useRef<LiveStore | null>(null);
  if (!liveRef.current) liveRef.current = createLiveStore();
  const live = liveRef.current;
  const [moving, setMoving] = useState(false);
  const [promoting, setPromoting] = useState(false);
  const [sharing, setSharing] = useState(false);
  const composer = useRef<ComposerHandle>(null);
  // The right panel: the route carries the open tab and the previewed file for the pane the URL
  // names; the second pane of a dual view keeps its panel to itself. Phones host it in a sheet.
  const route = useRoute();
  const phone = !useMedia("(min-width: 1024px)");
  const panelContext = focus ? focus.kind : main ? "main" : "session";
  // The session's browsers: the Browser tab is offered only once it has had one.
  const browsers = useBrowsers(id ? { session: id } : null);
  // Offered too while the operator's Chrome is connected (or chosen here): the switch lives in the tab.
  const hasBrowser = browsers.groups.length > 0 || !!(browsers.user && (browsers.user.connected || browsers.user.env === "user"));
  // In focus mode the panel's tab is written into the project's own address, so opening the board
  // beside the orchestrator does not leave the project for the agents list. The main chat keeps its
  // panel in orchestration's home for the same reason. Written as the session's plain address, every
  // toggle and tab left orchestration for /app/agents/<main>, drew the Agents column and a second
  // conversation there, and came back once that conversation moved itself home: the whole shell was
  // built twice on each click, and Back stepped through the detour.
  const panelAt = useMemo(
    () => (focus ? (q: Record<string, string | null>) => (focus.kind === "orchestrator" ? projectHome(focus.projectId, q) : projectSessionPath(focus.projectId, id, q))
      : main ? (q: Record<string, string | null>) => pathFor("orchestration", null, q)
      : undefined),
    [focus?.projectId, focus?.kind, id, main],
  );
  const tabsNow = useRef<PanelTab[]>([]);
  const panel = usePanel(id, sessionBase(id), { route: pane === "right" || embedded || bare ? null : route.query, beside: pane === "left" ? route.with : null, pane, context: panelContext, at: panelAt, closed: embedded || bare, shown: () => tabsNow.current });
  // The prompt coach's window over this chat, with the draft it was opened on; `raise` brings an open
  // window back up when the button is pressed again. It belongs to this chat and closes when the chat changes.
  const [coach, setCoach] = useState<{ draft: string; raise: number } | null>(null);
  useEffect(() => setCoach(null), [id]);
  // The side question's window (/btw), with what was asked; `ask` changes with each /btw or press of the
  // button. Like the coach's, it belongs to this chat and closes when the chat changes.
  const [btw, setBtw] = useState<{ question: string; ask: number } | null>(null);
  useEffect(() => setBtw(null), [id]);
  // An ordinary session's side questions decide whether it has a Questions tab at all, so they are read
  // before the tabs are; the orchestrator's and the main chat's lists are read below, with their scope.
  const sideWaiting = useQuestions(panelContext === "session" && !bare ? { sessionId: id } : null).questions?.length ?? 0;
  const panelTabs = tabsNow.current = tabsFor(panelContext, { browser: hasBrowser || panel.state.tab === "browser", preview: panel.state.stack.length > 0, subagents: (detail?.subagents?.length ?? 0) > 0 || panel.state.tab === "subagents", questions: sideWaiting > 0 || panel.state.tab === "questions" });
  const browserShown = pipGroup(browsers.groups);
  const runningSubagents = detail?.subagents?.filter((child) => child.running).length ?? 0;
  // Every way the conversation names a helper — its SubAgent step, its report, the reminder about the
  // idle ones — opens it here, beside the leader, rather than taking the leader off the screen.
  const subagentsNow = useRef(detail?.subagents);
  subagentsNow.current = detail?.subagents;
  const openSubagentRef = useCallback((ref: { id?: string; name?: string } | null) => {
    if (!ref) return panel.openSubagent(null);
    const child = subagentIdFor(subagentsNow.current ?? [], ref);
    if (child) panel.openSubagent(child);
    else toast(t("subagents.gone"));
  }, [panel.openSubagent, toast]);
  const openBrowser = useCallback(() => panel.open("browser"), [panel.open]);
  useFirstOpenToast(browsers.groups, phone && panel.state.tab !== "browser", openBrowser);
  const orchestrating = focus?.kind === "orchestrator";
  // A link from before orchestration mode, a notification or the palette may name an orchestrated
  // session by its plain address; only the session itself says where it belongs, so the move waits
  // for it. The address is replaced, so Back does not return to the plain one and move again.
  const mainSession = useMain(leaveForOrchestration).data?.session_id;
  const orchestrationPath = leaveForOrchestration && detail ? orchestrationPathOf(detail, mainSession) : null;
  useEffect(() => {
    if (orchestrationPath) navigate(orchestrationPath + window.location.search, { replace: true, keepHash: true });
  }, [orchestrationPath]);
  const staffName = useMember(focus && detail?.staff ? detail.staff.id : null).data?.name ?? "";
  // The orchestrator's file pane opens the project's folders and, read-only, each staff member's
  // worktree, where their work stays until it is merged. The listing is read again whenever the Files
  // tab opens, since a member may have started a worktree since the last look.
  const worktrees = useQuery<WorktreeListing>(orchestrating ? worktreesKey(focus!.projectId) : null, { staleMs: 5000 });
  const filesOpen = panel.state.tab === "files";
  useEffect(() => {
    if (orchestrating && filesOpen) void worktrees.refresh();
  }, [orchestrating, filesOpen]); // eslint-disable-line react-hooks/exhaustive-deps -- refresh follows its key
  const stagedFolders = useMemo(
    () => (orchestrating ? worktreeFolders(worktrees.data, detail?.folders ?? NO_FOLDERS, detail?.project?.folders ?? []) : NO_FOLDERS),
    [orchestrating, worktrees.data, detail?.folders, detail?.project?.folders],
  );
  // The worktrees follow the project's folders in the pane's switch. The chat's file cards read the
  // same list, and placeOf picks the innermost folder holding a path, so a file a member wrote opens
  // from that member's worktree rather than from the project folder around it.
  const paneFolders = useMemo(() => (stagedFolders.length ? [...(detail?.folders ?? NO_FOLDERS), ...stagedFolders] : detail?.folders ?? NO_FOLDERS), [stagedFolders, detail?.folders]);
  // What waits for the operator: the orchestrator's project's list, every project's in the main chat,
  // or an ordinary session's own side questions. The cards live in the panel's Questions tab; the
  // chat carries one line that opens it.
  const sideQuestions = panelContext === "session";
  const questionScope = useMemo<QuestionScope | null>(() => (orchestrating ? { projectId: focus!.projectId } : main ? "all" : sideQuestions ? { sessionId: id } : null), [orchestrating, focus?.projectId, main, sideQuestions, id]);
  const questions = useQuestions(questionScope);
  const waiting = questions.questions?.length ?? 0;
  // A session's side questions have a tab only while some wait (or the tab is open, so the cards just
  // answered can say so before they go); the orchestrator's and the main chat's lists always do.
  const questionsShown = !!questionScope && (!sideQuestions || panelTabs.includes("questions"));
  // A direct question already has an actionable line beside the composer. Its generic goal alert
  // used to add a second amber callout and another Open button for the same decision.
  const showGoalLine = questions.questions?.length === 0 || (questions.questions === null && Boolean(questions.error));
  // In the orchestrator's chat anything can be answered: the quote waits over the composer, which
  // takes the focus so the operator types the answer straight away.
  const focusChat = useMemo<FocusChat | null>(() => (focus ? {
    projectId: focus.projectId, orchestrator: orchestrating, toast,
    reply: orchestrating ? (target) => { setReply(id, target); composer.current?.focus(); } : undefined,
  } : null), [focus?.projectId, orchestrating, toast, id]);
  const body = useRef<HTMLDivElement>(null);
  const [panelPct, dragPanel] = usePanelWidth(body);
  // The session's terminals: the dock under the conversation on a desktop, a sheet and a full-screen
  // view on a phone. Listed here because the header's button, the dock and the delete dialog all count them.
  const sessionTerminals = useSessionTerminals(id);
  const [detailsFocus, setDetailsFocus] = useState<string | null>(null);
  const [editingTitle, setEditingTitle] = useState<string | null>(null);
  // The phone's sheets: the ⋮ menu and the three it leads to that the desktop shows in place.
  const [phoneSheet, setPhoneSheet] = useState<"menu" | "search" | "rename" | null>(null);
  const [modes, setModes] = useState<ModeInfo[]>([]);
  // The guessed next message the composer offers once an answer has settled; empty when there is none.
  const [suggestion, setSuggestion] = useState("");
  useEffect(() => setSuggestion(""), [id]);
  const [commands, setCommands] = useState<SlashCommand[]>([]);
  const [skills, setSkills] = useState<SkillEntry[]>([]);
  const [commandResult, setCommandResult] = useState<{ line: string; text: string } | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const userScrolling = useRef(false);
  /** Where the list was at the last scroll event: a move up from here is the reader leaving the end. */
  const lastTop = useRef(0);
  /** The line the reader just opened or closed, and where it was on the screen, for a moment after. */
  const opened = useRef<OpenedLine | null>(null);
  const [atBottom, setAtBottom] = useState(true);
  const [preview, setPreview] = useState<PreviewSource | null>(null);
  const [gallery, setGallery] = useState<{ items: HostSource[]; index: number } | null>(null);
  const [filesGeneration, setFilesGeneration] = useState(0);
  const [receipt, setReceipt] = useState<string | null>(null);
  const [dragging, setDragging] = useState(0);
  const [providerUsage, setProviderUsage] = useState<ProviderUsage | null>(null);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [asr, setAsr] = useState<AsrStatus | null>(null);
  const [snapshots, setSnapshots] = useState<SessionCheckpoints | null>(null);
  // What the host is holding for the running agent: follow-ups for the end of the turn, steers for
  // its next model call. `none` is a host without the route: the cards are simply not drawn, and the
  // route is not asked again.
  const [queuedInput, setQueuedInput] = useState<QueuedMessage[]>([]);
  const steerRoute = useRef<"unknown" | "ok" | "none">("unknown");
  // Approval keys the operator has spent or waved away, so the dock does not offer them twice.
  const [seenKeys, setSeenKeys] = useState<Set<string>>(() => new Set());

  async function loopAction(a: string) {
    try {
      await api.post(`/api/sessions/${id}/loop/action`, { action: a });
      toast(t("session.loop.action", { action: a }));
      load(true);
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function setMode(mode: string) {
    try {
      await api.post(`/api/sessions/${id}/mode`, { mode: mode === "default" || !mode ? null : mode });
      toast(t("session.mode.toast", { mode: modeName(mode === "default" ? "" : mode) }));
      load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function setAutopilot(on: boolean) {
    try {
      const status = await api.post<AutopilotStatus>(`/api/sessions/${id}/autopilot`, { on });
      toast(t(!on ? "composer.autopilot.toast.off" : status.enabled ? "composer.autopilot.toast.on" : "composer.autopilot.toast.disabled"));
      load();
    } catch (e) {
      toast(errorText(e));
      load();
    }
  }

  async function setYagni(on: boolean) {
    try {
      await api.post(`/api/sessions/${id}/yagni`, { on });
      toast(t(on ? "composer.yagni.toast.on" : "composer.yagni.toast.off"));
      load();
    } catch (e) {
      toast(errorText(e));
      load();
    }
  }

  // Existing snapshots describe which files can be restored; deleting history never creates one.
  const readSnapshots = useCallback(async () => {
    try {
      setSnapshots(await api.get<SessionCheckpoints>(`/api/sessions/${id}/checkpoints`));
    } catch {
      setSnapshots(null);
    }
  }, [id]);

  const turnAction = useCallback(
    async (kind: "revert" | "fork" | "retry", seq: number) => {
      try {
        if (kind === "retry") {
          if (busyRef.current) {
            toast(t("session.stopfirst"));
            return;
          }
          if (!(await confirmAsync(t("session.retry.title"), { body: t("session.retry.body"), action: t("turn.retry"), danger: true }))) return;
          const result = await api.post<{ seq: number; through: number }>(`/api/sessions/${id}/retry`, { seq });
          cutMessages(result.seq, result.through);
          stick.current = true;
          load();
          return;
        }
        if (kind === "revert") {
          // With snapshots off — which is a project's default — there is nothing to put the files
          // back from, and the operator should read that before clicking rather than in the toast after.
          const noSnapshots = detail?.project?.settings.snapshots === false;
          const body = t(noSnapshots ? "session.revert.body.nosnapshots" : "session.revert.body");
          if (!(await confirmAsync(t("session.revert.title"), { body, action: t("session.revert.action"), danger: true }))) return;
          // Read before the cut removes it: the reverted message goes back into the composer, so
          // undoing a turn to reword it does not mean typing it again from memory.
          const reverted = msgs.current.find((m) => m.seq === seq && m.role === "user");
          const r = await api.post<{ seq: number; through: number; dropped: number; workspace_restored: boolean; untouched: string[] }>(`/api/sessions/${id}/revert`, { seq });
          cutMessages(r.seq, r.through);
          const words = reverted ? withoutAttachedList(reverted.text).trim() : "";
          if (words) composer.current?.insert(words, !phone);
          const ws = r.workspace_restored
            ? r.untouched.length
              ? t("session.reverted.ws.nested", { n: r.untouched.length })
              : t("session.reverted.ws")
            : t("session.reverted.ws.none");
          toast(plural("session.reverted", r.dropped, { ws }));
          void readSnapshots();
        } else {
          const r = await api.post<{ id: string; title: string; messages: number }>(`/api/sessions/${id}/fork`, { seq });
          toast(t("session.fork.done", { title: r.title, n: r.messages }));
        }
        load();
      } catch (e) {
        toast(errorText(e));
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [id, toast, readSnapshots, detail?.project?.settings.snapshots, phone],
  );

  const [offline, setOffline] = useState(false);
  // What the screen holds, kept where code can read it without waiting for a render: a merge has to
  // decide whether the history has a hole in it before React gets to the next commit.
  const msgs = useRef<MessageView[]>([]);
  // Reads overlap — an event read and the safety-net poll — and they come back in whatever order the
  // network gives them. Only the newest one is allowed to land, so a slow older answer cannot put an
  // older status back on the screen.
  const readSeq = useRef(0);
  const historyReadSeq = useRef(0);
  function cutMessages(seq: number, through: number) {
    // The upper bound matters: retry may already have persisted its new answer before HTTP returns.
    historyReadSeq.current = ++readSeq.current;
    msgs.current = msgs.current.filter((m) => m.seq == null || m.seq < seq || m.seq > through);
    setDetail((previous) => previous ? { ...previous, messages: msgs.current } : previous);
  }
  const load = useCallback(
    async (quiet = false) => {
      const mine = ++readSeq.current;
      try {
        const next = await api.get<SessionDetail>(`/api/sessions/${id}`);
        if (mine !== readSeq.current) return;
        // The older pages the reader went back to stay; see `withOlder`.
        msgs.current = withOlder(msgs.current, next.messages);
        historyReadSeq.current = mine;
        setDetail({ ...next, messages: msgs.current });
        setOffline(false);
      } catch (e) {
        // Timers and the event stream retry by themselves: one banner in the header, not a toast every few seconds.
        setOffline(true);
        if (!quiet) toast(errorText(e));
      }
    },
    [id, toast],
  );

  // A different session is a different conversation: nothing the last one held may be folded into it,
  // and an answer still on its way belongs to the one that was left.
  useEffect(() => {
    msgs.current = [];
    setSendingMessages([]);
    historyReadSeq.current = ++readSeq.current;
    return () => forgetDisclosed(id);
  }, [id]);

  // A run changes the end of the conversation and nothing else, so a run's events are answered by
  // reading the end of it. `TAIL_AFTER_EVENT` messages is more than a round adds; when it turns out
  // not to reach what the screen already holds, `reconcile` says so and the whole thing is re-read.
  const refresh = useCallback(
    async (kind: "tail" | "state") => {
      const mine = ++readSeq.current;
      try {
        const next = await api.get<SessionDetail>(`/api/sessions/${id}?tail=${kind === "tail" ? TAIL_AFTER_EVENT : 1}`);
        if (kind === "state" && mine !== readSeq.current) return;
        if (kind === "tail" && mine < historyReadSeq.current) return;
        setOffline(false);
        if (kind === "state") {
          setDetail((prev) => (prev ? { ...next, messages: prev.messages } : next));
          return;
        }
        const merged = reconcile(msgs.current, next.messages);
        if (merged.gap) {
          await load(true);
          return;
        }
        msgs.current = merged.messages;
        historyReadSeq.current = mine;
        // A later status-only poll must not discard a completed history read: that used to
        // leave the preceding run as the streaming base after a long run finished.
        setDetail((prev) => (prev ? { ...(mine === readSeq.current ? next : prev), messages: merged.messages } : next));
      } catch (e) {
        setOffline(true);
        void e;
      }
    },
    [id, load],
  );

  // Events arrive in bursts — a message ends, the state changes, the run settles — and one read
  // answers all of them.
  const queued = useRef<{ timer: number; kind: "tail" | "state" } | null>(null);
  const refreshSoon = useCallback(
    (kind: "tail" | "state") => {
      const pendingRead = queued.current;
      if (pendingRead) {
        if (kind === "tail") pendingRead.kind = "tail";
        return;
      }
      const timer = window.setTimeout(() => {
        const wanted = queued.current?.kind ?? kind;
        queued.current = null;
        void refresh(wanted);
      }, EVENT_COALESCE_MS);
      queued.current = { timer, kind };
    },
    [refresh],
  );
  useEffect(() => () => {
    if (queued.current) window.clearTimeout(queued.current.timer);
    queued.current = null;
  }, [id]);

  // Older messages, when the reader scrolls past the top of what was loaded. The API may not take
  // the cursor yet, in which case it answers with the tail it always answers with — that is not an
  // older page, and asking again would only repeat it, so the screen stops asking.
  const [older, setOlder] = useState<"more" | "loading" | "done">("more");
  /** How far the reader was from the end when an older page went in, so they stay where they were. */
  const keepFromEnd = useRef<number | null>(null);
  /** One page at a time: the rendered range asks as often as the reader scrolls. */
  const olderBusy = useRef(false);
  const loadOlder = useCallback(async () => {
    const oldest = detail?.messages[0]?.seq;
    if (oldest == null || oldest <= 0) {
      setOlder("done");
      return;
    }
    if (olderBusy.current) return;
    olderBusy.current = true;
    setOlder("loading");
    try {
      const page = await api.get<SessionDetail>(`/api/sessions/${id}?before=${oldest}&tail=${OLDER_PAGE}`);
      if (!isOlderPage(page.messages, oldest)) {
        setOlder("done");
        return;
      }
      const el = scroller.current;
      keepFromEnd.current = el ? el.scrollHeight - el.scrollTop : null;
      msgs.current = prepend(msgs.current, page.messages);
      setDetail((prev) => (prev ? { ...prev, messages: msgs.current } : prev));
      setOlder(page.messages.length < OLDER_PAGE ? "done" : "more");
    } catch {
      setOlder("more");
    } finally {
      olderBusy.current = false;
    }
  }, [id, detail?.messages]);

  useEffect(() => {
    load();
    api.get<Record<string, { description?: string }>>("/api/modes").then((m) => setModes(Object.entries(m).map(([name, v]) => ({ name, description: v?.description ?? "" })))).catch(() => setModes([]));
    api.get<SlashCommand[]>("/api/commands").then(setCommands).catch(() => setCommands([]));
    api.get<SkillEntry[]>("/api/skills").then(setSkills).catch(() => setSkills([]));
    api.get<AsrStatus>("/api/asr").then(setAsr).catch(() => setAsr(null));
    readSnapshots();
  }, [load, readSnapshots]);

  // The scheduled tasks that belong to this session: created from it, aimed at it, or running in it now.
  const loadSchedules = useCallback(() => {
    api
      .get<Schedule[]>("/api/schedules")
      .then((all) => setSchedules(all.filter((x) => x.target_session === id || x.created_by_session === id || x.active_session_id === id)))
      .catch(() => undefined);
  }, [id]);
  useEffect(() => {
    loadSchedules();
    const t = setInterval(loadSchedules, 60000);
    return () => clearInterval(t);
  }, [loadSchedules]);

  // Usage of the provider the session talks to: a subscription's windows, or the day's metered spend.
  const provider = detail?.provider ?? "";
  useEffect(() => {
    if (!provider) return;
    let gone = false;
    const pull = () =>
      api
        .get<ProviderUsage>(`/api/usage/provider/${encodeURIComponent(provider)}`)
        .then((u) => !gone && setProviderUsage(u))
        .catch(() => undefined);
    pull();
    const t = setInterval(pull, 60000);
    return () => {
      gone = true;
      clearInterval(t);
    };
  }, [provider, detail?.usage.c]);

  async function scheduleAction(sc: Schedule, action: "run" | "delete") {
    try {
      if (action === "delete") {
        if (!(await confirmAsync(t("session.sched.delete.title", { name: sc.name })))) return;
        await api.delete(`/api/schedules/${sc.id}`);
        toast(t("session.sched.deleted"));
      } else {
        const r = await api.post<{ session_id: string }>(`/api/schedules/${sc.id}/run`);
        toast(t(r.session_id === id ? "session.sched.runninghere" : "session.sched.runningown"));
        if (r.session_id !== id) onOpen?.(r.session_id);
      }
      loadSchedules();
    } catch (e) {
      toast(errorText(e));
    }
  }

  const status = (detail?.status ?? "idle") as Status;
  const busy = status === "running" || status === "waiting";
  // The run is over, its answer is on the screen, and behind it the snapshot of the turn and the
  // handover to the other fronts are still being written. Not a run — nothing is being generated,
  // and the composer is open — but not nothing either: it is exactly the window in which an undo
  // waits for the files and a restart is refused, and drawing it as fully idle is what made both
  // of those read as the app misbehaving. The host says so twice, once to raise it and once to
  // take it away; between the two this is what there is to show.
  const saving = !busy && !!detail?.housekeeping;
  const compacting = detail?.compacting ?? null;
  // The summary written ahead of the trigger: shown beside whatever the session is doing, never in its place.
  const ahead = !compacting ? detail?.compaction_background ?? null : null;
  const busyRef = useRef(busy);
  busyRef.current = busy;

  // What the host is holding for the running agent. Read when a run begins and after every message
  // this screen queues; kept current from the stream, which carries the whole queue in each event.
  const loadQueued = useCallback(async () => {
    if (steerRoute.current === "none") return;
    try {
      const raw = await api.get<unknown>(`/api/sessions/${id}/steer`);
      steerRoute.current = "ok";
      setQueuedInput(readQueued(raw));
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) steerRoute.current = "none";
      setQueuedInput([]);
    }
  }, [id]);
  useEffect(() => {
    steerRoute.current = "unknown";
    setQueuedInput([]);
  }, [id]);
  useEffect(() => {
    if (busy) void loadQueued();
    else setQueuedInput([]);
  }, [busy, loadQueued]);
  const withdraw = useCallback(
    async (message: QueuedMessage) => {
      try {
        await api.delete(`/api/sessions/${id}/steer/${encodeURIComponent(message.id)}`);
        setQueuedInput((q) => q.filter((x) => x.id !== message.id));
        toast(t("composer.steer.withdrawn"));
      } catch (e) {
        // A 409 is the message having gone through after all; the list is re-read either way.
        toast(e instanceof ApiError && e.status === 409 ? t("composer.steer.gone") : errorText(e));
        void loadQueued();
      }
    },
    [id, toast, loadQueued],
  );
  // A follow-up the operator decides should not wait for the turn to end: the host moves it to the
  // steer queue, and the card stays, saying it is now read at the next step.
  const steerNow = useCallback(
    async (message: QueuedMessage) => {
      try {
        await api.post(`/api/sessions/${id}/steer/${encodeURIComponent(message.id)}`, {});
        setQueuedInput((q) => q.map((x) => (x.id === message.id ? { ...x, kind: "steer" } : x)));
        toast(t("composer.delivery.steered"));
      } catch (e) {
        toast(e instanceof ApiError && e.status === 409 ? t("composer.steer.gone") : errorText(e));
        void loadQueued();
      }
    },
    [id, toast, loadQueued],
  );

  // A tool call the policy refused, waiting on the operator: the dock above the pill offers it once.
  const approval = useMemo(() => pendingApproval(detail?.messages ?? [], seenKeys), [detail?.messages, seenKeys]);
  const approve = useCallback(
    async (a: Approval) => {
      try {
        await api.post(`/api/sessions/${id}/policy/grant`, { key: a.key });
        setSeenKeys((k) => new Set(k).add(a.key));
        toast(a.chrome ? t("composer.chrome.switched", { name: a.chrome.name }) : t("composer.approved"));
      } catch (e) {
        toast(errorText(e));
      }
    },
    [id, toast],
  );
  // What "Allow similar" would grant for the request in the dock, asked of the host because only it
  // still holds the refused call's arguments; the button says it before it is pressed.
  const [similar, setSimilar] = useState<{ key: string; label: string } | null>(null);
  const approvalKey = approval?.key ?? "";
  useEffect(() => {
    setSimilar(null);
    if (!approvalKey) return;
    let gone = false;
    api.get<{ similar?: { label?: unknown } | null }>(`/api/sessions/${id}/policy/similar/${approvalKey}`)
      .then((r) => {
        const label = r?.similar?.label;
        if (!gone && typeof label === "string" && label) setSimilar({ key: approvalKey, label });
      })
      .catch(() => undefined);
    return () => {
      gone = true;
    };
  }, [id, approvalKey]);
  const approveSimilar = useCallback(
    async (a: Approval) => {
      try {
        const r = await api.post<{ resolved?: unknown; similar?: { label?: unknown } }>(`/api/sessions/${id}/policy/grant-similar`, { key: a.key });
        const answered = Array.isArray(r?.resolved) ? r.resolved.filter((k): k is string => typeof k === "string") : [];
        setSeenKeys((k) => new Set([...k, a.key, ...answered]));
        toast(t("composer.approved.similar", { label: typeof r?.similar?.label === "string" ? r.similar.label : similar?.label ?? "" }));
      } catch (e) {
        toast(errorText(e));
      }
    },
    [id, toast, similar],
  );
  // Refusing tells the host too, so the request stops being open on every other front that shows it.
  const deny = useCallback(
    async (a: Approval) => {
      setSeenKeys((k) => new Set(k).add(a.key));
      try {
        await api.post(`/api/sessions/${id}/policy/refuse`, { key: a.key });
      } catch (e) {
        toast(errorText(e));
      }
    },
    [id, toast],
  );

  // The event stream carries every change while a run is active; this is the safety net, not the
  // feed, and it asks what the session is doing — not for the conversation over again.
  useEffect(() => {
    if (!busy) {
      live.reset();
      return;
    }
    const t = setInterval(() => refresh("state"), 20000);
    return () => clearInterval(t);
  }, [busy, live, refresh]);

  // Live events while the screen is open.
  useEffect(() => {
    // Whatever the last stream was in the middle of is over as far as this screen knows when it
    // drops, and a half-written turn kept across the gap would put the cursor back under an answer
    // that finished while the connection was down. The re-read brings back everything that is real.
    const stop = followSession(id, { handle, onDrop: () => { live.reset(); load(true); } });
    function handle(event: string, p: Record<string, any>) {
      const activeRun = live.get().runId;
      if (event === "run_settled" && p.run_id && activeRun && p.run_id !== activeRun) return;
      live.update((s) => liveAfter(s, event, p));
      if (event === "message_start") {
        setSuggestion("");
        // The queue the last run left behind starts the next one without anybody pressing send:
        // the chip says so as the first token arrives, not at the next read.
        // A run that starts also clears the last one's failure: it is being answered, not explained.
        setDetail((prev) => (prev ? { ...prev, run_id: p.run_id || prev.run_id, status: (prev.status === "idle" || prev.status === "failed") ? "running" : prev.status, error: "" } : prev));
        refreshSoon("tail");
      } else if (event === "error") {
        // Keep the provider's reason even when it failed before producing any reply.
        const message = String(p.message ?? "");
        if (message) setDetail((prev) => (prev ? { ...prev, error: message } : prev));
        // A stop a setting answers (a spend cap) offers the way to it in the conversation; the
        // host's notification about the same stop brings the toast.
        const setting = settingRef(p.setting);
        if (setting && message) raiseSettingNotice({ tone: "error", title: t("setting.notice.refused"), body: message, setting, sessionId: id, toast: false });
      } else if (event === "compaction_background") {
        // Not a state change: the session goes on running or waiting while the summary is written.
        setDetail((prev) => (prev ? { ...prev, compaction_background: (p.compaction as Compacting | null) ?? null } : prev));
      } else if (event === "setting_notice") {
        // An auxiliary model was not set or refused. Inline only: the host posts the toast itself.
        const notice = noticeFromEvent(id, p);
        if (notice) raiseSettingNotice({ ...notice, toast: false });
      } else if (event === "message_stop") {
        // The streamed copy is dropped only once the written one is on the screen, so the answer
        // never blinks out and back in. The cursor does not wait for that read — `liveAfter` has
        // already ended the turn — and so the read being slow costs nothing anybody can see.
        const stopped = live.get();
        void refresh("tail").then(() => live.update((s) => s === stopped ? { ...s, text: "", thinking: "" } : s));
      } else if (event === "model_changed") {
        // The header must not wait for the next read to stop naming a model that is not answering:
        // a fallback is at its most confusing in the seconds right after it happens.
        setDetail((prev) =>
          prev
            ? {
                ...prev,
                effective_model: String(p.to ?? ""),
                fallback: p.fallback ? { from: String(p.configured ?? p.from ?? ""), to: String(p.to ?? ""), reason: String(p.reason ?? "") } : null,
              }
            : prev,
        );
      } else if (event === "run_settled") {
        setFilesGeneration((n) => n + 1);
        // The run is over as the host knows it. The chip flips on this event, not on the read it
        // triggers: the read says the same thing a round trip later.
        setDetail((prev) => (prev ? { ...prev, status: p.status === "awaiting" ? "waiting" : p.status === "failed" ? "failed" : "idle", housekeeping: !!p.housekeeping } : prev));
        refreshSoon("tail");
        // The answer has settled: ask once for the operator's likely next message. The host keeps one
        // guess per answer, so the second settle of the same run costs nothing.
        if (p.status === "completed" && !p.housekeeping) {
          void api.get<{ text?: string }>(`/api/sessions/${id}/suggestion`).then((r) => setSuggestion(r.text ?? "")).catch(() => setSuggestion(""));
        }
      } else if (event === "history_cut") {
        cutMessages(Number(p.seq), Number(p.through));
        live.reset();
        void load(true);
      } else if (event === "steer_changed") {
        steerRoute.current = "ok";
        setQueuedInput((q) => queuedAfter(q, p));
        if (p.reason === "consumed") refreshSoon("tail");
      } else if (event === "compaction_completed") refreshSoon("tail");
      else if (event === "state_changed" || event === "tool_call_pending") refreshSoon("state");
    }
    return stop;
  }, [id, live, load, refresh, refreshSoon]);

  // The settled conversation, built once per message that lands. A turn whose messages did not
  // change comes back as the same object, so `memo` on the view holds and a streamed token
  // re-renders the turn it belongs to instead of all of them.
  const built = useRef<Turn[]>([]);
  const turns = useMemo(() => {
    built.current = buildTurns(detail?.messages ?? [], built.current);
    return built.current;
  }, [detail?.messages]);
  useEffect(() => {
    setSendingMessages((current) => current.filter((pending) => !sentMessageReachedTranscript(detail?.messages, pending.id)));
  }, [detail?.messages]);
  // What the receipts under the operator's messages measure "read" against.
  useAnsweredMark(id, detail?.messages, orchestrating);
  const tail = busy ? liveBase(turns, detail?.run_id) : null;
  const settled = useMemo(() => (tail ? turns.slice(0, -1) : turns), [turns, tail]);
  const turnKeys = useMemo(() => settled.map((t) => t.key), [settled]);

  // A page of older messages is put in front of everything the reader is looking at, so the screen
  // moves down by its height unless the distance to the end is restored.
  useLayoutEffect(() => {
    const el = scroller.current;
    const keep = keepFromEnd.current;
    if (!el || keep == null) return;
    keepFromEnd.current = null;
    el.scrollTop = el.scrollHeight - keep;
  }, [detail?.messages]);

  // A history short enough to have arrived whole has no page before it: it is never asked for one,
  // and the line that says a page is on its way is not in its way.
  const pageable = older !== "done" && (detail?.messages.length ?? 0) >= OLDER_PAGE;

  // The windowed list says when the oldest turn it holds is on screen. Held in a ref so the list
  // does not re-register its scroll listener every time a message lands.
  const wantOlder = useRef<() => void>(() => undefined);
  useEffect(() => {
    wantOlder.current = () => {
      if (older === "more" && pageable) void loadOlder();
    };
  }, [older, pageable, loadOlder]);
  const onTopOfList = useCallback(() => wantOlder.current(), []);

  // A link to one message (`#m<seq>`) opens the chat at that message however far back it is: the
  // pages before the loaded ones are read until it is among them. An orchestrator's chat runs for
  // weeks, and a link into it that opened at the end was a link to nothing.
  const anchor = useAnchor();
  const windowHandle = useRef<WindowedHandle | null>(null);
  /** The message the chat was last taken to, so that a render does not take the reader there again. */
  const anchored = useRef<number | null>(null);
  /** Each jump is numbered; one that finishes after a newer one was asked for is dropped. */
  const jumps = useRef(0);
  /** Where to go once the pages are in: a seq, or -1 for the top of what could be read. */
  const [target, setTarget] = useState<{ seq: number; jump: number } | null>(null);
  const [unreached, setUnreached] = useState<"far" | "gone" | null>(null);
  const reach = useCallback(
    async (seq: number, current: () => boolean): Promise<"here" | "far" | "gone"> => {
      for (let pages = 0; ; pages++) {
        const oldest = msgs.current[0]?.seq;
        if (oldest == null) return "gone";
        if (oldest <= seq) return "here";
        if (pages >= ANCHOR_PAGES) return "far";
        // One page at a time, shared with the reader's own scrolling up: the same page read twice
        // at once would be put in front of the history twice.
        while (olderBusy.current) {
          await new Promise((resolve) => window.setTimeout(resolve, 50));
          if (!current()) return "far";
        }
        if (msgs.current[0]?.seq !== oldest) continue;
        olderBusy.current = true;
        setOlder("loading");
        try {
          const page = await api.get<SessionDetail>(`/api/sessions/${id}?before=${oldest}&tail=${OLDER_PAGE}`);
          if (!isOlderPage(page.messages, oldest)) {
            setOlder("done");
            return "gone";
          }
          msgs.current = prepend(msgs.current, page.messages);
          setDetail((prev) => (prev ? { ...prev, messages: msgs.current } : prev));
          setOlder(page.messages.length < OLDER_PAGE ? "done" : "more");
        } catch {
          setOlder("more");
          return "far";
        } finally {
          olderBusy.current = false;
        }
        if (!current()) return "far";
      }
    },
    [id],
  );
  const jump = useCallback(
    async (seq: number) => {
      const mine = ++jumps.current;
      const current = () => mine === jumps.current;
      // The reader is being taken somewhere: the end lets go of them, or the pins that follow the
      // first load would pull them back down to it.
      stick.current = false;
      setUnreached(null);
      const where = await reach(seq, current);
      if (!current()) return;
      if (where !== "here") setUnreached(where);
      setTarget({ seq: where === "here" ? seq : -1, jump: mine });
    },
    [reach],
  );
  useEffect(() => {
    anchored.current = null;
    setTarget(null);
    setUnreached(null);
    return () => {
      jumps.current += 1;
    };
  }, [id]);
  // Only the pane the address is about follows its hash, and only once the session is known to stay
  // on this screen: a plain address that is about to move to orchestration mode leaves the jump to
  // the screen it moves to, which reads the same hash.
  const follows = pane !== "right";
  const anchorReady = follows && !!detail && !orchestrationPath;
  useEffect(() => {
    if (!follows || anchor == null || anchored.current === anchor) return;
    stick.current = false;
    if (!anchorReady) return;
    anchored.current = anchor;
    void jump(anchor);
  }, [follows, anchorReady, anchor, jump]);
  // A hit of the chat's search: the same jump, and the address says where the reader is now.
  const openMessage = useCallback(
    (seq: number) => {
      markAnchor(seq);
      anchored.current = seq;
      void jump(seq);
    },
    [jump],
  );
  // At the message: its turn is rendered, put a little below the top of the screen and lit for a
  // moment. The turns above it take their real heights over the next frames and move it, so it is put
  // back a few times — until the reader takes the list over with a finger or the wheel.
  useEffect(() => {
    const host = scroller.current;
    if (!target || !host) return;
    const index = target.seq < 0 ? -1 : turnFor(turnKeys, target.seq);
    if (index < 0) {
      host.scrollTop = 0;
      return;
    }
    const key = turnKeys[index];
    windowHandle.current?.reveal(key);
    let lit: HTMLElement | null = null;
    let over = false;
    const place = () => {
      if (over || userScrolling.current) return;
      const el = windowHandle.current?.element(key) ?? null;
      if (!el) {
        windowHandle.current?.reveal(key);
        return;
      }
      const moved = el.getBoundingClientRect().top - host.getBoundingClientRect().top - ANCHOR_MARGIN_PX;
      if (Math.abs(moved) >= 1) host.scrollTop += moved;
      // The windowed list may render the turn anew as it moves; the light follows it.
      if (el !== lit) {
        lit?.classList.remove("anchored");
        el.classList.add("anchored");
        lit = el;
      }
    };
    const frame = requestAnimationFrame(place);
    const timers = [60, 180, 400, 800, 1400].map((ms) => window.setTimeout(place, ms));
    const dim = window.setTimeout(() => {
      over = true;
      lit?.classList.remove("anchored");
    }, ANCHOR_LIT_MS);
    return () => {
      cancelAnimationFrame(frame);
      timers.forEach(clearTimeout);
      window.clearTimeout(dim);
      lit?.classList.remove("anchored");
    };
    // The keys are read as they are when the target is set, which is the render the pages went in with.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target]);

  // Follow the newest content only while the reader is at the bottom and not scrolling by hand. A
  // list that grew because the reader opened something in it is not new content: the line they
  // clicked stays where it was, and the end is theirs again only if that leaves them at it. Followed,
  // opening the steps of a turn on the screen at the end threw that turn up and out of view.
  const pinBottom = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    const line = opened.current;
    if (line && keepOpened(el, line)) {
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
      stick.current = gap < 48;
      lastTop.current = el.scrollTop;
      setAtBottom(gap < AT_END_PX);
      return;
    }
    opened.current = null;
    if (stick.current && !userScrolling.current) el.scrollTop = el.scrollHeight;
  }, []);
  useEffect(pinBottom, [turns, pinBottom]);

  // Images, code blocks and the streaming turn take their height after the list has been laid out:
  // while the reader is at the end, the end is where they stay.
  useEffect(() => {
    const el = scroller.current;
    const list = el?.querySelector(".timeline");
    if (!list || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => pinBottom());
    ro.observe(list);
    return () => ro.disconnect();
  }, [pinBottom]);

  // The first paint of a long history lands mid-way once images and code blocks take their height:
  // pin the bottom again after layout settles.
  const firstLoad = useRef(true);
  useEffect(() => {
    if (!detail || !firstLoad.current) return;
    firstLoad.current = false;
    const el = scroller.current;
    if (!el) return;
    const pin = () => {
      if (stick.current) el.scrollTop = el.scrollHeight;
    };
    const raf = requestAnimationFrame(pin);
    const timers = [120, 400, 1000].map((ms) => window.setTimeout(pin, ms));
    return () => {
      cancelAnimationFrame(raf);
      timers.forEach(clearTimeout);
    };
  }, [detail]);

  // A rotation, a split pane opening, a desktop window resized: the list is a different width and
  // every turn a different height. While the reader was at the end, the end is where they stay —
  // the heights are re-measured over the next few frames, so the pin is repeated over them.
  useEffect(() => {
    const vv = window.visualViewport;
    const on = () => {
      const el = scroller.current;
      if (!el || !stick.current) return;
      const pin = () => {
        if (stick.current && scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
      };
      const timers = [0, 120, 400].map((ms) => window.setTimeout(pin, ms));
      window.setTimeout(() => timers.forEach(clearTimeout), 600);
    };
    window.addEventListener("orientationchange", on);
    window.addEventListener("resize", on);
    vv?.addEventListener("resize", on);
    return () => {
      window.removeEventListener("orientationchange", on);
      window.removeEventListener("resize", on);
      vv?.removeEventListener("resize", on);
    };
  }, []);

  function jumpToBottom() {
    const el = scroller.current;
    if (!el) return;
    stick.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
    setAtBottom(true);
  }

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    let timer: number | undefined;
    const startHand = () => {
      userScrolling.current = true;
      window.clearTimeout(timer);
      // The reader scrolling is the reader taking over: a line held after a click would otherwise
      // pull the list back while they go down the result they had just opened in full.
      opened.current = null;
    };
    const endHand = () => {
      timer = window.setTimeout(() => {
        userScrolling.current = false;
      }, 400);
    };
    // A step, the line of a turn's work, a system note: whatever the reader opens or closes is held
    // where it is while the list takes its new height.
    const toggled = (e: Event) => {
      if (e instanceof KeyboardEvent && e.key !== "Enter" && e.key !== " ") return;
      const line = openedLine(e.target, el);
      if (line) opened.current = line;
    };
    // The keyboard and the scrollbar drive the list as a wheel does.
    const keys = (e: KeyboardEvent) => {
      if (SCROLL_KEYS.has(e.key)) {
        startHand();
        endHand();
      }
    };
    // A press right of the content is on the scrollbar; the release may land anywhere on the page.
    const press = (e: PointerEvent) => {
      if (e.target !== el || e.offsetX < el.clientWidth) return;
      startHand();
      window.addEventListener("pointerup", endHand, { once: true });
    };
    el.addEventListener("click", toggled, { capture: true });
    el.addEventListener("keydown", toggled, { capture: true });
    el.addEventListener("keydown", keys);
    el.addEventListener("pointerdown", press, { passive: true });
    el.addEventListener("touchstart", startHand, { passive: true });
    el.addEventListener("touchend", endHand, { passive: true });
    el.addEventListener("wheel", startHand, { passive: true });
    el.addEventListener("wheel", endHand, { passive: true });
    return () => {
      el.removeEventListener("click", toggled, { capture: true });
      el.removeEventListener("keydown", toggled, { capture: true });
      el.removeEventListener("keydown", keys);
      el.removeEventListener("pointerdown", press);
      window.removeEventListener("pointerup", endHand);
      el.removeEventListener("touchstart", startHand);
      el.removeEventListener("touchend", endHand);
      el.removeEventListener("wheel", startHand);
      el.removeEventListener("wheel", endHand);
    };
  }, []);

  function onScroll() {
    const el = scroller.current;
    if (!el) return;
    const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
    stick.current = stillAtEnd({ pinned: stick.current, gap, top: el.scrollTop, lastTop: lastTop.current, hand: userScrolling.current });
    lastTop.current = el.scrollTop;
    // Still pinned with a gap: the history grew under the pin before this event came. Take the end again.
    if (stick.current && gap >= 48) el.scrollTop = el.scrollHeight;
    setAtBottom(stick.current || gap < AT_END_PX);
    // Near the top of what was loaded: ask for the page before it, if the API has one. A history
    // short enough to have arrived whole has no page before it and is never asked for one.
    if (el.scrollTop < 400 && older === "more" && pageable) void loadOlder();
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(0);
    if (e.dataTransfer?.files?.length) composer.current?.addFiles(e.dataTransfer.files);
  }

  // A slash command, run by the host. A short answer is a toast, a long one a sheet; a failure is
  // thrown back so the composer can put the line back in the field.
  async function runCommand(line: string) {
    const name = line.slice(1).split(" ")[0].toLowerCase();
    const spec = commands.find((c) => c.name === name);
    if (spec?.confirm && !(await confirmAsync(t("session.command.confirm", { name })))) return;
    let r: { text: string };
    try {
      r = await api.post<{ text: string }>(`/api/sessions/${id}/command`, { line });
    } catch (e) {
      // A refusal that names its setting (a /compact whose summary model is over its limit) is said
      // in this conversation as well as in the corner, both with the way to the row; the composer,
      // which catches it next, sees it was said and does not say it a third time.
      raiseFromError(e, id);
      throw e;
    }
    const short = r.text.length < 140 && !r.text.includes("\n");
    if (short) toast(r.text);
    else setCommandResult({ line, text: r.text });
    load();
  }

  // The message goes out; while a run is on the host holds it until the turn ends, and the queue is
  // re-read so its card — the one that offers to steer instead — is there before the stream says so.
  async function send(text: string, files: File[], followUp: boolean, clientMessageId?: string, onProgress?: (fraction: number) => void, secrets?: string[]): Promise<"sent" | "queued"> {
    const reply = orchestrating ? currentReply(id) : null;
    let result: { receipt?: { status: string } };
    const pendingId = clientMessageId ?? crypto.randomUUID();
    const pendingText = text || [...files.map((file) => file.name), ...(secrets ?? []).map((name) => `🔒 ${name}`)].join(", ");
    // Submission can wait behind a busy run for several seconds. Show the operator's words
    // immediately, then let the durable transcript replace this temporary bubble.
    setSendingMessages((current) => [...current, { id: pendingId, text: pendingText }]);
    setSuggestion("");
    stick.current = true;
    try {
      if (files.length > 0) {
        const form = new FormData();
        form.append("text", uploadText(text, reply));
        form.append("client_message_id", pendingId);
        if (followUp) form.append("follow_up", "true");
        form.append("expected_running", followUp ? "true" : "false");
        for (const f of files) form.append("files", f, f.name);
        if (secrets?.length) form.append("secrets", secrets.join(","));
        result = await api.upload<{ receipt?: { status: string } }>(`/api/sessions/${id}/upload`, form, onProgress);
      } else {
        result = await api.post<{ receipt?: { status: string } }>(`/api/sessions/${id}/messages`, messageBody(text, { followUp, clientMessageId: pendingId, reply, secrets }));
      }
      if (result.receipt?.status !== "queued" && result.receipt?.status !== "consumed") throw new Error(t("composer.delivery.unconfirmed"));
    } catch (error) {
      setSendingMessages((current) => current.filter((message) => message.id !== pendingId));
      throw error;
    }
    // Only once the message is out: a send that fails puts the words back and keeps what they answer.
    if (reply) setReply(id, null);
    stick.current = true;
    if (followUp) void loadQueued();
    load();
    return result.receipt!.status === "queued" ? "queued" : "sent";
  }

  async function answer(answers: Answer[]) {
    try {
      await api.post(`/api/sessions/${id}/answer`, { answers });
    } finally {
      // Refused too: a question someone else answered is then drawn as answered rather than left
      // offering options whose send can only be refused again.
      load();
    }
  }

  async function stop() {
    if (!(await confirmDialog({ title: t("session.stop.title"), body: t(focus ? "session.stop.projectBody" : "session.stop.body"), action: t("session.stop.action"), danger: true, cancel: phone ? t("session.stop.keep") : undefined }))) return;
    try {
      await api.post(`/api/sessions/${id}/stop`);
      haptic("medium");
      toast(t("session.stopping"));
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function rename(title: string) {
    setEditingTitle(null);
    if (!title.trim() || title.trim() === detail?.title) return;
    try {
      await api.patch(`/api/sessions/${id}`, { title: title.trim() });
      load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function compact() {
    if (busy) {
      toast(t("session.stopfirst"));
      return;
    }
    if (!(await confirmAsync(t("session.compact.title"), { body: t("session.compact.body"), action: t("session.compact.action"), danger: false }))) return;
    toast(t("session.compacting"));
    try {
      await api.post(`/api/sessions/${id}/compact`, { instructions: "" });
      await load();
      toast(t("session.compacted"));
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function clearHistory() {
    if (busy) {
      toast(t("session.stopfirst"));
      return;
    }
    if (!(await confirmAsync(t("session.clear.title"), { body: t("session.clear.body"), action: t("session.clear.action") }))) return;
    try {
      const r = await api.post<{ dropped: number }>(`/api/sessions/${id}/clear`);
      toast(plural("session.cleared", r.dropped));
      await load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  const openPicker = () => composer.current?.openModel();

  async function chooseModel(body: ModelChoice) {
    try {
      const r = await api.post<{ model: string }>(`/api/sessions/${id}/model`, body);
      toast(t("session.model.picked", { model: r.model }));
      load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function chooseEffort(effort: string) {
    try {
      // A fixed effort is a choice the auto mode would overrule on the next message, so it leaves it.
      if (detail?.mode === AUTO_MODE) await api.post(`/api/sessions/${id}/mode`, { mode:null });
      const r = await api.post<{ reasoning_effort?: string }>(`/api/sessions/${id}/model`, effort === "off" ? { thinking: false } : { thinking: true, reasoning_effort: effort });
      toast(t("session.effort.picked", { effort: t(`add.effort.${effort === "off" ? "off" : r.reasoning_effort || effort}`) }));
      load();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function remove() {
    const ends = endsTerminals(sessionTerminals.running.length);
    if (!(await confirmAsync(t("session.delete.title"), { body: ends ? `${t("session.delete.body")} ${ends}` : t("session.delete.body"), action: t("session.delete.action") }))) return;
    try {
      await api.delete(`/api/sessions/${id}`);
      onBack();
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function detachTelegram() {
    if (!(await confirmAsync(t("session.telegram.detach.title"), { body: t("session.telegram.detach.body"), action: t("session.telegram.detach.action") }))) return;
    try {
      await api.delete(`/api/sessions/${id}/telegram`);
      setDetail((current) => current ? { ...current, telegram_linked: false } : current);
      toast(t("session.telegram.detach.done"));
    } catch (e) {
      toast(errorText(e));
    }
  }

  async function exportMarkdown() {
    try {
      const res = await fetch(`/api/sessions/${id}/export/download`, { headers: api.authHeaders() });
      if (!res.ok) throw new Error(String(res.status));
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `session-${id}.md`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      toast(t("session.export.failed", { error: String(e) }));
    }
  }

  // A workspace file opens in the panel's Preview tab; a file waiting in the composer, which is
  // nowhere the panel can fetch it from, still opens in the dialog.
  const openPreview = useCallback(
    (src: PreviewSource) => {
      if ("file" in src) setPreview(src);
      else panel.openFile(src);
    },
    [panel.openFile],
  );

  // What the conversation opens: a picture or a clip in the centred viewer, paging through the
  // conversation's other media, and everything else where openPreview sends it. The turns are read
  // through a ref so the context, and with it every drawn turn, does not change with each new step.
  const turnsNow = useRef<Turn[]>([]);
  turnsNow.current = turns;
  const placesNow = useRef({ workspace: "", folders: NO_FOLDERS });
  placesNow.current = { workspace: detail?.workspace ?? "", folders: paneFolders };
  const openFromChat = useCallback(
    (src: PreviewSource) => {
      const media = viewerSource(src);
      if (!media) return openPreview(src);
      const { workspace, folders } = placesNow.current;
      setGallery(galleryFor(conversationMedia(turnsNow.current, id, workspace, folders), media));
    },
    [id, openPreview],
  );

  // Ctrl/⌘ . toggles the panel, with Shift it covers the chat. The pane the URL names owns the keys.
  useEffect(() => {
    if (phone || pane === "right") return;
    const onKey = (e: KeyboardEvent) => {
      // Ctrl+. belongs to the program in a focused terminal.
      if (insideTerminal(e.target)) return;
      const which = panelShortcut(e);
      if (!which) return;
      e.preventDefault();
      if (which === "toggle") panel.toggle();
      else panel.expand();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [phone, pane, panel.toggle, panel.expand]);

  const terminalDock = useTerminalDock(id, sessionTerminals, { preferredEnv: detail?.project?.settings?.default_env, phone, toast });
  const openTerminalFile = useCallback((path: string, line?: number) => openPreview({ base: sessionBase(id), path, lines: line ? String(line) : undefined }), [id, openPreview]);
  const toggleDock = terminalDock.toggle;

  // Ctrl+` opens and closes the terminal dock, from anywhere in the session — the terminal included,
  // where it is the one key the program does not get. Ctrl+J (⌘J on a Mac) does the same outside a
  // terminal, as in VS Code. Matched by the physical key, so both work in a Russian layout too.
  useEffect(() => {
    if (phone || pane === "right") return;
    const mac = isMac();
    const onKey = (e: KeyboardEvent) => {
      if (!dockShortcut(e, mac)) return;
      if (e.code === "KeyJ" && insideTerminal(e.target)) return;
      e.preventDefault();
      toggleDock();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [phone, pane, toggleDock]);

  // Where this chat lives, for a link to one of its messages. The orchestrator's chat and a project's
  // sessions are opened in orchestration mode; a link to their plain address took a detour through
  // the Agents screen and a redirect before it arrived.
  const link = focus
    ? orchestrating ? projectHome(focus.projectId) : projectSessionPath(focus.projectId, id)
    : main ? pathFor("orchestration") : (detail && orchestrationPathOf(detail, mainSession)) || pathFor("agents", id);
  const sessionCtx = useMemo(
    () => ({
      id,
      workspace: detail?.workspace ?? "",
      folders: paneFolders,
      preview: openFromChat,
      openJobs: () => panel.open("jobs"),
      toast,
      link,
      asking: questionsKey(detail?.pending?.questions ?? null),
      compose: (text: string) => {
        if (!composer.current) return false;
        composer.current.insert(text);
        return true;
      },
    }),
    [id, detail?.workspace, paneFolders, openFromChat, panel.open, toast, link, detail?.pending?.questions],
  );

  // What the answer cited, clicked: a file opens at the lines it named, a Verify receipt opens as a
  // receipt, and any other run id is a background job — its log is a file in the workspace.
  useEffect(() => {
    const workspace = detail?.workspace ?? "";
    const on = (e: Event) => {
      const cited = (e as CustomEvent<EvidenceRequest>).detail;
      if (!cited) return;
      if (cited.kind === "run") {
        if (/^v\d+$/.test(cited.id)) setReceipt(cited.id);
        else openPreview({ base: sessionBase(id), path: `.jobs/${cited.id}.log` });
        return;
      }
      const rel = workspaceRelative(cited.path, workspace);
      if (rel) openFromChat({ base: sessionBase(id), path: rel, lines: cited.lines });
      else toast(t("session.outside", { path: cited.path }));
    };
    document.addEventListener(EVIDENCE_EVENT, on);
    return () => document.removeEventListener(EVIDENCE_EVENT, on);
  }, [id, detail?.workspace, toast, openPreview, openFromChat]);
  // A snapshot from the panel, for this conversation's composer: the picture as an attachment and its
  // address as a line of the draft. The field is not focused on a phone, where the sheet covers it.
  useEffect(() => {
    const on = (e: Event) => {
      const asked = (e as CustomEvent<ComposeRequest>).detail;
      if (!asked || asked.taken || asked.sessionId !== id || !composer.current) return;
      asked.taken = true;
      composer.current.addFiles(asked.files);
      if (asked.text) composer.current.insert(asked.text, !phone);
    };
    window.addEventListener(COMPOSE_EVENT, on);
    return () => window.removeEventListener(COMPOSE_EVENT, on);
  }, [id, phone]);
  const staffId = focus && detail?.staff ? detail.staff.id : null;
  const hasDetails = panelTabs.includes("details");
  const focusPlaceholder = placeholder ?? (orchestrating ? t("focus.composer") : staffName ? t("focus.composer.staff", { name: staffName }) : undefined);

  // What Retry regenerates after a failed run: the assistant message the run left after the last
  // operator turn, if it left one; a run that failed before answering has nothing to regenerate.
  const retrySeq = (() => {
    for (const message of [...(detail?.messages ?? [])].reverse()) {
      if (message.role === "user") return null;
      if (message.role === "assistant" && typeof message.seq === "number") return message.seq;
    }
    return null;
  })();
  const lastSeq = detail?.messages.reduce<number | null>((last, message) => (typeof message.seq === "number" ? message.seq : last), null) ?? null;
  // The phone's bar and its ⋮ sheet hold the same commands as the desktop header above; a command
  // added there belongs in one of these two lists too, or a phone loses it.
  // The workspace in the operator's own file manager, where there is one in front of them.
  const revealer = useReveal();
  const revealWorkspace = () => void revealer?.reveal({ session_id: id });
  const phoneTiles: SessionTile[] = phone && !pane ? [
    { id: "search", icon: "search", label: t("session.phone.search"), onSelect: () => setPhoneSheet("search") },
    { id: "terminal", icon: "terminal", label: t("session.phone.terminal"), count: sessionTerminals.running.length, disabled: !!terminalDock.reason && sessionTerminals.running.length === 0, onSelect: () => terminalDock.setSheet(true) },
    ...(hasBrowser ? [{ id: "browser", icon: "globe" as IconName, label: t("panel.tab.browser"), attn: !!browserShown?.needs_you, onSelect: openBrowser }] : []),
    ...(questionsShown ? [{ id: "questions", icon: "ask" as IconName, label: t("panel.tab.questions"), count: waiting, attn: waiting > 0, onSelect: () => panel.open("questions") }] : []),
    ...(panelTabs.includes("files") && hasDetails ? [{ id: "files", icon: "folder" as IconName, label: t("panel.tab.files"), onSelect: () => panel.open("files") }] : []),
    ...(hasDetails ? [{ id: "details", icon: "settings" as IconName, label: t("panel.tab.details"), onSelect: () => { setDetailsFocus("session"); panel.open("details"); } }] : []),
    ...(panelTabs.includes("preview") && panel.state.stack.length > 0 ? [{ id: "preview", icon: "eye" as IconName, label: t("panel.tab.preview"), onSelect: () => panel.open("preview") }] : []),
    ...(panelTabs.includes("jobs") && hasDetails ? [{ id: "jobs", icon: "clock" as IconName, label: t("panel.tab.jobs"), onSelect: () => panel.open("jobs") }] : []),
    ...(panelTabs.includes("subagents") ? [{ id: "subagents", icon: "spawn" as IconName, label: t("panel.tab.subagents"), count: detail?.subagents?.length || undefined, onSelect: () => panel.openSubagent(null) }] : []),
    ...(hasDetails ? [{ id: "mcp", icon: "plug" as IconName, label: t("session.mcp"), onSelect: () => { setDetailsFocus("mcp"); panel.open("details"); } }] : []),
    ...(sessionDiagrams?.length ? [{ id: "diagrams", icon: "pen" as IconName, label: t("session.phone.diagrams"), count: sessionDiagrams.length, onSelect: () => navigate(pathFor("diagrams", null, { session: id })) }] : []),
  ] : [];
  const contextPct = detail?.context?.window ? Math.round((100 * detail.context.tokens) / detail.context.window) : null;
  // A chat that is still alone in a project of its own can become a project from its own menu too.
  const makeProject = makeProjectCommand(detail?.project, () => setPromoting(true));
  const phoneRows: SessionRow[] = phone && !pane ? [
    { icon: "pen", label: t("session.rename"), onSelect: () => setPhoneSheet("rename") },
    ...(focus ? [] : [{ icon: "folder" as IconName, label: t("session.project.move"), value: detail?.project?.name, onSelect: () => setMoving(true) }]),
    ...(!focus && makeProject ? [{ icon: makeProject.icon!, label: makeProject.label, onSelect: makeProject.onSelect }] : []),
    // A helper's own chat leads back to its leader; a leader's helpers are a tile of the panel above.
    ...(detail?.subagent_of ? [{ icon: "back" as IconName, label: t("session.leader.word"), value: detail.leader_title ?? undefined, onSelect: () => onOpen?.(detail.subagent_of!) }] : []),
    ...(lastSeq !== null && !main && !orchestrating ? [{ icon: "fork" as IconName, label: t("session.phone.fork"), onSelect: () => void turnAction("fork", lastSeq) }] : []),
    ...(onSplit ? [{ icon: "split" as IconName, label: t("session.split"), onSelect: onSplit }] : []),
    { icon: "share", label: t("session.share"), value: detail?.share && detail.share.mode !== "local" ? t(detail.share.mode === "public" ? "session.share.chip.public" : "session.share.chip.key") : undefined, onSelect: () => setSharing(true) },
    { icon: "download", label: t("session.export"), onSelect: exportMarkdown },
    ...(revealer && hasDetails ? [{ icon: "external" as IconName, label: revealer.label, onSelect: revealWorkspace }] : []),
    { icon: "compact", label: t("session.compact"), value: contextPct !== null ? `${contextPct} %` : undefined, disabled: busy, onSelect: compact },
    ...(detail?.telegram_linked ? [{ icon: "unlink" as IconName, label: t("session.telegram.detach"), onSelect: detachTelegram }] : []),
    { icon: "trash", label: t("session.clear"), danger: true, disabled: busy, onSelect: clearHistory },
    { icon: "trash", label: t("session.delete"), danger: true, onSelect: remove },
  ] : [];
  // An idle session with jobs still running has not finished: say what it waits for rather than look done.
  const jobsPending = !busy && !saving && !compacting && status !== "failed" ? detail?.background_count ?? 0 : 0;
  const effortText = detail ? effortWord(detail.reasoning_effort, detail.thinking !== false, detail.mode === AUTO_MODE) : "";
  const phoneSub = offline ? <StateSub tone="offline">{t("session.phone.reconnecting")}</StateSub>
    : compacting ? <StateSub tone="compacting">{t("session.compacting.bar")}</StateSub>
    : busy ? <LiveSub status={status} base={tail} live={live} />
    : saving ? <StateSub tone="saving">{t("session.livebar.saving")}</StateSub>
    : status === "failed" ? <StateSub tone="failed">{statusWord("failed")}</StateSub>
    : jobsPending > 0 ? <StateSub tone="running">{plural("session.jobs.waiting", jobsPending)}</StateSub>
    : ahead ? <span>{t("session.compacting.ahead", { what: compactionStage(ahead) })}</span>
    : detail ? <span className={detail.fallback ? "ph-chat-fallback" : undefined}>{shortModel(detail.fallback?.to ?? detail.model, 28)}{effortText ? ` · ${effortText}` : ""}</span>
    : null;
  const phoneChip = phone && browserShown
    ? <BrowserHeadButton groups={browsers.groups} onOpen={openBrowser} streaming={panel.state.tab !== "browser"} saving={deviceSaving(true)} />
    : phone && questionScope && waiting > 0
      ? <IconButton icon="ask" label={plural("questions.line", waiting)} badge={waiting} className="questions-headbtn attn" onClick={() => panel.open("questions")} />
      : null;
  return (
    <FocusChatContext.Provider value={focusChat}>
    <ComposeTarget.Provider value={id}>
    <div className={`chat ${pane ? `pane pane-${pane}` : ""} ${focus ? `in-project ${focus.kind}` : ""}`} onDragEnter={(e) => { if (e.dataTransfer?.types.includes("Files")) setDragging((d) => d + 1); }} onDragLeave={() => setDragging((d) => Math.max(0, d - 1))} onDragOver={(e) => e.preventDefault()} onDrop={onDrop}>
      {dragging > 0 && <div className="dropzone"><Icon name="attach" size={28} /> {t("session.drop")}</div>}
      {bare ? null : phone && !pane ? <>
        <SessionTopBar
          title={detail?.title ?? "…"}
          sub={phoneSub}
          back={main || focus?.kind === "member" ? onBack : undefined}
          onTitle={hasDetails ? () => { setDetailsFocus("session"); panel.open("details"); } : undefined}
          chip={phoneChip}
          onMenu={() => setPhoneSheet("menu")}
          shared={detail?.share && detail.share.mode !== "local" ? (detail.share.mode === "public" ? "public" : "key") : null}
        />
        {offline && <Banner strip tone="warn" icon="reload" className="ph-chat-strip">{t("session.phone.reconnecting")}</Banner>}
      </> : <div className={`chat-head ${busy || saving || compacting ? "live" : ""}`}>
        {(pane || phone) && (
          <button className="iconbtn" onClick={onBack} aria-label={t(pane === "right" ? "session.closepane" : "shell.back")} title={t(pane === "right" ? "session.closepane" : "shell.back")}>
            <Icon name={pane === "right" ? "close" : "back"} />
          </button>
        )}
        <div className="grow chat-identity" style={{ minWidth: 0 }}>
          {editingTitle !== null ? (
            <input
              className="title-edit"
              autoFocus
              value={editingTitle}
              onChange={(e) => setEditingTitle(e.target.value)}
              onBlur={() => rename(editingTitle)}
              onKeyDown={(e) => {
                if (e.key === "Enter") rename(editingTitle);
                if (e.key === "Escape") setEditingTitle(null);
              }}
            />
          ) : (
            <span className="chat-title-line">
              <span className="chat-title truncate">{detail?.title ?? "…"}</span>
              <HostMark env={detail?.env} />
            </span>
          )}
          {detail?.subagent_of ? (
            <button className="leader-link" onClick={() => onOpen?.(detail.subagent_of!)} title={t("session.leader")}>
              ↳ {detail.leader_title ?? t("session.leader.word")}
            </button>
          ) : detail && (
            // The second line says what the phone's title button says: the project, the model and its
            // thinking, and the live state, which the line carries itself rather than a bar beside it.
            <span className="chat-sub">
              {isProject(detail.project) && detail.project.name !== detail.title && <><span className="truncate chat-sub-project">{detail.project.name}</span><span className="sep">·</span></>}
              <span className="truncate chat-sub-model">{shortModel(detail.fallback?.to ?? detail.model, 28)}{effortText && <span className="chat-sub-effort"> {effortText}</span>}</span>
              {detail.imported && <><span className="sep">·</span><span className="truncate chat-sub-from">{t("imp.head.from", { name: harnessMeta(detail.imported.harness, detail.imported.harness_name).name })}</span></>}
              {compacting ? null : busy || saving ? <><span className="sep">·</span><LiveBar status={status} saving={saving} base={tail} live={live} workspace={detail.workspace} onJump={jumpToBottom} /></>
                : status === "failed" ? <><span className="sep">·</span><span className="head-status failed" role="status"><b>{statusWord(status)}</b></span></>
                : jobsPending > 0 ? <><span className="sep">·</span><button type="button" className="head-status jobs-pending" onClick={() => panel.open("jobs")}>{plural("session.jobs.waiting", jobsPending)}</button></> : null}
              {ahead && <><span className="sep">·</span><AheadStatus c={ahead} /></>}
            </span>
          )}
        </div>
        {compacting ? (
          <CompactionBar c={compacting} />
        ) : !detail || detail.subagent_of ? (
          busy || saving ? (
            <LiveBar status={status} saving={saving} base={tail} live={live} workspace={detail?.workspace} onJump={jumpToBottom} />
          ) : status === "failed" ? (
            <span className="head-status failed" role="status"><Dot status={status} /><b>{statusWord(status)}</b></span>
          ) : null
        ) : null}
        {offline && <span className="head-status offline">{t("session.reconnecting")}</span>}
        <div className="head-actions">
          {detail?.share && detail.share.mode !== "local" && (
            <button type="button" className={`chip ${detail.share.mode === "public" ? "bad" : "attn"}`} onClick={() => setSharing(true)}>
              {t(detail.share.mode === "public" ? "session.share.chip.public" : "session.share.chip.key")}
            </button>
          )}
          {!phone && detail && (detail.subagents?.length ?? 0) > 0 && (
            <SubagentsButton detail={detail} on={panel.state.tab === "subagents"} onToggle={() => (panel.state.tab === "subagents" ? panel.close() : panel.openSubagent(null))} />
          )}
          {phone && questionsShown && (
            // A phone has no panel column: the questions open as a sheet from here, with how many wait.
            <button className={`iconbtn questions-headbtn ${waiting ? "attn" : ""}`} onClick={() => panel.open("questions")} aria-label={plural("questions.line", waiting)} title={t("panel.tab.questions")}>
              <Icon name="ask" />
              {waiting > 0 && <span className="count">{waiting}</span>}
            </button>
          )}
          {phone && browserShown && <BrowserHeadButton groups={browsers.groups} onOpen={openBrowser} streaming={panel.state.tab !== "browser"} saving={deviceSaving(true)} />}
          <ChatSearch sessionId={id} onPick={openMessage} />
          {!!sessionDiagrams?.length && <a className="btn small" href={pathFor("diagrams", null, { session: id })} aria-label={`${t("diagrams.session")} (${sessionDiagrams.length})`} onClick={(event) => { event.preventDefault(); navigate(pathFor("diagrams", null, { session: id })); }}><Icon name="pen" size={16} /> {phone ? sessionDiagrams.length : `${t("diagrams.session")} (${sessionDiagrams.length})`}</a>}
          <TerminalButton dock={terminalDock} phone={phone} />
          {!phone && <button className={`iconbtn ${panel.state.tab ? "on" : ""}`} onClick={panel.toggle} aria-label={t("panel.toggle")} title={t("panel.toggle.title")} aria-pressed={!!panel.state.tab}>
            <Icon name="panel" />
          </button>}
          <OverflowMenu
            label={t("session.actions")}
            items={[
              { label: t("session.rename"), icon: "pen", onSelect: () => setEditingTitle(detail?.title ?? "") },
              // A session in a project's focus mode stays in its project; the orchestrator's chat has no
              // files or jobs of its own to show.
              ...(focus ? [] : [{ label: t("session.project.move"), icon: "folder" as IconName, onSelect: () => setMoving(true) }]),
              ...(!focus && makeProject ? [makeProject] : []),
              "-",
              ...(hasDetails
                ? [
                    { label: t("panel.tab.details"), icon: "settings" as IconName, onSelect: () => { setDetailsFocus("session"); panel.open("details"); } },
                    ...(panelTabs.includes("files") ? [{ label: t("session.files"), icon: "folder" as IconName, onSelect: () => panel.open("files") }] : []),
                    ...(revealer ? [{ label: revealer.label, icon: "external" as IconName, onSelect: revealWorkspace }] : []),
                    ...(panelTabs.includes("jobs") ? [{ label: t("panel.tab.jobs"), icon: "terminal" as IconName, onSelect: () => panel.open("jobs") }] : []),
                    { label: t("session.mcp"), icon: "plug" as IconName, onSelect: () => { setDetailsFocus("mcp"); panel.open("details"); } },
                  ]
                : []),
              ...(onSplit ? [{ label: t("session.split"), icon: "split" as IconName, onSelect: onSplit }] : []),
              "-",
              { label: t("session.share"), icon: "share", onSelect: () => setSharing(true) },
              { label: t("session.export"), icon: "download", onSelect: exportMarkdown },
              { label: t("session.compact"), icon: "compact", onSelect: compact, disabled: busy },
              ...(detail?.telegram_linked ? [{ label: t("session.telegram.detach"), icon: "unlink" as IconName, onSelect: detachTelegram }] : []),
              "-",
              { label: t("session.clear"), icon: "trash", onSelect: clearHistory, disabled: busy, danger: true },
              { label: t("session.delete"), icon: "trash", onSelect: remove, danger: true },
            ]}
          />
        </div>
        {(busy || saving || compacting) && <HeadProgress status={status} compacting={compacting} />}
      </div>}
      {staffId && <StaffHeader projectId={focus!.projectId} staffId={staffId} toast={toast} />}
      {banner}

      <div ref={body} className={`chat-body ${panel.state.tab && !phone ? "with-panel" : ""} ${panel.state.expanded && !phone ? "panel-full" : ""}`} style={{ ["--panel-w" as string]: `${panelPct}%` }}>
        <div className="chat-main">
          {!phone && panel.state.tab !== "browser" && !(panel.state.expanded && panel.state.tab) && <BrowserPip groups={browsers.groups} onOpen={openBrowser} />}
          <div className="chat-scroll" ref={scroller} onScroll={onScroll}>
            <div className="timeline">
              {!detail && <TurnSkeleton />}
              {/* An imported chat begins with where it came from, above whatever older pages load. */}
              {detail?.imported && <ImportBanner sessionId={id} origin={detail.imported} toast={toast} onPulled={() => void load(true)} />}
              {pageable && <div className="sub older-note">{older === "loading" ? t("session.older") : ""}</div>}
              {snapshots?.pruned && (
                <div className="sub older-note">
                  {snapshots.pruned_before ? t("session.pruned.before", { date: absDate(snapshots.pruned_before) }) : t("session.pruned")}
                </div>
              )}
              {unreached && (
                <div className="anchor-note" role="status">
                  <span className="grow">{t(unreached === "far" ? "anchor.far" : "anchor.gone")}</span>
                  <button type="button" className="iconbtn" onClick={() => setUnreached(null)} aria-label={t("common.close")} title={t("common.close")}>
                    <Icon name="close" size={16} />
                  </button>
                </div>
              )}
              <SessionContext.Provider value={sessionCtx}>
                <SubagentOpener.Provider value={openSubagentRef}>
                <Windowed
                  keys={turnKeys}
                  scroller={scroller}
                  pinned={() => stick.current}
                  dragging={() => userScrolling.current}
                  onTop={onTopOfList}
                  handle={windowHandle}
                  render={(i) => (
                    <Safe>
                      {i > 0 && !settled[i].imported && !!settled[i - 1].imported && <HandoffLine at={settled[i].startedAt} />}
                      <TurnView turn={settled[i]} live={false} onTurnAction={turnAction} />
                    </Safe>
                  )}
                />
                {busy && <LiveTurn base={tail} live={live} onTurnAction={turnAction} onRender={pinBottom} />}
                {/* A message the host is holding is drawn once, as its card above the composer, where
                    its Steer and × are; the bubble here would be the same words a second time. */}
                {sendingMessages.filter((pending) => !sentMessageReachedTranscript(detail?.messages, pending.id) && !queuedInput.some((queued) => queued.id === pending.id)).map((pending) => <div className="turn" key={pending.id}><div className="turn-content"><div className="msg-wrap"><div className="msg user"><Md text={pending.text} /></div></div></div></div>)}
                </SubagentOpener.Provider>
              </SessionContext.Provider>
              {flow}
              {/* The agent waiting in its browser is said in the conversation too, with Open, so the
                  request is not only a chip in the bar. */}
              {phone && !pane && panel.state.tab !== "browser" && !browsers.groups.some(loginAsk) && <BrowserNeedsRow groups={browsers.groups} onOpen={openBrowser} />}
              {/* The agent's request to be signed in is answered here, in the conversation that made
                  it; the Browser tab draws the same card, so it is left out while that tab is open. */}
              {panel.state.tab !== "browser" && browsers.groups.some(loginAsk) && (
                <div className="login-stack">{browsers.groups.filter(loginAsk).map((g) => <BrowserLoginCard key={g.id} group={g} toast={toast} />)}</div>
              )}
              {/* Not while the Questions tab is the one open: the cards are already beside the chat,
                  and an "Open" that opens what is on screen had people clicking and waiting for
                  something new to appear. */}
              {questionScope && panel.state.tab !== "questions" && <QuestionsLine count={waiting} onOpen={() => panel.open("questions")} />}
            </div>
          </div>
          {!atBottom && (
            <div className="composer-jump-anchor">
              <button className="jump-down" onClick={jumpToBottom} aria-label={t("session.jump.label")} title={t("session.jump")}>
                <Icon name="down" size={18} />
              </button>
            </div>
          )}
          {phone && !pane && compacting && <div className="ph-compact-row"><CompactionBar c={compacting} /></div>}
          {phone && !pane && !busy && !!detail?.error && (
            // The phone's failed run: the cause, what ran and when, and Retry on the answer the run
            // left — the line above the composer said what went wrong and offered nothing to do.
            <div className="ph-runerror">
              <Banner tone="bad" icon="alert" sub={[shortModel(detail.model, 28), detail.messages.at(-1)?.created_at ? clock(detail.messages.at(-1)!.created_at) : ""].filter(Boolean).join(" · ")}
                action={retrySeq !== null ? <button type="button" className="ph-btn sm primary" onClick={() => void turnAction("retry", retrySeq)}><Icon name="reload" size={16} />{t("turn.retry")}</button> : undefined}>
                {t("session.runerror")}: {detail.error}
              </Banner>
            </div>
          )}
          {!(phone && !pane) && !busy && !!detail?.error && (
            <div className="runerror" role="status" aria-label={t("session.runerror")}>
              <Icon name="question" size={14} /><span><b>{t("session.runerror")}: </b>{detail.error}</span>
            </div>
          )}
          <SettingNoticeLines sessionId={id} />
          {staffId && <StaffMessages staffId={staffId} />}
          {/* A draft written before the first session read would bind to an empty model and workspace,
              then ask for target approval when those initial values arrived. */}
          {detail?.id === id && <Composer
            ref={composer}
            above={orchestrating ? <>{showGoalLine && <GoalStrip projectId={focus!.projectId} />}<ReplyChip sessionId={id} /></> : undefined}
            sessionId={id}
            status={status}
            onSend={send}
            onStop={stop}
            commands={commands}
            skills={skills}
            onCommand={runCommand}
            model={detail?.model ?? ""}
            fallback={detail?.fallback ?? null}
            onChooseModel={chooseModel}
            thinking={detail?.thinking}
            reasoningEffort={detail?.reasoning_effort} effortAuto={detail?.mode === AUTO_MODE}
            onChooseEffort={chooseEffort}
            suggestion={suggestion}
            onSuggestionUsed={() => setSuggestion("")}
            mode={detail?.mode ?? ""}
            modes={modes}
            yagni={!!detail?.yagni}
            onChooseMode={bare ? undefined : setMode}
            onYagni={bare ? undefined : setYagni}
            autopilot={detail?.autopilot}
            onAutopilot={bare || embedded || main || orchestrating ? undefined : setAutopilot}
            onCoach={bare || embedded || main || orchestrating || detail?.prompt_coach_of || detail?.btw_of ? undefined : (draft) => setCoach((open) => ({ draft: open?.draft ?? draft, raise: (open?.raise ?? 0) + 1 }))}
            onBtw={bare || embedded || main || orchestrating || detail?.prompt_coach_of || detail?.btw_of ? undefined : (question) => setBtw((open) => ({ question, ask: (open?.ask ?? 0) + 1 }))}
            place={{ project: detail?.project?.name, workspace: detail?.workspace_name || detail?.workspace, system: !!(detail?.project?.system || detail?.project?.settings.system) }}
            targetProject={detail?.project?.id}
            targetWorkspace={detail?.workspace}
            idlePlaceholder={focusPlaceholder}
            context={detail?.context ?? null}
            onContext={hasDetails ? () => { setDetailsFocus("context"); panel.open("details"); } : undefined}
            asr={asr}
            queued={queuedInput}
            onWithdraw={withdraw}
            onSteer={steerNow}
            questions={detail?.pending?.questions ?? null}
            answered={detail?.pending?.answered ?? null}
            onAnswer={answer}
            approval={approval}
            onApprove={approve}
            similar={similar && similar.key === approval?.key ? similar.label : null}
            onApproveSimilar={approveSimilar}
            onDeny={deny}
            onPreviewFile={(file) => setPreview({ file })}
            phone={phone}
            toast={toast}
          />}
          {!phone && !bare && <TerminalDock dock={terminalDock} workspace={detail?.workspace} fileOpener={openTerminalFile} />}
        </div>
        {detail && (
          <Panel
            state={panel.state}
            onTab={panel.open}
            onClose={panel.close}
            onExpand={panel.expand}
            onBack={panel.back}
            onForward={panel.forward}
            root={detail.project?.name ?? t("session.files.crumb")}
            downloadUrl={(e) => downloadHref(e.base, e.path)}
            sheet={phone}
            sheetTitle={detail.title}
            sheetSub={[detail.project?.name, detail.context?.messages ? plural("session.phone.messages", detail.context.messages) : null].filter(Boolean).join(" · ")}
            drag={dragPanel}
            tabs={panelTabs}
            pages={{
              ...(focus ? {
                board: <ProjectBoard projectId={focus.projectId} toast={toast} embedded />,
                brief: <BriefPage projectId={focus.projectId} compact toast={toast} />,
                wakeups: <WakeupsPage projectId={focus.projectId} compact toast={toast} />,
                folders: <FoldersPage projectId={focus.projectId} compact toast={toast} />,
              } : {}),
              ...(questionScope ? { questions: <QuestionsPanel scope={questionScope} toast={toast} /> } : {}),
              browser: <BrowserTab groups={browsers.groups} toast={toast} phone={phone} sessionId={id ?? undefined} user={browsers.user} />,
              subagents: <SubagentsTab
                leaderId={id}
                selected={panel.state.sub}
                onSelect={panel.openSubagent}
                onOpenFull={(child) => navigate(sessionPath(child))}
                toast={toast}
                chat={(child) => <SubagentTranscript key={child} id={child} onPreview={openPreview} toast={toast} />}
              />,
            }}
            badges={{ ...(questionsShown && waiting ? { questions: waiting } : {}), ...(runningSubagents ? { subagents: runningSubagents } : {}) }}
            marks={browserShown ? { browser: browserShown.needs_you ? "attn" : browserShown.acting ? "busy" : undefined } : undefined}
            details={hasDetails ? (ids) =>
              <SessionDetails
                ids={ids}
                id={id}
                role={orchestrating ? "orchestrator" : main ? "main" : "session"}
                detail={detail}
                busy={busy}
                modes={modes.map((m) => m.name)}
                schedules={schedules}
                provider={provider}
                providerUsage={providerUsage}
                onOpen={onOpen}
                toast={toast}
                reload={load}
                focus={detailsFocus}
                on={{
                  rename,
                  setMode,
                  openPicker,
                  compact,
                  clearHistory,
                  remove,
                  exportMarkdown,
                  loopAction,
                  scheduleAction,
                  move: () => setMoving(true),
                  showLog: (line, text) => setCommandResult({ line, text }),
                  openFiles: () => panel.open("files"),
                }}
              />
            : undefined}
            files={hasDetails && panelTabs.includes("files") ? <Explorer key={id} base={sessionBase(id)} root={detail?.project?.name} upload={!orchestrating} folders={paneFolders} home={detail?.folder_id} onPreview={openPreview} toast={toast} refresh={filesGeneration} written={producedFiles(turns.at(-1)?.activity ?? []).filter((f) => f.how === "wrote").map((f) => { const place = placeOf(f.path, detail.workspace); return place && !place.folder ? place.rel : ""; })} /> : undefined}
            jobs={hasDetails ? <JobsTab sessionId={id} messages={detail.messages} onOpen={panel.openFile} onPreview={openPreview} /> : undefined}
          />
        )}
      </div>

      <TerminalFull dock={terminalDock} phone={phone} workspace={detail?.workspace} fileOpener={openTerminalFile} />
      <TerminalSheet dock={terminalDock} />

      {commandResult && (
        <Overlay><div className="sheet-backdrop" onClick={() => setCommandResult(null)}>
          <div className="sheet" onClick={(e) => e.stopPropagation()}>
            <div className="grip" />
            <h3 className="mono">{commandResult.line}</h3>
            <div className="sheet-body">
              <pre className="diff" style={{ whiteSpace: "pre-wrap" }}>{commandResult.text}</pre>
            </div>
          </div>
        </div></Overlay>
      )}

      {promoting && detail && <MakeProjectSheet project={detail.project} title={detail.title} onClose={() => setPromoting(false)} onMade={() => load(true)} />}
      {moving && detail && <MoveSessionSheet sessionId={id} current={detail.project.id} currentOwn={!!detail.workspace_own} onClose={() => setMoving(false)} onMoved={() => load(true)} toast={toast} />}

      {sharing && <ShareSheet sessionId={id} share={detail?.share} onClose={() => setSharing(false)} onChanged={(share) => setDetail((current) => (current ? { ...current, share } : current))} toast={toast} />}

      {preview && <FilePreview src={preview} onClose={() => setPreview(null)} />}

      {gallery && <FileGallery sources={gallery.items} start={gallery.index} onClose={() => setGallery(null)} onPanel={openPreview} />}

      {receipt && <ReceiptDialog sessionId={id} receipt={receipt} onClose={() => setReceipt(null)} />}

      {phoneSheet === "menu" && <SessionMenuSheet tiles={phoneTiles} rows={phoneRows} onClose={() => setPhoneSheet(null)} />}
      {phoneSheet === "search" && <SearchSheet sessionId={id} onPick={openMessage} onClose={() => setPhoneSheet(null)} />}
      {phoneSheet === "rename" && <RenameSheet title={detail?.title ?? ""} onSave={(title) => { setPhoneSheet(null); void rename(title); }} onClose={() => setPhoneSheet(null)} />}

      {coach && (
        <PromptCoach parentId={id} draft={coach.draft} raise={coach.raise} phone={phone} toast={toast} onClose={() => setCoach(null)}
          onInsert={(prompt) => composer.current?.replace(prompt)}
          onOpenFile={hasDetails && panelTabs.includes("files") ? (path) => openPreview({ base: sessionBase(id), path }) : undefined}
          chat={(coachId) => <SessionScreen key={coachId} id={coachId} onBack={() => setCoach(null)} toast={toast} bare />} />
      )}
      {btw && (
        <SideQuestion parentId={id} question={btw.question} ask={btw.ask} phone={phone} onClose={() => setBtw(null)}
          chat={(btwId) => <SessionScreen key={btwId} id={btwId} onBack={() => setBtw(null)} toast={toast} placeholder={t("btw.followup")} bare />} />
      )}

    </div>
    </ComposeTarget.Provider>
    </FocusChatContext.Provider>
  );
}

/**
 * The header's way to the Subagents tab: how many helpers the session has, and a live dot while one
 * works. It used to open a popover listing them, which was the tab's list a second time with nothing
 * of their conversations; now it opens and closes the tab itself, and the count keeps the helpers in
 * sight while the panel is closed or on another tab. A helper's own chat has no button: the line
 * under its title leads back to the leader.
 */
function SubagentsButton({ detail, on, onToggle }: { detail: SessionDetail; on: boolean; onToggle: () => void }) {
  const children = detail.subagents ?? [];
  const running = children.filter((child) => child.running).length;
  const label = `${t("session.subagents.open")}: ${plural("session.subagents", children.length)}${running ? t("session.subagents.working", { n: running }) : ""}`;
  return (
    <button className={`iconbtn subagents-trigger ${on ? "on" : ""}`} onClick={onToggle} aria-label={label} title={label} aria-pressed={on}>
      <Icon name="spawn" />
      <span className="subagents-count">{children.length}</span>
      {running > 0 && <span className="subagents-live" aria-hidden="true" />}
    </button>
  );
}

class Safe extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? <div className="note">{t("session.norender")}</div> : this.props.children;
  }
}

/** The label's tooltip: what the chosen option was, or the question the operator answered. */
function autopilotTitle(mark: AutopilotMark): string {
  if (mark.kind === "choice" && mark.label) return t("turn.autopilot.choice.title", { label: mark.label });
  if (mark.kind === "answer" && mark.question) return t("turn.autopilot.answer.title", { question: mark.question });
  return t("turn.autopilot.continue.title");
}

function shortModel(name?: string, max = 18): string {
  if (!name) return t("session.model.none");
  const short = name.split("/").pop()!.replace(/^deepseek-/, "").replace(/\s*\(.*\)$/, "");
  return short.length > max ? `${short.slice(0, max - 1)}…` : short;
}

// ── turns ─────────────────────────────────────────────────────────────────────────────────

// The windowed list unmounts a turn as soon as it leaves the overscan band, so what the reader
// opened cannot live in the component: a step expanded and a long result fetched would both be gone
// two screens later, and the result fetched again on the way back. It is held here instead, keyed by
// session and by the row, and read back when the row mounts again — the same reason the markdown
// render cache is module-level. What a session held is dropped when the screen leaves it.
const disclosed = new Map<string, { open?: boolean; full?: FullResult | null }>();

function remember(key: string, patch: { open?: boolean; full?: FullResult | null }): void {
  disclosed.set(key, { ...disclosed.get(key), ...patch });
}

function forgetDisclosed(sessionId: string): void {
  const prefix = `${sessionId}:`;
  for (const key of [...disclosed.keys()]) if (key.startsWith(prefix)) disclosed.delete(key);
}

/** `useState` for a disclosure that has to survive its row leaving the window. */
function useDisclosed(key: string, initial: boolean): [boolean, (next: boolean | ((o: boolean) => boolean)) => void] {
  const [open, set] = useState(() => disclosed.get(key)?.open ?? initial);
  const write = useCallback(
    (next: boolean | ((o: boolean) => boolean)) => {
      set((o) => {
        const now = typeof next === "function" ? next(o) : next;
        remember(key, { open: now });
        return now;
      });
    },
    [key],
  );
  return [open, write];
}

function stepCount(items: Activity[]): number {
  return items.filter((a) => a.kind === "tool").length;
}

/**
 * The one line that says this answer is not the configured model's.
 *
 * It sits above the answer rather than under it, because it changes how the answer is read and the
 * reader has to have it before the text. Folded, it is the two names; opened, the reason the run
 * moved and the way to the calls themselves, which the Usage screen already lists per model.
 */
function FallbackChip({ fallback }: { fallback: ModelFallback }) {
  const [open, setOpen] = useState(false);
  const reason = DICT[`session.model.reason.${fallback.reason}`] ? t(`session.model.reason.${fallback.reason}`) : fallback.reason;
  return (
    <div className="fallback-note">
      <button className="chip attn" onClick={() => setOpen((o) => !o)}>
        <Icon name="model" size={14} />
        <span className="truncate">{t("session.model.fallback.turn", { to: fallback.to, from: fallback.from })}</span>
        <Chevron open={open} size={12} />
      </button>
      {open && (
        <div className="sub">
          {t("session.model.fallback.why", { from: fallback.from, to: fallback.to, reason })}{" "}
          <a href={pathFor("usage")} onClick={(e) => { e.preventDefault(); navigate(pathFor("usage")); }}>
            {t("session.model.fallback.calls")}
          </a>
        </div>
      )}
    </div>
  );
}

/** The orchestrator's folded line: its steps in the project's words, each named once ("Task created,
 *  Assigned, Asked you"), then whatever else it called in the ordinary words. */
function stepLine(items: Activity[]): string {
  const tools = items.filter((a): a is ToolItem => a.kind === "tool");
  const verbs = [...new Set(tools.map((item) => stepDescription(item)?.verb).filter((v): v is string => !!v))];
  const rest = familyLine(tools.filter((item) => !stepDescription(item)));
  return [verbs.join(", "), rest.charAt(0).toLowerCase() + rest.slice(1)].filter(Boolean).join(", ");
}

/** The folded line's summary of the work: "read 2 files, ran 3 commands and 4 more". */
function familyLine(items: Activity[]): string {
  const { named, more } = familyCounts(items);
  if (!named.length) return "";
  const parts = named.map(({ family, n }) => (DICT[`turn.family.${family}`] ? plural(`turn.family.${family}`, n) : t("turn.family.other", { name: family, n })));
  const text = parts.join(", ") + (more > 0 ? ` ${plural("turn.family.more", more)}` : "");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** What a system note is called on its one line. */
function noteTitle(note: SystemNote): string {
  if (note.kind === "loop") {
    const head = note.iteration === undefined ? t("turn.note.loop.plain") : note.total ? t("turn.note.loop.of", { n: note.iteration, total: note.total }) : t("turn.note.loop", { n: note.iteration });
    return [head, note.cadence, t("turn.note.instruction")].filter(Boolean).join(" · ");
  }
  if (note.kind === "other") return t("turn.note.other", { origin: note.origin });
  if (note.kind === "subagent") return t("turn.note.subagent", { name: note.origin });
  if (note.kind === "page") return t("turn.note.page", { slug: note.origin });
  return t(`turn.note.${note.kind}`);
}

/** A loop's wake-up, a schedule's prompt, a reminder: one folded line, never a bubble. */
function SystemNoteRow({ note, cacheKey, run }: { note: SystemNote; cacheKey?: string; run?: { folded: boolean; toggle: () => void } }) {
  // The task a helper was given is what its conversation is about: open, where a wake-up is folded.
  const [open, setOpen] = useState(note.kind === "task");
  const openSubagent = useContext(SubagentOpener);
  const subagentLink = openSubagent && (note.kind === "subagent" || note.kind === "subagents");
  return (
    <div className={`sysnote ${note.kind}`}>
      <div className="sysnote-row">
      <button type="button" className="sysnote-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <Icon name={note.kind === "loop" ? "loop" : note.kind === "schedule" || note.kind === "reminder" ? "clock" : note.kind === "subagent" || note.kind === "subagents" || note.kind === "task" ? "spawn" : note.kind === "answers" ? "ask" : "compact"} size={14} />
        <span className="truncate">{noteTitle(note)}</span>
        <Chevron open={open} />
      </button>
      {subagentLink && <button type="button" className="sysnote-open" onClick={() => openSubagent(note.kind === "subagent" ? { name: note.origin } : null)}>{t(note.kind === "subagent" ? "subagents.open.one" : "subagents.open.all")}</button>}
      {run && <button type="button" className="run-disclosure" onClick={run.toggle} aria-expanded={!run.folded} aria-label={t(run.folded ? "turn.run.show" : "turn.run.hide")} title={t(run.folded ? "turn.run.show" : "turn.run.hide")}>
        <span>{t(run.folded ? "turn.run.show" : "turn.run.hide")}</span><Chevron open={!run.folded} />
      </button>}
      </div>
      {open && <Md className="sysnote-body" text={note.body} cacheKey={cacheKey} />}
    </div>
  );
}

const TurnView = memo(function TurnView({ turn, live, onTurnAction }: { turn: Turn; live: boolean; onTurnAction?: (kind: "revert" | "fork" | "retry", seq: number) => void }) {
  const { id: sessionId, workspace, folders, preview, toast, link, compose } = useContext(SessionContext);
  const focusChat = useFocusChat();
  const [folded, setFolded] = useDisclosed(`${sessionId}:run:${turn.key}`, false);
  const split = useReplies() === "split";
  if (turn.summary) return <SummaryBlock message={turn.summary} />;
  const elapsed = (live ? Date.now() : turn.endedAt) - turn.startedAt;
  const summary = activitySummary(turn, live, Date.now());
  const activeTool = live ? [...turn.activity].reverse().find((item) => item.kind === "tool" && item.running) as ToolItem | undefined : undefined;
  const activeAction = activeTool ? describe(activeTool) : null;
  const currentAction = summary.staleSeconds >= 30
    ? t("session.status.stale", { n: summary.staleSeconds })
    : activeAction
      ? `${activeAction.verb}${activeAction.detail ? ` · ${activeAction.detail}` : ""}`
    : live
      ? t(turn.answer ? "session.status.responding" : "session.status.preparing")
      : "";
  // History deletion is available even when the optional file checkpoint has expired.
  const canRevert = turn.user?.seq != null;
  const seq = turn.user?.seq ?? null;
  const settled = !!seq && !!onTurnAction && !live;
  const inbound = turn.user?.origin?.startsWith("inbound:") ? turn.user.origin.slice(8) : "";
  const fromPage = turn.user?.origin?.startsWith("page:") ? turn.user.origin.slice(5) : "";
  const files = live ? null : turnFiles(turn.activity, workspace, folders);
  // Split, every text the agent wrote between its steps is a reply of its own and cuts the steps
  // into blocks around it; merged, the texts fold into one block of steps with the tool calls.
  const segments: Segment[] = split ? splitActivity(turn.activity) : turn.activity.length || live ? [{ kind: "work", items: turn.activity }] : [];
  // While the run works past its last reply there is a block for what it is doing now to live in.
  if (split && live && !turn.answer && segments.at(-1)?.kind !== "work") segments.push({ kind: "work", items: [] });
  let lastWork = -1;
  let lastText = -1;
  segments.forEach((segment, index) => {
    if (segment.kind === "work") lastWork = index;
    else lastText = index;
  });
  // The copy, link and retry row goes under the last text of the turn: the answer, or, when the
  // turn ended on a step, the last reply before it.
  const closing = turn.answer ? null : lastText >= 0 && !live ? (segments[lastText] as Extract<Segment, { kind: "text" }>) : null;
  const answerActions = (text: string, answerSeq: number | undefined): MessageAction[] => [
    ...(seq ? [{ icon: "link" as IconName, label: t("turn.link"), onSelect: copyLink }] : []),
    ...(answerSeq && focusChat?.reply ? [{ icon: "reply" as IconName, label: t("reply.action"), onSelect: () => focusChat.reply!({ seq: answerSeq, excerpt: excerptOf(text) }) }] : []),
    ...(answerSeq && onTurnAction ? [{ icon: "reload" as IconName, label: t("turn.retry"), onSelect: () => onTurnAction("retry", answerSeq) }] : []),
  ];
  const turnMenu: MenuItem[] | undefined = settled
    ? [
        { label: t("session.fork.action"), icon: "fork", onSelect: () => onTurnAction!("fork", seq!) },
        ...(canRevert ? [{ label: t("session.revert.action.menu"), icon: "undo" as IconName, danger: true, onSelect: () => onTurnAction!("revert", seq!) }] : []),
      ]
    : undefined;
  const copyLink = async () => {
    const url = `${window.location.origin}${link}#m${seq}`;
    toast((await copyText(url)) ? t("turn.link.copied") : url);
  };
  return (
    <div className={`turn ${turn.note?.kind === "loop" ? "loop-turn" : ""} ${folded && !live ? "folded" : ""}`} id={seq ? `m${seq}` : undefined}>
      {/* The project's events the orchestrator was woken with are the news of the chat: a card, open. */}
      {/* A member's steps for the operator are a note of the host's too, drawn from their fields as a
          card of their own rather than as the markdown the model and the file carry. */}
      {turn.user && turn.note?.kind === "events" && (turn.user.operator_steps
        ? <StepsCard steps={turn.user.operator_steps} seq={seq} onOpen={preview} />
        : <EventCard text={turn.note.body} seq={seq} />)}
      {turn.user && turn.note?.kind === "events" && !turn.user.operator_steps && !live && <KeptFiles text={turn.note.body} onOpen={preview} />}
      {turn.user && turn.note && turn.note.kind !== "events" && <SystemNoteRow note={turn.note} cacheKey={live ? undefined : `n${seq ?? turn.key}`} run={turn.note.kind === "loop" && !live ? { folded, toggle: () => setFolded(!folded) } : undefined} />}
      <div className="turn-content" hidden={folded && !live}>
      {turn.user && !turn.note && (
        <div className="msg-wrap">
          <UserBubble>
            {inbound && <span className="msg-origin">{t("turn.origin.inbound", { source: inbound })}</span>}
            {fromPage && <span className="msg-origin" data-from-page={fromPage}>{t("turn.origin.page", { slug: fromPage })}</span>}
            {turn.user.autopilot && <span className="msg-origin msg-autopilot" data-autopilot={turn.user.autopilot.kind} title={autopilotTitle(turn.user.autopilot)}>{t(`turn.autopilot.${turn.user.autopilot.kind}`)}</span>}
            {turn.user.yagni && <span className="msg-origin msg-yagni" title={t("turn.yagni.title")}>{t(turn.user.yagni === "on" ? "turn.yagni.on" : "turn.yagni.off")}</span>}
            {turn.user.depth && <DepthChip depth={turn.user.depth} sessionId={sessionId} toast={toast} />}
            {turn.user.reply_to && <ReplyQuote reply={turn.user.reply_to} />}
            <Md className="msg-text" text={withoutAttachedList(turn.user.text)} cacheKey={live ? undefined : `u${seq ?? turn.key}`} />
            {turn.user.secrets?.length ? <div className="msg-secrets">{turn.user.secrets.map((secret) => <SecretChip key={secret.name} name={secret.name} scope={secret.scope} />)}</div> : null}
          </UserBubble>
          <KeptFiles text={turn.user.text} onOpen={preview} />
          {focusChat?.orchestrator && <MessageFate message={turn.user} sessionId={sessionId} projectId={focusChat.projectId} />}
          {/* Under the message, not beside it: a row beside the bubble is off-screen on a phone. */}
          <MessageActions
            text={turn.user.text}
            actions={
              settled
                ? ([
                    { icon: "fork", label: t("session.fork.action"), onSelect: () => onTurnAction!("fork", seq!) },
                    ...(canRevert ? [{ icon: "undo", label: t("session.revert.action.menu"), danger: true, onSelect: () => onTurnAction!("revert", seq!) }] : []),
                  ] as MessageAction[])
                : []
            }
          />
        </div>
      )}
      {segments.map((segment, index) => segment.kind === "work" ? (
        <Fragment key={`w${index}`}>
        <WorkBlock
          disclosure={index === 0 ? `${sessionId}:turn:${turn.key}` : `${sessionId}:turn:${turn.key}:${index}`}
          items={segment.items}
          live={live && index === lastWork}
          elapsed={index === lastWork ? elapsed : null}
          imported={turn.imported}
          current={index === lastWork ? currentAction : ""}
          waiting={live && index === lastWork && !turn.answer && turn.pendingTools === 0 && turn.activity.length > 0}
          orchestrator={!!focusChat?.orchestrator}
          onRetry={!live && onTurnAction ? (retrySeq) => onTurnAction("retry", retrySeq) : undefined}
        />
        {/* Outside the folded steps, right after the step that drew it: a widget is part of the answer,
            and it is drawn as soon as its call returns so the operator can use it while the agent goes on. */}
        <WidgetFrames sessionId={sessionId} widgets={drawnWidgets(segment.items)} toast={toast} compose={compose} />
        </Fragment>
      ) : (
        <Fragment key={`t${index}`}>
          <Md className="answer interim" text={segment.text} cacheKey={live ? undefined : `r${turn.key}-${index}`} />
          {!live && <KeptFiles text={segment.text} onOpen={preview} />}
          {closing === segment && <MessageActions text={segment.text} actions={answerActions(segment.text, segment.seq)} more={turnMenu} />}
        </Fragment>
      ))}
      {turn.activity.map((item) => item.kind === "tool" && item.name === "AskUser" ? <AskUserRecord key={item.id} item={item} /> : null)}
      {turn.fallback && (turn.answer || live) && <FallbackChip fallback={turn.fallback} />}
      {turn.answer && (turn.media?.length ? (
        <div className={`answer answer-with-media ${live ? "streaming" : ""}`}>
          {splitMediaAnswer(turn.answer, turn.media).map((part, index) => part.kind === "text"
            ? <Md key={`text-${index}`} text={part.text} cacheKey={live ? undefined : `a${turn.key}-${index}`} />
            : <InlineMedia key={part.presentation.id} sessionId={sessionId} presentation={part.presentation} onPreview={preview} />)}
        </div>
      ) : <Md className={`answer ${live ? "streaming" : ""}`} text={turn.answer} cacheKey={live ? undefined : `a${turn.key}`} />)}
      {turn.answer && !live && <KeptFiles text={turn.answer} onOpen={preview} />}
      {/* Outside the folded steps: a change that waits for the operator's Apply must be seen without
          opening the turn's work, and it can be answered while the agent is still writing. */}
      {settingsChangeIds(turn.activity).map((id) => <SettingsChangeCard key={id} changeId={id} />)}
      {files && <TurnFiles sessionId={sessionId} sent={files.sent} changed={files.changed} onOpen={preview} />}
      {/* A page the agent showed is drawn as soon as the call returns, not when the turn ends: the
          point of showing it is that the operator can open it while the agent goes on. */}
      <PageCards pages={turnPages(turn.activity)} />
      {turn.answer && !live && <MessageActions text={mediaCopyText(turn.answer, turn.media ?? [])} actions={answerActions(turn.answer, turn.answerSeq)} more={turnMenu} />}
      </div>
      {/* Outside the folded content: a loop's turn is folded by default, and this line is the one
          thing about it that must be seen without unfolding it. */}
      {turn.outcome && !live && <RunOutcomeLine outcome={turn.outcome} />}
    </div>
  );
});

/**
 * One block of a turn's steps under its folding line. A turn has one when its texts fold into the
 * steps and one between every two replies when they do not; the last block carries the turn's time
 * and what the run is doing now, the earlier ones only what they hold. Each folds on its own and
 * remembers it, and each folds itself away when the run that filled it ends.
 */
function WorkBlock({ disclosure, items, live, elapsed, imported, current, waiting, orchestrator, onRetry }: { disclosure: string; items: Activity[]; live: boolean; elapsed: number | null; imported?: string; current: string; waiting: boolean; orchestrator: boolean; onRetry?: (seq: number) => void }) {
  const always = useContext(StepsOpen);
  const [open, setOpen] = useDisclosed(disclosure, always);
  const wasLive = useRef(live);
  useEffect(() => {
    // Activity is opt-in, including during streaming; completion folds the details away — except
    // where the steps are what the reader came for.
    if (wasLive.current && !live && !always) setOpen(false);
    wasLive.current = live;
  }, [live]);
  const steps = stepCount(items);
  const families = open ? "" : orchestrator ? stepLine(items) : familyLine(items);
  const label = elapsed !== null
    ? imported && !live ? t("imp.worked", { name: harnessMeta(imported).name }) : t(live ? "session.activity" : "session.worked", { t: duration(elapsed) })
    : steps > 0 ? plural("session.part.steps", steps) : t("session.reasoning");
  return (
    <>
      <button className="thinking-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className={`dots ${live ? "on" : ""}`}>
          <i />
          <i />
          <i />
        </span>
        <span className="worked">{label}</span>
        {current && <span className="activity-current truncate">{current}</span>}
        {elapsed !== null && steps > 0 && <span className="steps">{plural("session.steps", steps)}</span>}
        {families && <span className="families truncate" title={families}>· {families}</span>}
        <Chevron open={open} />
      </button>
      {open && (
        <div className="activity">
          <ActivityList items={items} compact={false} onRetry={onRetry} />
          {waiting && <div className="working">{t("session.working")}</div>}
        </div>
      )}
    </>
  );
}

let repliesNow: Prefs["replies"] | null = null;
function subscribeReplies(changed: () => void): () => void {
  const fresh = () => {
    repliesNow = null;
    changed();
  };
  window.addEventListener("daedalus-appearance", fresh);
  window.addEventListener("storage", fresh);
  return () => {
    window.removeEventListener("daedalus-appearance", fresh);
    window.removeEventListener("storage", fresh);
  };
}

/** How the agent's texts between steps are drawn, read once per change rather than once per turn:
 *  six hundred turns each parsing the stored preferences on every token would be the whole frame. */
function useReplies(): Prefs["replies"] {
  return useSyncExternalStore(subscribeReplies, () => (repliesNow ??= readPrefs().replies));
}

/**
 * The operator's bubble. On a phone its text stops at six lines with "Show all" under it: a long
 * voice note filled half the screen and pushed the answer it asked for out of sight. Only the
 * phone's stylesheet caps the height, so the button appears only where the text actually overflows
 * — a desktop never measures an overflow and never shows it.
 */
function UserBubble({ children }: { children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null);
  const [over, setOver] = useState(false);
  const [all, setAll] = useState(false);
  useLayoutEffect(() => {
    const text = box.current?.querySelector<HTMLElement>(".msg-text");
    if (!text || all) return;
    const measure = () => setOver(text.scrollHeight > text.clientHeight + 2);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(text);
    return () => observer.disconnect();
  }, [all]);
  return (
    <div ref={box} className={`msg user ${all ? "" : "clamped"} ${over && !all ? "over" : ""}`}>
      {children}
      {over && !all && <button type="button" className="msg-more" onClick={() => setAll(true)}>{t("turn.showall")}<Icon name="chevron" size={14} /></button>}
    </div>
  );
}

/** The closing line of a run that produced no answer: that there is none, why the run stopped, and which
 *  step or compaction tier gave out. Part of the turn, so a "Context summary" drawn after it cannot hide it. */
function RunOutcomeLine({ outcome }: { outcome: RunOutcome }) {
  const pass = outcome.compaction;
  const failures = Object.entries(pass?.summariser_failures ?? {})
    .map(([kind, count]) => `${kind} ${count}`)
    .join(", ");
  return (
    <div className={`run-outcome cause-${outcome.cause}`} role="status" aria-label={t("run.outcome.title")}>
      {/* An alert, not a question mark: on the red card a "?" read as a glyph that failed to load. */}
      <Icon name="alert" size={14} />
      <div>
        <b>{t("run.outcome.title")}</b> — {t(`run.outcome.cause.${outcome.cause}`)}
        {!!outcome.steps && (
          <div className="run-outcome-detail">
            {outcome.last_tool ? t("run.outcome.steps.last", { n: outcome.steps, tool: outcome.last_tool }) : t("run.outcome.steps", { n: outcome.steps })}
          </div>
        )}
        {/* The engine's own words ("force_compaction", "at_floor") are for whoever debugs the run;
            the sentence above is the explanation, and these wait behind a disclosure. */}
        {(outcome.detail || pass) && (
          <details className="run-outcome-more">
            <summary>{t("run.outcome.more")}</summary>
            {outcome.detail && <div className="run-outcome-detail">{outcome.detail}</div>}
            {pass && (
              <div className="run-outcome-detail">
                {t("run.outcome.compaction", { outcome: pass.outcome || "—" })}
                {failures && <>{" · "}{t("run.outcome.failures", { list: failures })}</>}
                {!!pass.floor_dropped && <>{" · "}{t("run.outcome.floor", { n: pass.floor_dropped })}</>}
              </div>
            )}
          </details>
        )}
      </div>
    </div>
  );
}

/** Three turn-shaped blocks while the first read is on its way. */
function TurnSkeleton() {
  return (
    <div className="turn-skeleton" aria-hidden>
      {[0, 1, 2].map((i) => (
        <div key={i} className="turn">
          <div className="skeleton sk-user" style={{ width: `${34 + (i * 13) % 30}%` }} />
          <div className="skeleton sk-line" style={{ width: "38%" }} />
          <div className="skeleton sk-line" style={{ width: `${70 + (i * 9) % 25}%` }} />
          <div className="skeleton sk-line" style={{ width: `${50 + (i * 17) % 40}%` }} />
        </div>
      ))}
    </div>
  );
}

/**
 * The turn the run is writing right now. It reads the stream directly, so a token repaints this
 * component and nothing else: the settled turns above it never hear about it.
 */
function LiveTurn({ base, live, onTurnAction, onRender }: { base: Turn | null; live: LiveStore; onTurnAction?: (kind: "revert" | "fork" | "retry", seq: number) => void; onRender?: () => void }) {
  const state = useSyncExternalStore(live.subscribe, live.get);
  useClock(1000);
  useLayoutEffect(() => onRender?.());
  const turn = applyLive(base, state, Date.now());
  // `ended` is the model's full stop. From it the turn reads as written — no cursor under it, no
  // dots over it — whatever the session is still doing behind the answer.
  return (
    <Safe>
      <TurnView turn={turn} live={!state.ended} onTurnAction={onTurnAction} />
    </Safe>
  );
}

/**
 * A conversation's turns and nothing around them, for a page that reads and follows the session on
 * its own: the extension's side panel. The same turns the chat draws, from the same pieces, so the
 * panel never grows a second way of showing a step. The page keys it by the session's id.
 */
export function TranscriptView({ detail, live, onPreview, toast, onRender }: { detail: SessionDetail; live: LiveStore; onPreview: (src: PreviewSource) => void; toast: (text: string) => void; onRender?: () => void }) {
  const id = detail.id;
  const status = (detail.status ?? "idle") as Status;
  const busy = status === "running" || status === "waiting";
  const built = useRef<Turn[]>([]);
  const turns = useMemo(() => {
    built.current = buildTurns(detail.messages ?? [], built.current);
    return built.current;
  }, [detail.messages]);
  const tail = busy ? liveBase(turns, detail.run_id) : null;
  const settled = tail ? turns.slice(0, -1) : turns;
  useEffect(() => () => forgetDisclosed(id), [id]);
  const ctx = useMemo(() => ({
    id,
    workspace: detail.workspace ?? "",
    folders: detail.folders ?? NO_FOLDERS,
    preview: onPreview,
    openJobs: () => undefined,
    toast,
    link: sessionPath(id),
    asking: questionsKey(detail.pending?.questions ?? null),
  }), [id, detail.workspace, detail.folders, onPreview, toast, detail.pending?.questions]);
  useLayoutEffect(() => onRender?.(), [detail.messages, onRender]);
  return (
    <div className="timeline panel-chat">
      <SessionContext.Provider value={ctx}>
        {settled.map((turn) => <Safe key={turn.key}><TurnView turn={turn} live={false} /></Safe>)}
        {busy && <LiveTurn base={tail} live={live} onRender={onRender} />}
      </SessionContext.Provider>
    </div>
  );
}

/** How much of a helper's conversation the panel reads: its newest messages. A helper's run is one task,
 *  and the full chat is a button away for the rare one longer than this. */
const SUBAGENT_TAIL = 160;

/**
 * A subagent's conversation as the Subagents tab shows it beside the leader: read-only, following the
 * helper's own event stream so its run is drawn as it happens, every block of steps open. Nothing in it
 * acts on the helper — no composer, no fork or revert — because the leader is the one who talks to it.
 */
export function SubagentTranscript({ id, onPreview, toast }: { id: string; onPreview: (src: PreviewSource) => void; toast: (text: string) => void }) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [failed, setFailed] = useState(false);
  const liveRef = useRef<LiveStore | null>(null);
  if (!liveRef.current) liveRef.current = createLiveStore();
  const live = liveRef.current;
  const box = useRef<HTMLDivElement>(null);
  const seq = useRef(0);
  const load = useCallback(async () => {
    const mine = ++seq.current;
    try {
      const next = await api.get<SessionDetail>(`/api/sessions/${id}?tail=${SUBAGENT_TAIL}`);
      if (mine === seq.current) { setDetail(next); setFailed(false); }
    } catch {
      if (mine === seq.current) setFailed(true);
    }
  }, [id]);
  const soon = useRef<number | null>(null);
  const loadSoon = useCallback(() => {
    if (soon.current) return;
    soon.current = window.setTimeout(() => { soon.current = null; void load(); }, EVENT_COALESCE_MS);
  }, [load]);
  useEffect(() => {
    void load();
    const stop = followSession(id, {
      handle: (event, p) => {
        live.update((state) => liveAfter(state, event, p));
        if (event === "message_start") setDetail((prev) => (prev && prev.status !== "running" ? { ...prev, status: "running", run_id: p.run_id || prev.run_id } : prev));
        if (event === "run_settled") setDetail((prev) => (prev ? { ...prev, status: p.status === "awaiting" ? "waiting" : p.status === "failed" ? "failed" : "idle" } : prev));
        if (event === "message_start" || event === "message_stop" || event === "run_settled" || event === "compaction_completed" || event === "history_cut" || (event === "steer_changed" && p.reason === "consumed")) loadSoon();
      },
      onDrop: () => { live.reset(); void load(); },
    });
    return () => {
      stop();
      if (soon.current) window.clearTimeout(soon.current);
      soon.current = null;
      live.reset();
      forgetDisclosed(id);
    };
  }, [id, live, load, loadSoon]);
  const status = (detail?.status ?? "idle") as Status;
  const busy = status === "running" || status === "waiting";
  useEffect(() => { if (!busy) live.reset(); }, [busy, live]);
  const built = useRef<Turn[]>([]);
  const turns = useMemo(() => {
    built.current = buildTurns(detail?.messages ?? [], built.current);
    return built.current;
  }, [detail?.messages]);
  const tail = busy ? liveBase(turns, detail?.run_id) : null;
  const settled = tail ? turns.slice(0, -1) : turns;
  // The panel scrolls, not this list: it follows the end while the reader is at it, as the chat does,
  // and lands there once when the conversation first arrives.
  const scroller = () => box.current?.closest<HTMLElement>(".panel-body, .sheet-body") ?? null;
  const landed = useRef(false);
  const follow = useCallback(() => {
    const el = scroller();
    if (!el) return;
    if (!landed.current || el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTop = el.scrollHeight;
  }, []);
  useLayoutEffect(() => {
    if (!detail) return;
    follow();
    landed.current = true;
  }, [detail?.messages, follow]);
  const ctx = useMemo(() => ({
    id,
    workspace: detail?.workspace ?? "",
    folders: detail?.folders ?? NO_FOLDERS,
    preview: onPreview,
    openJobs: () => undefined,
    toast,
    link: sessionPath(id),
    asking: questionsKey(detail?.pending?.questions ?? null),
  }), [id, detail?.workspace, detail?.folders, onPreview, toast, detail?.pending?.questions]);
  if (!detail) return failed ? <div className="empty">{t("subagents.unreadable")}</div> : <TurnSkeleton />;
  return (
    <div ref={box} className="timeline subagent-chat" data-subagent={id}>
      {detail.messages.length >= SUBAGENT_TAIL && <div className="sub older-note">{t("subagents.older")}</div>}
      <SessionContext.Provider value={ctx}>
        <StepsOpen.Provider value={true}>
          {settled.map((turn) => <Safe key={turn.key}><TurnView turn={turn} live={false} /></Safe>)}
          {busy && <LiveTurn base={tail} live={live} onRender={follow} />}
        </StepsOpen.Provider>
      </SessionContext.Provider>
      {!busy && turns.length === 0 && <div className="empty">{t("subagents.idle.empty")}</div>}
    </div>
  );
}

/** The bar over the composer while a run is going: what the agent is on, and for how long.
 *
 *  It stays for the moment after the run, quieter, while the turn is being written down — see
 *  `saving` above the composer. Same bar rather than a second one: the thing it reports on is the
 *  same turn, and a line that appears somewhere else would read as a new event.
 */
/** The run, as one line in the header: dot, word, elapsed, step, what the agent is doing. Clicking it
 *  brings the newest content back into view. The `livebar` classes are what the settle harness watches. */
export function LiveBar({ status, saving, base, live, workspace, onJump }: { status: Status; saving: boolean; base: Turn | null; live: LiveStore; workspace?: string; onJump: () => void }) {
  const state = useSyncExternalStore(live.subscribe, live.get);
  useClock(1000);
  if (saving) {
    return (
      <button className="livebar head-status saving" onClick={onJump} role="status" aria-live="polite" title={t("session.jump.step")}>
        <Dot status="idle" />
        <b>{t("session.livebar.saving")}</b>
      </button>
    );
  }
  const turn = applyLive(base, state, Date.now());
  const steps = stepCount(turn.activity);
  const running = [...turn.activity].reverse().find((a) => a.kind === "tool" && a.running) as ToolItem | undefined;
  // A session waiting on the operator has nothing in progress, whatever its last turn holds.
  const step =
    status === "waiting"
      ? t("session.livebar.waiting")
      : running
        ? (() => {
            const d = describe(running, workspace);
            return `${d.verb}${d.detail ? ` ${d.detail}` : ""}`;
          })()
        : state.ended
          ? t("session.livebar.saving")
          : turn.answer
            ? t("session.livebar.writing")
            : state.thinking
              ? t("session.reasoning")
              : t("session.livebar.thinking");
  return (
    <button className={`livebar head-status ${status}`} onClick={onJump} role="status" aria-live="polite" title={t("session.jump.step")}>
      <Dot status={status} />
      <b>{statusWord(status === "waiting" ? "waiting" : "running")}</b>
      <span className="num">{duration(Date.now() - turn.startedAt)}</span>
      {steps > 0 && <span>{t("session.livebar.step", { n: steps })}</span>}
      {step && <span className="truncate">· {step}</span>}
    </button>
  );
}

/** The phone bar's second line during a run: the state and how long the turn has taken. */
function LiveSub({ status, base, live }: { status: Status; base: Turn | null; live: LiveStore }) {
  const state = useSyncExternalStore(live.subscribe, live.get);
  useClock(1000);
  const turn = applyLive(base, state, Date.now());
  const waitingNow = status === "waiting";
  return <StateSub tone={waitingNow ? "waiting" : "running"}>{waitingNow ? t("session.phone.waiting") : `${statusWord("running")} · ${duration(Date.now() - turn.startedAt)}`}</StateSub>;
}

/** The 2 px line along the header's bottom edge: a shimmer while a run is on, a measured width while
 *  the history is being compacted. */
function HeadProgress({ status, compacting }: { status: Status; compacting: Compacting | null }) {
  const pct = compacting ? compactionPct(compacting) : null;
  // A session waiting on the operator is not progressing: the line holds still, in the waiting colour.
  const mode = pct !== null ? "measured" : status === "waiting" ? "waiting" : "busy";
  return (
    <div className={`head-progress ${mode}`} aria-hidden>
      <i style={pct === null ? undefined : { width: `${pct}%` }} />
    </div>
  );
}

function compactionPct(c: Compacting): number {
  const parts = c.parts_total > 1 ? Math.round((100 * c.parts_done) / c.parts_total) : 0;
  return c.stage === "writing" ? 97 : c.stage === "merging" ? 88 : c.parts_total > 1 ? Math.round(parts * 0.8) : 35;
}

/** A repaint on a timer, for the elapsed times — and only in the components that show one. */
function useClock(ms: number): void {
  const [, beat] = useState(0);
  useEffect(() => {
    const t = setInterval(() => beat((n) => n + 1), ms);
    return () => clearInterval(t);
  }, [ms]);
}

type MessageAction = { icon: IconName; label: string; danger?: boolean; onSelect: () => void };

/** The row of small buttons under a message: copy it, and whatever else the turn allows; the rest behind ⋯. */
function MessageActions({ text, actions = [], more }: { text: string; actions?: MessageAction[]; more?: MenuItem[] }) {
  // A phone shows the same icons as a desktop, in 44 px targets (ui/phone.css): the word "Copy"
  // beside one icon and every other action behind ⋯ made the turn's commands a hunt.
  const menu = more ?? [];
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    setCopied(await copyText(text));
    window.setTimeout(() => setCopied(false), 1600);
  };
  const controls = useRef<HTMLDivElement>(null);
  const context = useRef<HTMLElement>(null);
  useEffect(() => {
    // The text this row belongs to is the nearest answer above it: a turn whose replies split its
    // steps holds several, and the first of them is not the one the row copies.
    let above = controls.current?.previousElementSibling ?? null;
    while (above && !above.classList.contains("answer")) above = above.previousElementSibling;
    context.current = controls.current?.closest<HTMLElement>(".msg-wrap") ?? (above as HTMLElement | null);
  });
  useContextActions(context, [{ label:t("common.copy"), icon:"copy", onSelect:() => void copy() }, ...actions, ...(more ?? [])]);
  return (
    <div ref={controls} className="msg-actions">
      <button className="iconbtn small" onClick={copy} aria-label={t(copied ? "common.copied" : "common.copy")} title={t(copied ? "common.copied" : "common.copy")}>
        <Icon name={copied ? "check" : "copy"} size={15} />
      </button>
      {actions.map((a) => (
        <button key={a.label} className={`iconbtn small ${a.danger ? "danger" : ""}`} onClick={a.onSelect} aria-label={a.label} title={a.label}>
          <Icon name={a.icon} size={15} />
        </button>
      ))}
      {menu.length > 0 && <OverflowMenu small label={t("turn.more")} items={menu} />}
    </div>
  );
}

function SummaryBlock({ message }: { message: MessageView }) {
  const [open, setOpen] = useState(false);
  const meta = message.compaction;
  // An imported chat starts with the summary of its early part: the other program's own compactions
  // and Daedalus's summary of the turns after them, said as the start of the conversation.
  const imported = message.imported?.harness && meta?.reason === "import";
  return (
    <div className={`summary ${imported ? "imp-summary" : ""}`}>
      <button className="summary-head" onClick={() => setOpen((o) => !o)}>
        <Icon name="compact" /> {imported ? t("imp.summary") : t("session.summary")}
        {imported ? t("imp.summary.meta", { n: meta?.messages ?? 0 }) : meta ? t("session.summary.meta", { n: meta.messages ?? 0, reason: meta.reason }) : ""}
        <Chevron open={open} />
      </button>
      {open && <Md className="summary-body" text={message.text} />}
    </div>
  );
}

/** The verb of a step, in the reader's language: "Ran command", "Выполнил команду". */
function verb(name: string, running: boolean): string {
  return t(`tool.${name}.${running ? "on" : "off"}`);
}

/** Verb + detail for a tool ("Ran command", "Read file"); the icon and the family it groups under. */
function describe(item: ToolItem, workspace?: string): { verb: string; family: string; detail: string; icon: IconName } {
  const a = item.args;
  const str = (k: string) => (typeof a[k] === "string" ? (a[k] as string) : a[k] === undefined ? "" : JSON.stringify(a[k]));
  const base = (p: string) => p.split("/").filter(Boolean).pop() ?? p;
  const r = item.running;
  switch (item.name) {
    case "Exec":
      return { verb: verb("Exec", r), family: "Exec", detail: commandPreview(str("command"), workspace), icon: "terminal" };
    case "Read":
      return { verb: verb("Read", r), family: "Read", detail: base(str("path")), icon: "file" };
    case "Write":
      return { verb: verb("Write", r), family: "Write", detail: base(str("path")), icon: "pen" };
    case "Edit":
      return { verb: verb("Edit", r), family: "Edit", detail: base(str("path")), icon: "pen" };
    case "Find":
    case "Search":
      return { verb: verb("Find", r), family: "search", detail: str("pattern") || str("query"), icon: "search" };
    case "WebSearch":
      return { verb: verb("WebSearch", r), family: "search", detail: str("query"), icon: "search" };
    case "WebFetch":
      return { verb: verb("WebFetch", r), family: "WebFetch", detail: str("url").replace(/^https?:\/\//, "").slice(0, 60), icon: "globe" };
    case "SendFile":
      return { verb: verb("SendFile", r), family: "SendFile", detail: base(str("path")), icon: "attach" };
    case "Widget":
      return { verb: verb("Widget", r), family: "Widget", detail: str("title"), icon: "sliders" };
    case "ImageView":
      return { verb: verb("ImageView", r), family: "ImageView", detail: `${base(str("path"))}${str("task") ? " · " + str("task").slice(0, 60) : ""}`, icon: "image" };
    case "AttachMedia":
      return { verb: verb("AttachMedia", r), family: "media", detail: `${Array.isArray(a.items) ? a.items.length : 1}`, icon: "image" };
    case "AskUser":
      return { verb: t("tool.AskUser"), family: "AskUser", detail: "", icon: "question" };
    case "Skill":
      return { verb: verb("Skill", r), family: "Skill", detail: str("skill") || str("name"), icon: "skill" };
    case "SettingsGet":
      return { verb: verb("SettingsGet", r), family: "SettingsGet", detail: str("section"), icon: "settings" };
    case "SettingsChange":
      return { verb: verb("SettingsChange", r), family: "SettingsChange", detail: a.changes && typeof a.changes === "object" ? Object.keys(a.changes).join(", ") : "", icon: "settings" };
    case "Remember":
    case "Recall":
    case "Forget":
      return { verb: t(`tool.${item.name}`), family: item.name, detail: str("query") || str("text").slice(0, 60), icon: "bulb" };
    case "Verify":
      return { verb: r ? t("tool.Verify.on") : item.error ? t("tool.Verify.failed") : t("tool.Verify.off"), family: "Verify", detail: str("criterion"), icon: "wrench" };
    case "SubAgent":
      return { verb: verb("SubAgent", r), family: "SubAgent", detail: str("name") || str("task").split("\n")[0].slice(0, 60), icon: "spawn" };
    case "SubAgentList":
      return { verb: t("tool.SubAgentList"), family: "SubAgentList", detail: "", icon: "spawn" };
    case "SubAgentSend":
    case "SubAgentDelete":
      return { verb: verb(item.name, r), family: item.name, detail: str("name"), icon: "spawn" };
    case "ReportToLeader":
      return { verb: verb("ReportToLeader", r), family: "ReportToLeader", detail: str("text").split("\n")[0].slice(0, 80), icon: "spawn" };
    case "SpawnAgent":
      return { verb: verb("SpawnAgent", r), family: "SpawnAgent", detail: str("title"), icon: "spawn" };
    case "AskPeer":
      return { verb: verb("AskPeer", r), family: "AskPeer", detail: str("name"), icon: "question" };
    case "StaySilent":
      return { verb: t("tool.StaySilent"), family: "StaySilent", detail: str("note").slice(0, 60), icon: "dot" };
    case "SavePrompt":
      return { verb: verb("SavePrompt", r), family: "SavePrompt", detail: str("title"), icon: "wand" };
    case "HistorySearch":
      return { verb: verb("HistorySearch", r), family: "search", detail: str("query"), icon: "search" };
    case "ToolSearch": {
      // A search that loaded a group says which, and how many tools came with it; the query is what
      // the model typed, and the group is what the reader wants to know it now has.
      const loaded = item.groups?.length ? item.groups : null;
      const asked = searchedGroup(a);
      if (loaded || asked) {
        const detail = loaded ? loaded.map((g) => groupDetail(g.group, g.tools)).join(", ") : groupName(asked);
        return { verb: t(`tool.grouploaded.${r ? "on" : "off"}`), family: "ToolSearch", detail, icon: "wrench" };
      }
      return { verb: verb("ToolSearch", r), family: "ToolSearch", detail: str("query") || str("select"), icon: "search" };
    }
    // Calls imported from another program that have no native tool of their own: the host keeps
    // them under their own names, and they read as the native step they amount to.
    case "Task":
    case "Agent":
      return { verb: verb("SubAgent", r), family: "SubAgent", detail: str("description") || str("prompt").split("\n")[0].slice(0, 60), icon: "spawn" };
    case "TodoWrite":
    case "todowrite":
    case "update_plan": {
      const list = Array.isArray(a.todos) ? a.todos : Array.isArray(a.plan) ? a.plan : [];
      const finished = list.filter((x) => typeof x === "object" && x && ["completed", "done"].includes(String((x as Record<string, unknown>).status))).length;
      return { verb: t("imp.tool.plan"), family: "plan", detail: t("imp.tool.plan.n", { n: list.length, done: finished }), icon: "check" };
    }
    case "apply_patch": {
      const patch = str("input") || str("patch") || str("raw");
      const files = [...patch.matchAll(/^\*\*\* (?:Update|Add|Delete) File: (.+)$/gm)].map((m) => base(m[1].trim()));
      return { verb: verb("Edit", r), family: "Edit", detail: files.join(", ").slice(0, 60), icon: "pen" };
    }
    case "HistoryExpand":
      return { verb: t("tool.HistoryExpand"), family: "HistoryExpand", detail: t("tool.HistoryExpand.range", { from: str("from_seq"), to: str("to_seq") }), icon: "file" };
    default: {
      if (item.name.startsWith("Board")) {
        const what = item.name.replace(/^Board/, "").toLowerCase();
        const word = what === "add" || what === "update" ? t(`tool.board.${what}.${r ? "on" : "off"}`) : what === "get" ? t("tool.board.get") : t("tool.board.list");
        return { verb: t("tool.board", { what: word }), family: item.name, detail: str("title") || str("task_id") || str("id"), icon: "skill" };
      }
      // The names of the agent's own machinery are the tool ids themselves: they are what the agent
      // writes in its own reasoning and what the documentation calls them, so they stay as they are.
      if (item.name.startsWith("Self")) return { verb: item.name.replace(/^Self/, "Self: "), family: item.name, detail: str("branch") || str("title") || str("repo"), icon: "wrench" };
      if (item.name.startsWith("Schedule")) return { verb: item.name.replace(/^Schedule/, "Schedule: "), family: item.name, detail: str("name") || str("schedule_id"), icon: "clock" };
      if (item.name.startsWith("Service")) {
        const what = item.name.replace(/^Service/, "").toLowerCase();
        const word = what === "start" || what === "stop" ? verb(`Service${what[0].toUpperCase()}${what.slice(1)}`, r) : what === "logs" ? t("tool.ServiceLogs") : t("tool.ServiceList");
        return { verb: word, family: item.name, detail: str("name") ? `${str("name")}${str("command") ? " · " + str("command").slice(0, 50) : ""}` : "", icon: "globe" };
      }
      if (item.name.startsWith("Loop")) return { verb: item.name.replace(/^Loop/, "Loop: "), family: item.name, detail: str("reason") || str("note") || str("instruction").slice(0, 60), icon: "loop" };
      if (item.name.startsWith("Mcp")) {
        // Mcp_Postingboard_read_thread → "postingboard · read thread", with the first string argument as the detail.
        const parts = item.name.replace(/^Mcp_?/, "").split("_");
        const server = (parts.shift() ?? "").toLowerCase();
        const tool = parts.join(" ").replace(/_/g, " ");
        const first = Object.values(a).find((v) => typeof v === "string") as string | undefined;
        return { verb: `${server} · ${tool}`.trim(), family: item.name, detail: (first ?? "").slice(0, 60), icon: "plug" };
      }
      // A wrench, not the dot: at the step list's size the dot read as an icon that failed to load.
      return { verb: item.name, family: item.name, detail: Object.keys(a).length ? JSON.stringify(a).slice(0, 60) : "", icon: "wrench" };
    }
  }
}

function ActivityList({ items, compact, onRetry }: { items: Activity[]; compact: boolean; onRetry?: (seq: number) => void }) {
  const steps = !!useFocusChat()?.orchestrator;
  const out: ReactElement[] = [];
  let i = 0;
  while (i < items.length) {
    const it = items[i];
    if (it.kind === "note") {
      out.push(
        <div key={i} className="note">
          <Md className="md" text={it.text} />
          {it.seq && onRetry && <MessageActions text={it.text} actions={[{ icon: "reload", label: t("turn.retry"), onSelect: () => onRetry(it.seq!) }]} />}
        </div>,
      );
      i++;
      continue;
    }
    if (it.kind === "thinking") {
      if (!compact) out.push(<ThoughtBlock key={i} text={it.text} />);
      i++;
      continue;
    }
    if (it.kind === "summary") {
      let j = i;
      while (j < items.length && items[j].kind === "summary") j++;
      const group = items.slice(i, j) as SummaryItem[];
      if (group.length > 1) out.push(<SummaryGroup key={i} group={group} />);
      else out.push(<SummaryRow key={i} text={it.text} reason={it.reason} />);
      i = j;
      continue;
    }
    if (it.kind === "group") {
      out.push(<GroupLoadRow key={`g${i}`} group={it.group} tools={it.tools} />);
      i++;
      continue;
    }
    // The agent's questions are drawn as their record outside the folded steps (TurnView).
    if (it.kind === "tool" && it.name === "AskUser") {
      i++;
      continue;
    }
    // The orchestrator's own steps are one row each, named in the project's words: "Task created ·
    // Photos" twice says more than "Called Tasks 2 times".
    if (steps && stepDescription(it)) {
      out.push(<ToolRow key={it.id} item={it} />);
      i++;
      continue;
    }
    // Group consecutive tools of one family (Read/Read/Read → "Read 3 files").
    const family = it.name;
    let j = i;
    while (j < items.length && items[j].kind === "tool" && (items[j] as ToolItem).name === family) j++;
    const group = items.slice(i, j) as ToolItem[];
    if (group.length > 1) out.push(<ToolGroup key={i} family={family} group={group} />);
    else out.push(<ToolRow key={it.id} item={it} />);
    i = j;
  }
  return <>{out}</>;
}

/** "Loaded tool group: Browser (12 tools)" — a group the run loaded by calling one of its tools by name. */
function GroupLoadRow({ group, tools }: { group: string; tools: number }) {
  return (
    <div className="act-wrap">
      <div className="act grouploaded">
        <Icon name="wrench" size={16} />
        <span className="verb">{t("tool.grouploaded.off")}</span>
        <span className="detail">{groupDetail(group, tools)}</span>
      </div>
    </div>
  );
}

function ToolGroup({ family, group }: { family: string; group: ToolItem[] }) {
  const running = group.some((g) => g.running);
  const [open, setOpen] = useState(running);
  useEffect(() => {
    if (running) setOpen(true);
  }, [running]);
  const d = describe(group[group.length - 1]);
  return (
    <div className="group">
      <div className={`act head ${running ? "running" : ""}`} onClick={() => setOpen((o) => !o)}>
        <Icon name={d.icon} />
        <span className="verb">{groupVerb(d.family, running, group.length, family)}</span>
        <span className="act-end"><Chevron open={open} /></span>
      </div>
      {open && group.map((g) => <ToolRow key={g.id} item={g} nested />)}
    </div>
  );
}

/** "Read 3 files", "Прочитал 3 файла" — the whole line, because the count sits inside it. */
const GROUPED = ["Exec", "Read", "Write", "Edit", "search", "WebFetch", "SendFile", "media"];

function groupVerb(family: string, running: boolean, n: number, name: string): string {
  if (!GROUPED.includes(family)) return plural(`tool.group.other${running ? "" : ".done"}`, n, { name });
  return plural(`tool.group.${family}${running ? "" : ".done"}`, n);
}

function SummaryGroup({ group }: { group: SummaryItem[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="act-wrap">
      <div className="act prose" onClick={() => setOpen((o) => !o)}>
        <Icon name="compact" />
        <span className="verb">{t("session.compacted.title")}</span>
        <span className="detail">{t("session.compacted.group", { n: group.length })}</span>
        <span className="act-end"><Chevron open={open} /></span>
      </div>
      {open && group.map((s, k) => <SummaryRow key={k} text={s.text} reason={s.reason} />)}
    </div>
  );
}

function SummaryRow({ text, reason }: { text: string; reason: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="act-wrap">
      <div className="act prose" onClick={() => setOpen((o) => !o)}>
        <Icon name="compact" />
        <span className="verb">{t("session.compacted.title")}</span>
        <span className="detail">{reason !== "auto" ? `${reason} · ` : ""}{plainPreview(text, 100)}</span>
        <span className="act-end"><Chevron open={open} /></span>
      </div>
      {open && <Md className="summary-inline" text={text} />}
    </div>
  );
}

function ThoughtBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="act-wrap">
      <div className="act prose" onClick={() => setOpen((o) => !o)}>
        <Icon name="bulb" />
        <span className="verb">{t("session.reasoning")}</span>
        <span className="detail">{plainPreview(text, 100)}</span>
        <span className="act-end"><Chevron open={open} /></span>
      </div>
      {open && <div className="thought">{text}</div>}
    </div>
  );
}

/** The tools whose detail is a file the panel can open. */
const FILE_TOOLS = ["Read", "Write", "Edit", "ImageView", "SendFile"];

/** The steps that name one subagent, and so offer to open it beside the leader. */
const SUBAGENT_STEPS = ["SubAgent", "SubAgentSend"];

/** Which subagent a step names: the session the host's answer reports, else the name in the call. */
function subagentOfStep(item: ToolItem): { id?: string; name?: string } | null {
  const id = /\(session ([\w-]+)\)/.exec(item.result ?? "")?.[1];
  const name = typeof item.args.name === "string" && item.args.name.trim() ? item.args.name.trim() : undefined;
  return id || name ? { id, name } : null;
}

/** An AskUser call's record, drawn outside the turn's folded steps (TurnView): what the operator
 *  decided is read back without opening the work. The call the session still waits on draws nothing
 *  here — its card is above the composer. */
function AskUserRecord({ item }: { item: ToolItem }) {
  const { asking } = useContext(SessionContext);
  const asked = Array.isArray(item.args.questions) ? (item.args.questions as Question[]) : [];
  const waiting = item.result === undefined && asking !== "" && asking === questionsKey(asked);
  if (waiting) return null;
  return <AskRecord item={item} />;
}

function ToolRow({ item, nested }: { item: ToolItem; nested?: boolean }) {
  const { id: sessionId, workspace, folders, preview, openJobs } = useContext(SessionContext);
  const [open, setOpen] = useDisclosed(`${sessionId}:tool:${item.id}`, false);
  const openSubagent = useContext(SubagentOpener);
  const subagentRef = SUBAGENT_STEPS.includes(item.name) ? subagentOfStep(item) : null;
  const step = useFocusChat()?.orchestrator ? stepDescription(item) : null;
  const d = step ?? describe(item, workspace);
  const expanded = open || (item.running && item.name === "Exec");
  // The file a step names is the evidence: it opens where the answer's citations open.
  const path = FILE_TOOLS.includes(item.name) ? toolSource(item, sessionId, workspace, folders) : null;
  const openFile = (e: React.MouseEvent) => {
    if (!path) return;
    e.stopPropagation();
    preview(path);
  };
  return (
    <div className={`act-wrap ${nested ? "nested" : ""}`}>
      <div className={`act ${step ? "step" : ""} ${item.error ? "error" : ""} ${item.running ? "running" : ""}`} onClick={() => setOpen((o) => !o)} role="button" aria-expanded={expanded} tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setOpen((o) => !o); } }}>
        <Icon name={d.icon} size={16} />
        <span className="verb">{d.verb}</span>
        {d.detail && (path ? (
          <button type="button" className="detail link" onClick={openFile} title={t("turn.open.file", { name: d.detail })}>{d.detail}</button>
        ) : (
          <span className="detail">{d.detail}</span>
        ))}
        <span className="act-end">
          {/* A step imported from another program keeps the name it had there, beside Daedalus's own verb. */}
          {item.orig && item.orig !== d.verb && <span className="act-orig" title={t("imp.tool.orig", { name: item.orig })}>{item.orig}</span>}
          {item.error && !item.running && <span className="failed">{t("turn.failed")}</span>}
          {item.name === "Verify" && !item.running && (
            <button type="button" className="receipt-link" onClick={(e) => { e.stopPropagation(); openJobs(); }}>{t("turn.receipt")}</button>
          )}
          {openSubagent && subagentRef && (
            <button type="button" className="receipt-link subagent-link" onClick={(e) => { e.stopPropagation(); openSubagent(subagentRef); }}>{t("subagents.open.one")}</button>
          )}
          {item.ms !== undefined && item.ms >= 500 && <span className="dur num">{duration(item.ms)}</span>}
          <Chevron open={expanded} />
        </span>
      </div>
      {item.name === "ImageView" && !item.running && <ToolAttachment item={item} />}
      {expanded && <ToolCard item={item} />}
    </div>
  );
}

function ToolCard({ item }: { item: ToolItem }) {
  const a = item.args;
  const parts: ReactNode[] = [];
  if (item.name === "Exec") parts.push(<RawHtml key="c" html={codeBlock(String(a.command ?? ""), "bash")} />);
  else if (item.name === "Write") parts.push(<RawHtml key="c" html={codeBlock(String(a.content ?? "").slice(0, 4000), langOf(String(a.path ?? "")))} />);
  else if (item.name === "Edit") {
    const before = String(a.old_string ?? a.old ?? "");
    const after = String(a.new_string ?? a.new ?? "");
    const patch = [...before.split("\n").map((l) => `-${l}`), ...after.split("\n").map((l) => `+${l}`)].join("\n");
    // The same head as the written file and the command beside it — a label and a copy button —
    // so the three expanded steps read as one kind of block; the copy takes the change as a patch.
    parts.push(
      <div key="c" className="codecard diffcard">
        <div className="codehead">
          <span>diff</span>
          <button className="copy" data-copy="1" data-copy-text={patch} type="button" aria-label={t("common.copy")} title={t("common.copy")}>
            <span className="glyph-copy"><Icon name="copy" size={14} /></span>
            <span className="glyph-done"><Icon name="check" size={14} /></span>
          </button>
        </div>
        <div className="diff">
          <pre className="del">{before}</pre>
          <pre className="add">{after}</pre>
        </div>
      </div>,
    );
  }
  else parts.push(<RawHtml key="c" html={codeBlock(JSON.stringify(a, null, 2), "args")} />);
  if (item.result !== undefined) parts.push(<ToolResultText key="r" item={item} />);
  return <div className="toolcard">{parts}</div>;
}

type Verification = { id: number; criterion: string; command: string; exit_code: number; passed: number; output_head: string; duration_ms: number; at: string; sandboxed: number; dependencies: string; tests_run: number | null };

/** A cited Verify receipt: what was claimed, the command that checked it, and how it ended. */
function ReceiptDialog({ sessionId, receipt, onClose }: { sessionId: string; receipt: string; onClose: () => void }) {
  const [row, setRow] = useState<Verification | null | undefined>(undefined);
  useEffect(() => {
    let gone = false;
    api
      .get<Verification[]>(`/api/sessions/${sessionId}/verifications`)
      .then((rows) => !gone && setRow(rows.find((r) => `v${r.id}` === receipt) ?? null))
      .catch(() => !gone && setRow(null));
    return () => {
      gone = true;
    };
  }, [sessionId, receipt]);
  return (
    <Overlay>
      <div className="sheet-backdrop" onClick={onClose}>
        <div className="sheet" onClick={(e) => e.stopPropagation()} role="dialog" aria-label={t("session.receipt", { id: receipt })}>
          <div className="grip" />
          <h3>{t("session.receipt", { id: receipt })}</h3>
          <div className="sheet-body">
            {row === undefined && <div className="empty">{t("common.loading")}</div>}
            {row === null && <div className="empty">{t("session.receipt.none", { id: receipt })}</div>}
            {row && (
              <>
                <p className="receipt-claim">
                  <b>{row.passed ? `✅ ${t("session.receipt.ok")}` : `❌ ${t("session.receipt.fail")}`}</b> — {row.criterion}
                </p>
                <div className="sub">
                  {t("session.receipt.meta", { code: row.exit_code, secs: (row.duration_ms / 1000).toFixed(1), when: timeAgo(row.at) })}
                  {row.tests_run !== null && t("session.receipt.tests", { n: row.tests_run })}
                  {row.sandboxed ? t("session.receipt.sandboxed") : ""}
                  {row.dependencies && t("session.receipt.depends", { list: row.dependencies })}
                </div>
                <RawHtml html={codeBlock(row.command, "sh")} />
                {row.output_head && (looksLikeDiff(row.output_head) ? <DiffView text={row.output_head} /> : <pre className="filetext">{row.output_head}</pre>)}
              </>
            )}
          </div>
        </div>
      </div>
    </Overlay>
  );
}

/** `link` is the address this chat lives at, which a link to one of its messages starts from. */
const NO_FOLDERS: SessionFolder[] = [];

/** `asking`: the key of the questions the session waits on (empty: none), so the transcript's AskUser
 *  call that asked them reads as waiting and an older unanswered one does not. */
/** `compose`: put text into the conversation's composer for the operator to send (a widget's ask);
 *  absent where the transcript has no composer of its own. */
const SessionContext = createContext<{ id: string; workspace: string; folders: SessionFolder[]; preview: (src: PreviewSource) => void; openJobs: () => void; toast: (text: string) => void; link: string; asking: string; compose?: (text: string) => boolean }>({
  id: "",
  workspace: "",
  folders: NO_FOLDERS,
  preview: () => undefined,
  openJobs: () => undefined,
  toast: () => undefined,
  link: "",
  asking: "",
});

/** Opens one of the session's subagents beside it, by id or by the name its leader gave it; null opens
 *  the list. Absent (null) where the transcript is not a leader's own screen. */
const SubagentOpener = createContext<((ref: { id?: string; name?: string } | null) => void) | null>(null);

/** Whether the steps of a turn are drawn open and stay open when the run ends: a helper's conversation
 *  beside its leader is read for what it did, where the leader's own chat folds the work away. */
const StepsOpen = createContext(false);

/** A tool's path as the workspace knows it: absolute paths inside the workspace become relative, others stay unreachable. */
function workspaceRelative(path: string, workspace: string): string | null {
  if (!path) return null;
  if (!path.startsWith("/")) return path.replace(/^\.\//, "");
  const root = workspace.replace(/\/+$/, "");
  if (root && (path === root || path.startsWith(root + "/"))) return path.slice(root.length + 1);
  return null;
}

/** The image an ImageView looked at: shown under the step, opened in the centred viewer. */
function ToolAttachment({ item }: { item: ToolItem }) {
  const { id, workspace, folders, preview } = useContext(SessionContext);
  const src = id ? toolSource(item, id, workspace, folders) : null;
  if (!src) return null;
  const name = src.path.split("/").pop() ?? src.path;
  if (previewKind(name) === "image") {
    return (
      <div className="tool-attachment">
        <AuthImg src={src} alt={name} className="tool-image" onClick={() => preview(src)} />
      </div>
    );
  }
  return (
    <div className="tool-attachment">
      <button type="button" className="file-chip" onClick={() => preview(src)} title={t(canPreview(name) ? "preview.open" : "preview.download")}>
        <span aria-hidden>{fileGlyph(name)}</span> {name}
      </button>
    </div>
  );
}

/** Where a compaction stands, in words: writing, merging, which part of how many, or summarising. */
function compactionStage(c: Compacting): string {
  return c.stage === "writing"
    ? t("session.compacting.writing")
    : c.stage === "merging"
      ? t("session.compacting.merging")
      : c.parts_total > 1
        ? t("session.compacting.part", { n: Math.min(c.parts_done + 1, c.parts_total), total: c.parts_total })
        : t("session.compacting.summarising");
}

/** The summary written ahead of the trigger, as a quiet note beside the session's own state. */
function AheadStatus({ c }: { c: Compacting }) {
  return <span className="head-status compaction-ahead" role="status" title={t("session.compacting.ahead.hint")}>{t("session.compacting.ahead", { what: compactionStage(c) })}</span>;
}

/** The compaction in flight: which stage, how many parts are summarised, how long it has run. */
function CompactionBar({ c }: { c: Compacting }) {
  useClock(1000);
  const what = compactionStage(c);
  return (
    <div className="livebar head-status compacting" role="status" aria-live="polite">
      <Dot status="compacting" />
      <b>{t("session.compacting.bar")}</b>
      <span className="num">{duration(Date.now() - new Date(c.started_at).getTime())}</span>
      <span className="truncate">{t("session.compacting.messages", { n: c.messages, what })}</span>
    </div>
  );
}

function ToolResultText({ item }: { item: ToolItem }) {
  const { id: sessionId, toast } = useContext(SessionContext);
  const key = `${sessionId}:result:${item.id}`;
  return <ToolResultView sessionId={sessionId} item={item} toast={toast} initial={disclosed.get(key)?.full ?? null} keep={(full) => remember(key, { full })} />;
}

function langOf(path: string): string {
  const ext = path.split(".").pop()?.toLowerCase() ?? "";
  const map: Record<string, string> = { py: "python", ts: "typescript", tsx: "tsx", js: "javascript", md: "markdown", sh: "bash", json: "json", toml: "toml", yaml: "yaml", yml: "yaml", html: "html", css: "css" };
  return map[ext] ?? ext;
}

// ── files ─────────────────────────────────────────────────────────────────

/** A file tree under an API root: a session's workspace or a named workspace; uploads go to ``uploadUrl`` when given. */
export function Files({ base, uploadUrl, onPreview, toast }: { base: string; uploadUrl?: string; onPreview: (src: PreviewSource) => void; toast?: (t: string) => void }) {
  const [path, setPath] = useState("");
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [gen, setGen] = useState(0);
  const upload = useRef<HTMLInputElement>(null);
  useEffect(() => {
    setError(null);
    api
      .get(`${base}/files?path=${encodeURIComponent(path)}`)
      .then(setData)
      .catch((e) => {
        setData(null);
        setError(errorText(e));
      });
  }, [base, path, gen]);
  async function sendFiles(files: FileList | null) {
    if (!files?.length || !uploadUrl) return;
    setUploading(true);
    try {
      const form = new FormData();
      form.append("path", data?.kind === "dir" ? path : path.split("/").slice(0, -1).join("/"));
      for (const f of Array.from(files)) form.append("files", f, f.name);
      const res = await fetch(uploadUrl, { method: "POST", headers: api.authHeaders(), body: form });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail ?? res.statusText);
      const r = (await res.json()) as { files: string[] };
      toast?.(plural("session.files.added", r.files.length));
      setGen((g) => g + 1);
    } catch (e) {
      toast?.(errorText(e));
    } finally {
      setUploading(false);
      if (upload.current) upload.current.value = "";
    }
  }
  if (error) return <div className="empty">{t("session.files.error", { what: path || t("session.files.theworkspace"), error })}</div>;
  if (!data) return <div className="empty">{t("common.loading")}</div>;
  const crumbs = path ? path.split("/") : [];
  const all: any[] = data.kind === "dir" ? data.entries : [];
  const entries = all.filter((e) => showHidden || !e.name.startsWith("."));
  const hidden = all.length - all.filter((e) => !e.name.startsWith(".")).length;
  const download = downloadHref(base, path);
  return (
    <div className="files">
      <div className="crumbs">
        <button className="crumb" onClick={() => setPath("")}>{t("session.files.crumb")}</button>
        {crumbs.map((c, i) => (
          <span key={i}>
            <span className="sub"> / </span>
            <button className="crumb" onClick={() => setPath(crumbs.slice(0, i + 1).join("/"))}>{c}</button>
          </span>
        ))}
        {data.kind !== "dir" && (
          <a className="btn small" style={{ marginLeft: "auto" }} href={download} target="_blank" rel="noreferrer">
            {t("preview.download")}{data.size ? ` · ${fmtBytes(data.size)}` : ""}
          </a>
        )}
        {data.kind === "dir" && uploadUrl && (
          <>
            <input ref={upload} type="file" multiple hidden onChange={(e) => sendFiles(e.target.files)} />
            <button className="btn small" style={{ marginLeft: "auto" }} disabled={uploading} onClick={() => upload.current?.click()} title={t("session.files.upload.title")}>
              <Icon name="up" size={14} /> {t(uploading ? "session.files.uploading" : "session.files.upload")}
            </button>
          </>
        )}
      </div>
      {data.kind === "dir" && entries.length === 0 && <div className="empty">{t("session.files.empty")}</div>}
      {data.kind === "dir" &&
        entries.map((e: any) => (
          <button
            key={e.name}
            className="card pressable row filerow"
            onClick={() => {
              const next = path ? `${path}/${e.name}` : e.name;
              // Files with a preview open in the dialog; folders and the rest are walked into as before.
              if (!e.dir && canPreview(e.name)) onPreview({ base, path: next });
              else setPath(next);
            }}
          >
            <span aria-hidden>{fileGlyph(e.name, e.dir)}</span>
            <div className="grow title">{e.name}</div>
            {!e.dir && <span className="sub">{fmtBytes(e.size)}</span>}
            {e.mtime && <span className="sub">{shortDateTime(e.mtime * 1000)}</span>}
          </button>
        ))}
      {data.kind === "dir" && hidden > 0 && (
        <button className="btn small" onClick={() => setShowHidden((h) => !h)}>
          {plural(showHidden ? "session.files.hidden.hide" : "session.files.hidden", hidden)}
        </button>
      )}
      {data.kind === "file" && data.truncated && <div className="sub" style={{ margin: "6px 0" }}>{t("session.files.truncated")}</div>}
      {data.kind !== "dir" && canPreview(path) && (
        <button className="btn small" style={{ marginBottom: 8 }} onClick={() => onPreview({ base, path })}><Icon name="eye" size={14} /> {t("preview.open")}</button>
      )}
      {data.kind === "file" && <pre className="filetext">{data.content}</pre>}
      {data.kind === "binary" && previewKind(path) === "image" && <AuthImg className="preview" src={{ base, path }} alt={path} onClick={() => onPreview({ base, path })} />}
      {data.kind === "binary" && previewKind(path) !== "image" && <div className="empty">{t("session.files.binary", { size: fmtBytes(data.size) })}</div>}
    </div>
  );
}
