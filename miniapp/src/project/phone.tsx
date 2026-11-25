// A project on a phone: four tabs and a More at the foot of the window instead of the app's own (the
// orchestrator, the board, the team, the terminals, and a sheet that holds the project's other pages),
// a header with the project and how its work goes, and a card with the request that has waited longest
// for the operator, answered with one tap and no need to open a conversation or a terminal.
// `phoneTab` in focus.ts decides where the bar shows; App.tsx draws it. The bar is the shared Tabs, the
// sheet the shared Sheet and its rows, the team's rows the shared Row.
//
// Everything here is sized for a thumb: rows and answers take the touch row height, and a field is
// 16 px so Safari does not zoom into it.

import { FormEvent, ReactNode, useId, useMemo, useState } from "react";
import { api, ApiError, type Ask, type StaffSessionView, type TerminalEnv, type TerminalEnvName, type TerminalView as TerminalRow } from "../api";
import { Skeleton } from "../ui/components";
import { toast } from "../ui/dialogs";
import { useEvent, useStreamUp } from "../events";
import { plural, t } from "../i18n";
import { EnvPill } from "../envpill";
import { Icon, type IconName } from "../icons";
import { PageHeader } from "../shell";
import { ORCHESTRATION_LIST, navigate, useRoute, projectHome, projectPagePath } from "../router";
import { alwaysServer, answeredBy, canAlways, composerWhen, nowChoice } from "../staff/model";
import { invalidate, useOffline, useQuery } from "../store";
import { PhoneTerminal, type PhoneTerminalProps } from "../terminal/mobile";
import { TerminalCard } from "../terminal/phonecard";
import { type PickedPlace, TerminalPicker } from "../terminal/picker";
import { PhoneTeamList } from "../team/phone";
import { StaffSheet } from "../team/StaffSheet";
import { ProjectSettingsSheet } from "../projects";
import { Setups } from "../team/setups";
import type { Staff, Team } from "../team/team";
import type { ProjectBoardData } from "../board/board";
import { errorText } from "../ui";
import { boardKey, staffKey, terminalsKey, useProject, useUsage } from "./data";
import { oldestOpen, PHONE_MORE, PHONE_TABS, type PhoneMorePage, type PhoneTab, teamCounts } from "./focus";
import { useMember } from "./staff";
import { budgetCompact, useGoalBudget } from "./ProjectBudget";
import { budgetAttention, distinctOperatorActions, operatorAttentionCount, type NextAction } from "./attention-model";
import { NeedsYouCard } from "./needs";
import { operatorReviewReady } from "./focus";
import { absDate } from "../format";
import { Badge, IconButton, MenuRow, Sheet, Tabs, type TabItem } from "../ui/primitives";
import { useReveal } from "../reveal";

const enc = encodeURIComponent;
const operatorAsksKey = (projectId: string) => `/api/asks?project=${enc(projectId)}&routed_to=operator`;

/** The team and the board under the keys focus mode reads them by, so the caches are shared. Only
 *  these two: a phone's project page has no column to fill with the terminals and the wake-ups. */
