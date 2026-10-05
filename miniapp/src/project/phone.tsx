// A project on a phone: four tabs at the bottom instead of the app's own (the orchestrator, the team,
// the board, the terminals), a header with the project and how its work goes, and a banner with the
// request that has waited longest for the operator, answered with one tap and no need to open a
// conversation or a terminal. `phoneTab` in focus.ts decides where the bar shows; App.tsx draws it.
//
// Everything here is sized for a thumb: rows and answers take the touch row height, and a field is
// 16 px so Safari does not zoom into it.

import { FormEvent, ReactNode, useId, useMemo, useState } from "react";
import { api, ApiError, type Ask, type StaffSessionView, type TerminalEnvName, type TerminalView as TerminalRow } from "../api";
import { Skeleton } from "../ui/components";
import { MenuItem, OverflowMenu, toast } from "../ui/dialogs";
import { useEvent, useStreamUp } from "../events";
import { relTime } from "../format";
import { plural, t } from "../i18n";
import { EnvPill } from "../envpill";
import { Icon, type IconName } from "../icons";
import { go, PageHeader } from "../shell";
import { ORCHESTRATION_LIST, navigate, pathFor, projectHome, projectPagePath, projectSessionPath, projectStaffPath } from "../router";
import { alwaysServer, answeredBy, askWords, canAlways, composerWhen, nowChoice } from "../staff/model";
import { HealthLine } from "../staff/health";
import { invalidate, useOffline, useQuery } from "../store";
import { PhoneTerminal, type PhoneTerminalProps } from "../terminal/mobile";
import { TerminalRowMenu } from "../terminal/rowmenu";
import { HarnessBadge, StaffAvatar } from "../team/parts";
import { StaffSheet } from "../team/StaffSheet";
import type { Staff, Team } from "../team/team";
import type { ProjectBoardData } from "../board/board";
import { errorText } from "../ui";
import { boardKey, staffKey, terminalsKey, useProject, useUsage } from "./data";
import { firstWait, oldestOpen, PHONE_TABS, type PhoneTab, splitTeam, staffTone, teamCounts, waitKey } from "./focus";
import { useMember } from "./staff";
import { spendLine, staffUsage } from "./usage";
import { budgetCompact, useGoalBudget } from "./ProjectBudget";
import { operatorAttentionCount, type NextAction } from "./attention-model";

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

const TAB_ICONS: Record<PhoneTab, IconName> = { orchestrator: "conductor", attention: "alert", journal: "journal" };

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