function useTeamAndBoard(projectId: string): { team: Team | null; board: ProjectBoardData | null } {
  const live = useStreamUp();
  const team = useQuery<Team>(staffKey(projectId), { pollMs: live ? 60000 : 15000, staleMs: 5000 });
  const board = useQuery<ProjectBoardData>(boardKey(projectId), { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  return {
    team: team.data && Array.isArray(team.data.staff) ? team.data : null,
    board: board.data && Array.isArray(board.data.tasks) ? board.data : null,
  };
}

const TAB_ICONS: Record<PhoneTab, IconName> = { orchestrator: "conductor", board: "board", team: "bots", terminals: "terminal" };
const MORE_ICONS: Record<PhoneMorePage, IconName> = { attention: "alert", journal: "journal", brief: "pen", wakeups: "clock", folders: "folder" };
// The decisions and the history keep the names the desktop column gives them; the rest are named as
// their pages are.
const MORE_LABELS: Record<PhoneMorePage, string> = { attention: "focus.nav.attention", journal: "focus.nav.journal", brief: "focus.page.brief", wakeups: "focus.page.wakeups", folders: "focus.page.folders" };

function tabPath(projectId: string, tab: PhoneTab): string {
  return tab === "orchestrator" ? projectHome(projectId) : projectPagePath(projectId, tab);
}

/** The operator's open requests in a project, kept current by their events. */
export function useOperatorAsks(projectId: string) {
  const live = useStreamUp();
  const query = useQuery<{ asks: Ask[] }>(operatorAsksKey(projectId), { pollMs: live ? 60000 : 10000, staleMs: 2000 });
  useEvent(["ask.", "permission.", "staff.status"], (event) => {
    if (!event.project_id || event.project_id === projectId) invalidate(`/api/asks?project=${enc(projectId)}`);
  }, [projectId]);
  const asks = Array.isArray(query.data?.asks) ? [...new Map(query.data!.asks.map((ask) => [ask.id, ask])).values()] : [];
  const open = useMemo(() => oldestOpen(asks), [asks]);
  return { ...open, openAsks: asks.filter((ask) => ask.routed_to === "operator" && !ask.resolved_at),
    unverified: !!query.error || !query.data };
}

// ── the tab bar ──────────────────────────────────────────────────────────────────────────────

/** The project's tabs, in the place of the app's, and "More" for its other pages: the shared Tabs, laid
 *  out as a bar at the foot of the window. The team's tab carries how many requests wait; More carries
 *  how many decisions wait, because the page that lists them sits in its sheet, and a count nobody can
 *  see from the bar is a decision nobody makes. */
export function ProjectTabs({ projectId, current }: { projectId: string; current: PhoneTab | "more" | null }) {
  const { waiting, openAsks, unverified: asksUnverified } = useOperatorAsks(projectId);
  const live = useStreamUp();
  const offline = useOffline();
  const [moreOpen, setMoreOpen] = useState(false);
  const board = useQuery<ProjectBoardData>(boardKey(projectId), { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  const next = useQuery<{ actions: NextAction[] }>(`/api/projects/${enc(projectId)}/next-actions`, { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  const budget = useGoalBudget(projectId);
  const countKnown = !offline && !asksUnverified && !!board.data && !board.error
    && !!next.data && !next.error && !!budget.data && !budget.error;
  const decisions = operatorAttentionCount(openAsks, board.data?.tasks ?? [], next.data?.actions ?? [], budget.data);
  const tasks = board.data?.tasks ?? [];
  const parts = { asks: openAsks.length, review: tasks.filter(operatorReviewReady).length,
    blocked: distinctOperatorActions(next.data?.actions ?? [], openAsks, tasks).length + (budgetAttention(budget.data) ? 1 : 0) };
  // A count that cannot be confirmed shows as "?" rather than vanishing: no badge would read as
  // "nothing waits", which is the one thing it cannot say.
  const badge = countKnown ? (decisions > 0 ? (decisions > 99 ? "99+" : String(decisions)) : null) : "?";
  const unknown = countKnown ? undefined : t("focus.attention.countUnknown");
  const items: TabItem[] = [
    ...PHONE_TABS.map((tab) => ({
      id: tab, label: t(`phone.tab.${tab}`), icon: TAB_ICONS[tab],
      count: tab === "team" && waiting > 0 ? <Badge tone="attention">{waiting > 99 ? "99+" : waiting}</Badge> : undefined,
    })),
    { id: "more", label: t("nav.more"), icon: "more" as const, expanded: moreOpen, onPress: () => setMoreOpen((open) => !open),
      count: badge ? <Badge tone="attention"><span title={unknown} aria-label={unknown ?? plural("phone.more.decisions", decisions)}>{badge}</span></Badge> : undefined },
  ];
  return (
    <>
      <nav className="project-tabs" aria-label={t("phone.tabs")}>
        <Tabs variant="page" label={t("phone.tabs")} tabs={items} value={moreOpen || current === "more" ? "more" : current ?? ""}
          onChange={(id) => { if (id !== "more") navigate(tabPath(projectId, id as PhoneTab)); }} />
      </nav>
      {moreOpen && <ProjectMoreSheet projectId={projectId} current={current === "more"} badge={badge} unknown={unknown} parts={parts} onClose={() => setMoreOpen(false)} />}
    </>
  );
}

/** What the More sheet says under "Needs decision": how many to answer, to review and blocked. */
type DecisionParts = { asks: number; review: number; blocked: number };

const MORE_HINTS: Partial<Record<PhoneMorePage, string>> = { journal: "pmore.journal.hint" };

/** The project's other pages on a phone: the project's name and where it runs at the top, a row for
 *  each page with what waits there, its settings, and the way back to every project after a divider. */
function ProjectMoreSheet({ projectId, current, badge, unknown, parts, onClose }: { projectId: string; current: boolean; badge: string | null; unknown?: string; parts: DecisionParts; onClose: () => void }) {
  const { project } = useProject(projectId);
  const route = useRoute();
  const [settings, setSettings] = useState(false);
  const revealer = useReveal();
  const here = current ? route.page : null;
  if (settings && project) return <ProjectSettingsSheet project={project} onClose={onClose} onRemoved={() => { onClose(); navigate(ORCHESTRATION_LIST); }} toast={toast} />;
  const goTo = (href: string) => { onClose(); navigate(href); };
  const env: TerminalEnvName = project?.settings.default_env ?? project?.folders[0]?.env ?? "container";
  const decisionHint = [parts.asks ? plural("pmore.asks", parts.asks) : "", parts.review ? plural("pmore.review", parts.review) : "", parts.blocked ? plural("pmore.blocked", parts.blocked) : ""].filter(Boolean).join(" · ");
  const hint = (page: PhoneMorePage) => page === "attention" ? (decisionHint || undefined) : MORE_HINTS[page] ? t(MORE_HINTS[page]!) : undefined;
  const value = (page: PhoneMorePage) => page === "attention" && badge ? <Badge tone="attention"><span title={unknown} aria-label={unknown ?? plural("phone.more.decisions", parts.asks + parts.review + parts.blocked)}>{badge}</span></Badge>
    : page === "folders" && project ? project.folders.length : undefined;
  return (
    <Sheet onClose={onClose} className="ph-sheet more-sheet" ariaLabel={t("nav.more")} title={t("nav.more")}
      preview={project ? { title: project.name, meta: [t(`term.env.${env}`), t("pmore.created", { date: absDate(project.created_at) })].join(" · ") } : undefined}>
      <div className="menu in-sheet" role="menu" aria-label={t("nav.more")}>
        {PHONE_MORE.map((page) => (
          <MenuRow key={page} icon={MORE_ICONS[page]} label={t(MORE_LABELS[page])} sub={hint(page)} aside={value(page)} checked={here === page ? true : undefined}
            data={{ more: page }} onSelect={() => goTo(projectPagePath(projectId, page))} />
        ))}
        <MenuRow icon="settings" label={t("phone.more.settings")} sub={t("pmore.settings.hint")} disabled={!project} data={{ more: "settings" }} onSelect={() => setSettings(true)} />
        {revealer && project && <MenuRow icon="external" label={revealer.label} data={{ more: "reveal" }} onSelect={() => { onClose(); void revealer.reveal({ project_id: project.id }); }} />}
        <div className="menu-sep" role="separator" />
        <MenuRow icon="grid" label={t("focus.all")} data={{ more: "projects" }} onSelect={() => goTo(ORCHESTRATION_LIST)} />
      </div>
    </Sheet>
  );
}

// ── the header ───────────────────────────────────────────────────────────────────────────────

/** The project's header on a phone: the hamburger to the app's drawer (Inbox, Agents, Settings and
 *  every project are there, so nothing outside the project is out of reach from inside it), its name
 *  and how the work goes, and the environment it runs in. Its other pages are in the bar's "More"
 *  sheet, not in a menu here: two menus listing the same pages left the operator guessing which one
 *  to open. All projects stays in that sheet as well. */
export function ProjectPhoneHead({ projectId, title, subtitle, actions }: { projectId: string; title?: string; subtitle?: string; actions?: ReactNode }) {
  const { project } = useProject(projectId);
  const { data: budget } = useGoalBudget(projectId);
  const remaining = budgetCompact(budget);
  const { team, board } = useTeamAndBoard(projectId);
  const counts = teamCounts(team?.staff ?? [], board?.tasks ?? []);
  const env: TerminalEnvName = project?.settings.default_env ?? project?.folders[0]?.env ?? "container";
  const line = [plural("phone.working", counts.working), counts.review ? plural("phone.review", counts.review) : ""].filter(Boolean).join(" · ");
  return (
    <PageHeader
      title={title ?? project?.name ?? "…"}
      subtitle={subtitle ?? (team ? line : undefined)}
      actions={
        <>
          {actions}
          {remaining && <span className="chip tiny project-budget-chip" title={`${remaining} · ${t("budget.summaryHint")}`}>{remaining}</span>}
          {project && <EnvPill env={env} tiny />}
        </>
      }
    />
  );
}

// ── the banner ───────────────────────────────────────────────────────────────────────────────

/**
 * The answers to one request, each a thumb-sized button: its options, or allow and deny, and
 * "Answer…" for words of the operator's own. The first answer to reach the host wins; one that lost
 * to an answer given elsewhere (Telegram, the desktop) says so, and the request leaves either way.
 */
/**
 * `always` offers "Always" beside Allow, for a command-line member whose CLI asks permissions (the
 * staff view decides it from the capability table); `server`, the MCP server of the asked tool, adds
 * "Always, all <server> tools" beside it (`alwaysServer`). A refusal because someone answered first
 * names who did.
 */
export function AskAnswers({ ask, projectId, toast, always = false, server = "", unverified = false }: { ask: Ask; projectId: string; toast: (text: string) => void; always?: boolean; server?: string; unverified?: boolean }) {
  const offline = useOffline();
  const blocked = offline || unverified;
  const offlineId = useId();
  const [writing, setWriting] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const options = Array.isArray(ask.detail?.options) ? ask.detail.options.filter((o): o is string => typeof o === "string") : [];
  async function send(body: { selected?: string[]; text?: string; allow?: boolean; always?: boolean; server?: boolean }) {
    if (busy || blocked) return;
    setBusy(true);
    try {
      await api.post(`/api/asks/${enc(ask.id)}/answer`, body);
      setWriting(false);
      setText("");
    } catch (e) {
      const who = e instanceof ApiError && e.status === 409 ? answeredBy(e.message) : "";
      toast(e instanceof ApiError && e.status === 409 ? (who ? t("perm.conflict.by", { who: t(`perm.by.${who}`) }) : t("focus.ask.conflict")) : errorText(e));
    } finally {
      setBusy(false);
      invalidate(`/api/asks?project=${enc(projectId)}`);
      invalidate(`/api/projects/${enc(projectId)}/board`);
      invalidate(`/api/projects/${enc(projectId)}/staff`);
      // An "Always" may have left the member a standing grant, which the staff view lists.
      if (body.always && ask.staff_id) invalidate(`/api/staff/${enc(ask.staff_id)}/session`);
    }
  }
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const words = text.trim();
    if (ask.kind === "permission") void send({ allow: false, text: words });
    else if (words) void send({ text: words });
  };
  const permission = ask.kind === "permission" || ask.kind === "folder";
  return (
    <div className="ask-answers">
      {blocked && <div id={offlineId} className="focus-attention-warning" role="status">{t(offline ? "focus.attention.offline" : "focus.attention.stale")}</div>}
      {!writing && (
        <div className="ask-answers-row">
          {permission ? (
            <>
              <button className="btn primary" disabled={busy || blocked} aria-describedby={blocked ? offlineId : undefined} onClick={() => void send({ allow: true })}>{t(ask.kind === "folder" ? "focus.ask.yes" : "phone.ask.allow")}</button>
              {always && ask.kind === "permission" && <button className="btn" disabled={busy || blocked} aria-describedby={blocked ? offlineId : undefined} data-answer="always" onClick={() => void send({ allow: true, always: true })}>{t("perm.always")}</button>}
              {always && server && ask.kind === "permission" && <button className="btn" disabled={busy || blocked} aria-describedby={blocked ? offlineId : undefined} data-answer="always-server" onClick={() => void send({ allow: true, always: true, server: true })}>{t("perm.always.server", { server })}</button>}
              <button className="btn" disabled={busy || blocked} aria-describedby={blocked ? offlineId : undefined} onClick={() => void send({ allow: false })}>{t(ask.kind === "folder" ? "focus.ask.no" : "phone.ask.deny")}</button>
            </>
          ) : (
            options.map((option, i) => (
              <button key={option} className={`btn ${i === 0 ? "primary" : ""}`} disabled={busy || blocked} aria-describedby={blocked ? offlineId : undefined} onClick={() => void send({ selected: [option] })}>{option}</button>
            ))
          )}
          {ask.kind !== "folder" && (
            // A pen and a dashed frame: as a bare grey word under two solid buttons it read as a
            // caption, and nobody found the way to type an answer of their own.
            <button className="btn ask-answers-write" disabled={busy || blocked} aria-describedby={blocked ? offlineId : undefined} onClick={() => setWriting(true)}><Icon name="pen" size={14} />{t(ask.kind === "permission" ? "phone.ask.denyWhy" : "phone.ask.write")}</button>
          )}
        </div>
      )}
      {writing && (
        <form className="ask-answers-own" onSubmit={submit}>
          <input
            className="field"
            autoFocus
            value={text}
            maxLength={4000}
            placeholder={t(ask.kind === "permission" ? "phone.ask.why" : "focus.ask.placeholder")}
            aria-label={t(ask.kind === "permission" ? "phone.ask.why" : "focus.ask.placeholder")}
            enterKeyHint="send"
            onChange={(e) => setText(e.target.value)}
          />
          <button className="btn primary" type="submit" disabled={busy || blocked || (ask.kind !== "permission" && !text.trim())} aria-describedby={blocked ? offlineId : undefined}>{t("focus.ask.send")}</button>
          <button className="iconbtn" type="button" onClick={() => setWriting(false)} aria-label={t("common.cancel")} title={t("common.cancel")}><Icon name="close" /></button>
        </form>
      )}
    </div>
  );
}

/** The request that has waited longest for the operator, at the top of a tab, answered in place: the
 *  compact card of project/needs.tsx, whose "+N more" opens the one decision sheet. */
export function NeedsYouBanner({ projectId, toast }: { projectId: string; toast: (text: string) => void }) {
  const asks = useOperatorAsks(projectId);
  return <NeedsYouCard projectId={projectId} asks={asks} toast={toast} />;
}

// ── the team ─────────────────────────────────────────────────────────────────────────────────

export function PhoneTeam({ projectId, toast }: { projectId: string; toast: (text: string) => void }) {
  const { team, board } = useTeamAndBoard(projectId);
  const usage = useUsage(projectId);
  const [hiring, setHiring] = useState(false);
  const [editing, setEditing] = useState<Staff | null>(null);
  const tasks = new Map((board?.tasks ?? []).map((task) => [task.id, task.title]));
  const reload = () => invalidate(`/api/projects/${enc(projectId)}/staff`);
  return (
    <>
      <ProjectPhoneHead
        projectId={projectId}
        actions={team && !team.project.ephemeral ? <IconButton icon="plus" label={t("team.hire")} onClick={() => setHiring(true)} /> : undefined}
      />
      <div className="screen phone-project ph-team">
        <NeedsYouBanner projectId={projectId} toast={toast} />
        {!team && <Skeleton rows={4} />}
        {/* The rows, their long-press commands and the grouping of a big team live in team/phone.tsx. */}
        {team && <PhoneTeamList projectId={projectId} team={team} tasks={tasks} usage={usage} toast={toast} onEdit={setEditing} onHire={() => setHiring(true)}
          empty={!team.project.ephemeral && !team.project.system ? <Setups team={team} toast={toast} onDone={reload} /> : undefined} />}
      </div>
      {hiring && team && <StaffSheet team={team} onClose={() => setHiring(false)} onDone={reload} toast={toast} />}
      {editing && team && <StaffSheet team={team} member={editing} onClose={() => setEditing(null)} onDone={reload} toast={toast} />}
    </>
  );
}

// ── the terminals ────────────────────────────────────────────────────────────────────────────

export function PhoneTerminals({ projectId, toast }: { projectId: string; toast: (text: string) => void }) {
  const offline = useOffline();
  const { project } = useProject(projectId);
  const { data } = useQuery<{ terminals: TerminalRow[]; envs?: TerminalEnv[] }>(terminalsKey(projectId), { pollMs: 10000, staleMs: 3000 });
  const rows = Array.isArray(data?.terminals) ? data!.terminals : [];
  const [picking, setPicking] = useState(false);
  // A new terminal asks where, as every "+" does; the quick places are the project's own folders in
  // each environment (the host's falls back to the operator's home).
  const folderIn = (env: TerminalEnvName) => project?.folders.find((f) => f.env === env && !f.readonly) ?? project?.folders.find((f) => f.env === env);
  const suggested: TerminalEnvName = project?.settings.default_env ?? project?.folders[0]?.env ?? "container";
  async function make(picked: PickedPlace) {
    const { openFreeTerminal } = await import("../screens/Terminals");
    await openFreeTerminal(picked.env, picked.cwd, projectId, toast, picked.sandbox);
    invalidate(terminalsKey(projectId));
  }
  return (
    <>
      <ProjectPhoneHead
        projectId={projectId}
        title={t("focus.page.terminals")}
        subtitle={project?.name}
        actions={<IconButton icon="plus" label={t("term.new")} disabled={!project || offline} onClick={() => setPicking(true)} />}
      />
      <div className="screen phone-project">
        {offline && <div className="result-warning" role="status">{t("focus.terminalOffline")}</div>}
        <NeedsYouBanner projectId={projectId} toast={toast} />
        {!data && <Skeleton rows={2} />}
        {data && rows.length === 0 && <div className="empty calm">{t("focus.terminals.empty")}</div>}
        {/* The cards of the Terminals screen, so a terminal looks and answers the same in both places. */}
        <div className="ph-tcards" role="list">
          {rows.map((row) => <div key={row.id} role="listitem"><TerminalCard row={row} projectName={project?.name ?? null} toast={toast} /></div>)}
        </div>
      </div>
      {picking && (
        <TerminalPicker
          envs={data?.envs ?? []}
          folders={{ container: { path: folderIn("container")?.path ?? null, projectId }, host: { path: folderIn("host")?.path ?? data?.envs?.find((e) => e.env === "host")?.home ?? null } }}
          projects={project ? [project] : []}
          lens={project ?? null}
          suggested={suggested}
          anchor={null}
          phone
          onPick={make}
          onClose={() => setPicking(false)}
        />
      )}
    </>
  );
}

// ── a staff member's terminal ────────────────────────────────────────────────────────────────

/**
 * A command-line member's terminal on a phone (M3): what the operator writes goes to the member as a
 * message through the team's delivery, with a receipt, instead of being typed into its program; and
 * the member's open request is answered from the buttons above the keys.
 */
export function StaffPhoneTerminal({ staffId, projectId, ...props }: PhoneTerminalProps & { staffId: string; projectId: string | null }) {
  const offline = useOffline();
  const { data: member } = useMember(staffId);
  const pid = projectId ?? member?.project_id ?? "";
  const { data } = useQuery<{ asks: Ask[] }>(pid ? operatorAsksKey(pid) : null, { pollMs: 10000, staleMs: 2000 });
  useEvent(["ask.", "permission."], (event) => {
    if (pid && event.project_id === pid) invalidate(`/api/asks?project=${enc(pid)}`);
  }, [pid]);
  const mine = oldestOpen((data?.asks ?? []).filter((a) => a.staff_id === staffId)).ask;
  const { data: view } = useQuery<StaffSessionView>(`/api/staff/${enc(staffId)}/session`, { staleMs: 10000 });
  const name = member?.name ?? "";
  const compose = {
    placeholder: name ? t("term.phone.compose.staff", { name }) : t("term.phone.compose"),
    onSend: async (text: string) => {
      if (offline) { toast(t("focus.staffDraftOffline")); return false; }
      try {
        // The same choice the staff view's composer starts on, with no picker here to change it.
        await api.post(`/api/staff/${enc(staffId)}/messages`, { text, when: composerWhen(nowChoice(view?.capabilities), null) });
        invalidate(`/api/staff/${enc(staffId)}/messages`);
        return true;
      } catch (e) {
        toast(errorText(e));
        return false;
      }
    },
  };
  const askActions = mine && pid ? (
    <div className="term-phone-ask" data-ask={mine.short_id}>
      <div className="term-phone-ask-text truncate">{mine.text}</div>
      <AskAnswers key={mine.id} ask={mine} projectId={pid} toast={toast} always={canAlways(mine, view?.capabilities)} server={alwaysServer(mine, view?.capabilities)} />
    </div>
  ) : null;
  const actions = offline ? <><div className="result-warning" role="status">{t("focus.staffDraftOffline")}</div>{askActions}</> : askActions;
  return <PhoneTerminal {...props} compose={compose} actions={actions} />;
}