/** The project's tabs, in the place of the app's. The team's tab carries how many requests wait. */
export function ProjectTabs({ projectId, current }: { projectId: string; current: PhoneTab | null }) {
  const { openAsks, unverified: asksUnverified } = useOperatorAsks(projectId);
  const live = useStreamUp();
  const offline = useOffline();
  const board = useQuery<ProjectBoardData>(boardKey(projectId), { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  const next = useQuery<{ actions: NextAction[] }>(`/api/projects/${enc(projectId)}/next-actions`, { pollMs: live ? 60000 : 15000, staleMs: 3000 });
  const budget = useGoalBudget(projectId);
  const countKnown = !offline && !asksUnverified && !!board.data && !board.error
    && !!next.data && !next.error && !!budget.data && !budget.error;
  const decisions = operatorAttentionCount(openAsks, board.data?.tasks ?? [], next.data?.actions ?? [], budget.data);
  return (
    <nav className="tabbar project-tabs" aria-label={t("phone.tabs")}>
      {PHONE_TABS.map((tab) => {
        const href = tabPath(projectId, tab);
        return (
          <a key={tab} href={href} data-tab={tab} className={current === tab ? "active" : ""} aria-current={current === tab ? "page" : undefined} onClick={(e) => go(e, href)}>
            <span className="glyph">
              <Icon name={TAB_ICONS[tab]} size={22} />
          {tab === "attention" && (countKnown ? decisions > 0 : true) && <span className="tab-badge"
            title={countKnown ? undefined : t("focus.attention.countUnknown")}
            aria-label={countKnown ? undefined : t("focus.attention.countUnknown")}>{countKnown ? (decisions > 99 ? "99+" : decisions) : "?"}</span>}
            </span>
            {t(`focus.nav.${tab}`)}
          </a>
        );
      })}
    </nav>
  );
}

// ── the header ───────────────────────────────────────────────────────────────────────────────

/** The project's header on a phone: back to orchestration's list of projects, its name and how the work goes, the
 *  environment it runs in, and its other pages behind a menu. */
export function ProjectPhoneHead({ projectId, title, subtitle, actions, extra }: { projectId: string; title?: string; subtitle?: string; actions?: ReactNode; extra?: MenuItem[] }) {
  const { project } = useProject(projectId);
  const { data: budget } = useGoalBudget(projectId);
  const remaining = budgetCompact(budget);
  const { team, board } = useTeamAndBoard(projectId);
  const counts = teamCounts(team?.staff ?? [], board?.tasks ?? []);
  const env: TerminalEnvName = project?.settings.default_env ?? project?.folders[0]?.env ?? "container";
  const line = [plural("phone.working", counts.working), counts.review ? plural("phone.review", counts.review) : ""].filter(Boolean).join(" · ");
  const pages: MenuItem[] = [
    ...(extra ?? []),
    ...(extra?.length ? ["-" as const] : []),
    { label: t("focus.page.brief"), icon: "pen", onSelect: () => navigate(projectPagePath(projectId, "brief")) },
    { label: t("focus.page.journal"), icon: "journal", onSelect: () => navigate(projectPagePath(projectId, "journal")) },
    { label: t("focus.page.wakeups"), icon: "clock", onSelect: () => navigate(projectPagePath(projectId, "wakeups")) },
    { label: t("focus.page.folders"), icon: "folder", onSelect: () => navigate(projectPagePath(projectId, "folders")) },
    { label: t("focus.page.team"), icon: "bots", onSelect: () => navigate(projectPagePath(projectId, "team")) },
    { label: t("focus.page.board"), icon: "board", onSelect: () => navigate(projectPagePath(projectId, "board")) },
    { label: t("focus.page.terminals"), icon: "terminal", onSelect: () => navigate(projectPagePath(projectId, "terminals")) },
  ];
  return (
    <PageHeader
      title={title ?? project?.name ?? "…"}
      subtitle={subtitle ?? (team ? line : undefined)}
      back={ORCHESTRATION_LIST}
      actions={
        <>
          {actions}
          {remaining && <span className="chip tiny project-budget-chip" title={`${remaining} · ${t("budget.summaryHint")}`}>{remaining}</span>}
          {project && <EnvPill env={env} tiny />}
          <OverflowMenu items={pages} label={t("phone.pages")} />
        </>
      }
    />
  );
}

// ── the banner ───────────────────────────────────────────────────────────────────────────────

/** Who is asking, in words: the member by name, or the orchestrator. */
function askerLine(ask: Ask, names: Map<string, string>): string {
  const name = ask.staff_id ? names.get(ask.staff_id) : undefined;
  if (!name) return t(ask.kind === "permission" ? "phone.ask.orchestrator.permission" : "phone.ask.orchestrator");
  return t(ask.kind === "permission" ? "phone.ask.permission" : ask.kind === "folder" ? "phone.ask.folder" : "phone.ask.question", { name });
}

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
      toast(t("focus.ask.sent"));
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

/** The request that has waited longest for the operator, at the top of a tab, answered in place. */
export function NeedsYouBanner({ projectId, toast }: { projectId: string; toast: (text: string) => void }) {
  const { ask, waiting, unverified } = useOperatorAsks(projectId);
  const { data: team } = useQuery<{ staff: Staff[] }>(staffKey(projectId), { staleMs: 5000 });
  if (!ask) return null;
  const names = new Map((team?.staff ?? []).map((m) => [m.id, m.name]));
  return (
    <section className="needs-banner" data-ask={ask.short_id} aria-label={t("phone.needs")}>
      <div className="needs-banner-head">
        <Icon name="alert" size={14} />
        <span className="grow">{t("phone.needs")}</span>
        {/* The age first, then the count behind a dot: "+1 more 4d" run together left it unclear whose
            age that was. The age is the shown question's. */}
        <span className="needs-banner-when">{relTime(ask.created_at)}</span>
        {waiting > 1 && <span className="needs-banner-more">· {t("phone.needs.more", { n: waiting - 1 })}</span>}
      </div>
      <div className="needs-banner-text"><b>{askerLine(ask, names)}</b> {askWords(ask)}</div>
      <AskAnswers key={ask.id} ask={ask} projectId={projectId} toast={toast} unverified={unverified} />
    </section>
  );
}

// ── the team ─────────────────────────────────────────────────────────────────────────────────

/** Where a member leads on a phone: into its conversation, a command-line member's view (its Feed,
 *  with its terminal a tap away), or its card to edit. */
function memberPath(projectId: string, member: Staff): string | null {
  if (member.live?.session_id) return projectSessionPath(projectId, member.live.session_id);
  if (member.harness !== "daedalus" && member.live) return projectStaffPath(projectId, member.id);
  return null;
}

export function PhoneTeam({ projectId, toast }: { projectId: string; toast: (text: string) => void }) {
  const { team, board } = useTeamAndBoard(projectId);
  const usage = useUsage(projectId);
  const [hiring, setHiring] = useState(false);
  const [editing, setEditing] = useState<Staff | null>(null);
  const { team: members, oneOff } = splitTeam(team?.staff ?? []);
  const tasks = new Map((board?.tasks ?? []).map((task) => [task.id, task.title]));
  const open = (member: Staff) => {
    const path = memberPath(projectId, member);
    if (path) navigate(path);
    else setEditing(member);
  };
  const reload = () => invalidate(`/api/projects/${enc(projectId)}/staff`);
  const row = (member: Staff) => (
    <PhoneStaffRow
      key={member.id}
      member={member}
      task={member.live?.task_id ? tasks.get(member.live.task_id) : undefined}
      spend={spendLine(staffUsage(usage, member.id))}
      onOpen={() => open(member)}
      onEdit={() => setEditing(member)}
    />
  );
  return (
    <>
      <ProjectPhoneHead
        projectId={projectId}
        actions={team && !team.project.ephemeral ? <button className="iconbtn" onClick={() => setHiring(true)} aria-label={t("team.hire")} title={t("team.hire")}><Icon name="plus" /></button> : undefined}
      />
      <div className="screen phone-project">
        <NeedsYouBanner projectId={projectId} toast={toast} />
        {!team && <Skeleton rows={4} />}
        {team && members.length === 0 && oneOff.length === 0 && (
          <div className="empty">
            <b>{t("team.empty")}</b>
            <div>{t("team.empty.sub")}</div>
          </div>
        )}
        {members.length > 0 && <div className="phone-staff" role="list">{members.map(row)}</div>}
        {oneOff.length > 0 && (
          <>
            <div className="section-title">{t("focus.oneoff")} <span className="n">{oneOff.length}</span></div>
            <div className="phone-staff" role="list">{oneOff.map(row)}</div>
          </>
        )}
      </div>
      {hiring && team && <StaffSheet team={team} onClose={() => setHiring(false)} onDone={reload} toast={toast} />}
      {editing && team && <StaffSheet team={team} member={editing} onClose={() => setEditing(null)} onDone={reload} toast={toast} />}
    </>
  );
}

/** A member as M8 draws it: who, what runs them and where, what they are on, what they spent today,
 *  and a dot for how it goes. */
function PhoneStaffRow({ member, task, spend, onOpen, onEdit }: { member: Staff; task?: string; spend: string | null; onOpen: () => void; onEdit: () => void }) {
  const tone = staffTone(member);
  const wait = firstWait(member);
  const line = wait
    ? t("focus.wait.line", { reason: t(waitKey(wait.reason)), n: wait.position })
    : [task, member.live?.waiting_for || t(`focus.tone.${tone}`)].filter(Boolean).join(" · ");
  // The row opens the member's work; editing the member is the small button beside it, so the
  // common tap goes where the work is and the rare one is still a thumb away.
  return (
    <div className="phone-staff-item" role="listitem" data-staff={member.id}>
    <button className="phone-staff-row" onClick={onOpen} title={wait?.detail || undefined}>
      <StaffAvatar name={member.name} color={member.color} />
      <span className="phone-staff-main">
        <span className="phone-staff-name">
          <span className="truncate">{member.name}</span>
          {!member.one_off && member.role && <span className="phone-staff-role truncate">{member.role}</span>}
          <HarnessBadge harness={member.harness} />
          {member.env === "host" && <EnvPill env="host" tiny />}
        </span>
        {/* Two lines before it gives up: a wait's reason is the one thing that says why the member is
            stuck, and one line cut "the machine's terminal limit is reached" before its verb. */}
        <span className={`phone-staff-line clamp-2 ${wait || tone === "waiting" ? "waits" : ""}`}>{line}</span>
        {/* The spend has a line of its own rather than joining the one above: that line truncates, and
            the money is the part the operator would lose on a narrow phone. */}
        {spend && <span className="phone-staff-line staff-spend truncate">{spend}</span>}
        <HealthLine health={member.health ?? null} compact />
      </span>
      <span className={`focus-dot tone-${tone}`} aria-label={t(`focus.tone.${tone}`)} role="img" />
    </button>
    <button className="iconbtn phone-staff-edit" onClick={onEdit} aria-label={t("team.edit.for", { name: member.name })} title={t("team.edit.for", { name: member.name })}>
      <Icon name="more" />
    </button>
    </div>
  );
}

// ── the board ────────────────────────────────────────────────────────────────────────────────

/** `board` is handed the request the banner shows, so the board does not show it a second time. */
export function PhoneBoard({ projectId, toast, board }: { projectId: string; toast: (text: string) => void; board: (bannered: string | null) => ReactNode }) {
  const { project } = useProject(projectId);
  const { ask } = useOperatorAsks(projectId);
  // The board's own bar under the header carries its counts and its "+": the header only names it.
  return (
    <>
      <ProjectPhoneHead projectId={projectId} title={project ? t("pboard.title.of", { name: project.name }) : t("focus.page.board")} subtitle="" />
      <div className="phone-project phone-board">
        <NeedsYouBanner projectId={projectId} toast={toast} />
        {board(ask?.id ?? null)}
      </div>
    </>
  );
}

// ── the terminals ────────────────────────────────────────────────────────────────────────────

/** A terminal's line on a phone: running, how long ago it spoke, or how it ended. */
function terminalLine(row: TerminalRow): string {
  if (row.status !== "running") return row.exit_signal ? t("term.state.exitedSignal", { signal: row.exit_signal }) : t("focus.terminal.code", { code: row.exit_code ?? "?" });
  return row.last_output_at ? t("phone.term.spoke", { when: relTime(row.last_output_at) }) : t("term.running");
}

export function PhoneTerminals({ projectId, toast }: { projectId: string; toast: (text: string) => void }) {
  const offline = useOffline();
  const { project } = useProject(projectId);
  const { data } = useQuery<{ terminals: TerminalRow[] }>(terminalsKey(projectId), { pollMs: 10000, staleMs: 3000 });
  const rows = Array.isArray(data?.terminals) ? data!.terminals : [];
  const [making, setMaking] = useState(false);
  // A new terminal opens in the project's first folder of its default environment; the Terminals
  // screen, one menu away, is where any other folder is chosen.
  const env: TerminalEnvName = project?.settings.default_env ?? project?.folders[0]?.env ?? "container";
  const folder = project?.folders.find((f) => f.env === env && !f.readonly) ?? project?.folders.find((f) => f.env === env);
  async function make() {
    if (offline) return;
    setMaking(true);
    try {
      const { openFreeTerminal } = await import("../screens/Terminals");
      await openFreeTerminal(env, folder?.path, projectId, toast);
      invalidate(terminalsKey(projectId));
    } finally {
      setMaking(false);
    }
  }
  return (
    <>
      <ProjectPhoneHead
        projectId={projectId}
        title={t("focus.page.terminals")}
        subtitle={project?.name}
        actions={<button className="iconbtn" disabled={making || !project || offline} onClick={() => void make()} aria-label={t("term.new")} title={t("term.new")}><Icon name="plus" /></button>}
      />
      <div className="screen phone-project">
        {offline && <div className="result-warning" role="status">{t("focus.terminalOffline")}</div>}
        <NeedsYouBanner projectId={projectId} toast={toast} />
        {!data && <Skeleton rows={2} />}
        {data && rows.length === 0 && <div className="empty calm">{t("focus.terminals.empty")}</div>}
        <div className="phone-terms" role="list">
          {rows.map((row) => (
            <div key={row.id} role="listitem" className="phone-term-row">
              <a className={`phone-term ${row.status === "running" ? "" : "ended"} ${row.env}`} href={pathFor("terminals", row.id)} onClick={(e) => go(e, pathFor("terminals", row.id))} data-terminal={row.id}>
                <Icon name={row.env === "host" ? "lock" : "terminal"} size={18} />
                <span className="phone-term-main">
                  <span className="phone-term-title truncate">{row.title || t("term.untitled")}</span>
                  <span className="phone-term-line truncate">{[row.owner.label, terminalLine(row)].filter(Boolean).join(" · ")}</span>
                </span>
                <EnvPill env={row.env} />
              </a>
              {!offline && <TerminalRowMenu row={row} toast={toast} />}
            </div>
          ))}
        </div>
      </div>
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
        toast(t("phone.told", { name: name || t("focus.team") }));
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
