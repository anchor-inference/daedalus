// A terminal as a card on a phone, where the Terminals screen and a project's Terminals tab list
// them: its title in the terminal's face, who it belongs to, the last rows of its screen and how it is
// doing. A tap opens it; its commands are behind a long press; a staff member's card that waits for
// permission answers it in a sheet, without opening the terminal.

import { useState } from "react";
import type { Ask, HarnessCapabilities, TerminalView as TerminalRow } from "../api";
import { EnvPill } from "../envpill";
import { relTime } from "../format";
import { t } from "../i18n";
import { navigate, pathFor } from "../router";
import { peek, useOffline, useQuery } from "../store";
import { colourVar, initials } from "../team/team";
import { alwaysServer, askWords, canAlways } from "../staff/model";
import { PhoneAskAnswers } from "../staff/phoneask";
import { ActionSheet, Banner, BottomSheet, RowSkeleton, useLongPress } from "../ui/phone";
import { cardStatus, ownerLine, previewRows, runStyle, running } from "./preview";
import { useTerminalCommands } from "./rowmenu";

/** One terminal as a card: its title in the terminal's face, who it belongs to, the last rows of its
 *  screen, and how it is doing. A card that waits on the operator is marked amber and answers here. */
export function TerminalCard({ row, projectName, toast }: { row: TerminalRow; projectName: string | null; toast: (text: string) => void }) {
  const offline = useOffline();
  const [menu, setMenu] = useState(false);
  const [answering, setAnswering] = useState(false);
  const status = cardStatus(row);
  const live = running(row);
  const action = live ? row.activity?.action : undefined;
  // A request the operator answers: the host names it, or says the terminal waits in amber.
  const asks = live && !!action && (action.kind === "answer" || row.activity?.level === "warn" || row.activity?.level === "bad");
  const { items, layer } = useTerminalCommands(row, toast);
  const open = () => navigate(pathFor("terminals", row.id));
  const press = useLongPress(() => setMenu(true));
  const lines = previewRows(row.preview, 4);
  const answer = () => (row.owner.kind === "staff" && row.owner.id && row.project_id ? setAnswering(true) : action && navigate(action.path));
  return (
    <article className={`ph-tcard ${asks ? "ask" : ""} ${live ? "" : "ended"}`} data-terminal={row.id}>
      <div className="ph-tcard-open" role="link" tabIndex={0} aria-label={t("term.open", { title: row.title || t("term.untitled") })}
        onClick={() => { if (press.fired.current) { press.fired.current = false; return; } open(); }}
        onKeyDown={(e) => {
          if (e.target !== e.currentTarget) return;
          if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
          else if (e.key === "ContextMenu" || (e.shiftKey && e.key === "F10")) { e.preventDefault(); setMenu(true); }
        }}
        {...press.handlers}>
        <span className="ph-tcard-hd">
          <span className="ph-tcard-nm">{row.title || t("term.untitled")}</span>
          <EnvPill env={row.env} />
        </span>
        <span className="ph-tcard-sb">{ownerLine(row, projectName)}</span>
        {lines.some((line) => line.length > 0) && (
          <pre className="ph-tcard-term" aria-hidden="true">
            {lines.map((line, y) => <div key={y}>{line.map((run, x) => <span key={x} style={runStyle(run)}>{run.t}</span>)}</div>)}
          </pre>
        )}
      </div>
      <div className="ph-tcard-ft">
        <span className="ph-dot" data-level={status.level} aria-hidden />
        <span className="ph-tcard-st" data-level={status.level}>{status.text}</span>
        {asks && <button type="button" className="ph-btn sm warn" disabled={offline} onClick={answer}>{action?.kind === "answer" || !action?.label ? t("term.card.answer") : action.label}</button>}
        {live && action && !asks && <button type="button" className="ph-btn sm" onClick={() => navigate(action.path)}>{action.label}</button>}
      </div>
      {menu && (
        <ActionSheet onClose={() => setMenu(false)}
          preview={{ title: <span className="mono">{row.title || t("term.untitled")}</span>, meta: [t(`term.env.${row.env}`), ownerLine(row, projectName), status.text].join(" · ") }}
          items={[{ label: t("term.card.open"), icon: "expand", onSelect: open }, ...(offline ? [] : items)]} />
      )}
      {layer}
      {answering && row.owner.id && row.project_id && <AnswerSheet row={row} staffId={row.owner.id} projectId={row.project_id} toast={toast} onClose={() => setAnswering(false)} onOpen={() => { setAnswering(false); open(); }} />}
    </article>
  );
}

/** The request a staff member's terminal waits on, answered without opening the terminal. */
function AnswerSheet({ row, staffId, projectId, toast, onClose, onOpen }: { row: TerminalRow; staffId: string; projectId: string; toast: (text: string) => void; onClose: () => void; onOpen: () => void }) {
  const enc = encodeURIComponent;
  const { data } = useQuery<{ asks: Ask[] }>(`/api/asks?project=${enc(projectId)}&routed_to=operator`, { pollMs: 10000, staleMs: 2000 });
  const { data: view } = useQuery<{ capabilities?: HarnessCapabilities }>(`/api/staff/${enc(staffId)}/session`, { staleMs: 10000 });
  const ask = (data?.asks ?? []).filter((a) => a.staff_id === staffId && !a.resolved_at).sort((a, b) => a.created_at.localeCompare(b.created_at))[0];
  const name = row.owner.label || t("term.card.unnamed");
  const colour = peekColour(projectId, staffId);
  return (
    <BottomSheet onClose={onClose} className="ph-answer" label={t("ph.term.asks", { name })}
      title={<span className="ph-sheet-kt"><span className="ph-avatar" style={{ ["--c" as string]: colourVar(colour) }} aria-hidden>{initials(name)}</span><span className="ph-sheet-kt-w"><span className="ph-sheet-kt-t">{t("ph.term.asks", { name })}</span><span className="ph-sheet-kt-m">{[row.title, ask ? relTime(ask.created_at) : ""].filter(Boolean).join(" · ")}</span></span></span>}
      footer={<div className="ph-foot-stack">{ask ? <PhoneAskAnswers ask={ask} projectId={projectId} toast={toast} always={canAlways(ask, view?.capabilities)} server={alwaysServer(ask, view?.capabilities)} onDone={onClose} /> : null}<button type="button" className="ph-btn ghost" onClick={onOpen}>{t("ph.term.openTerminal")}</button></div>}>
      <div className="ph-sheet-pad">
        {ask ? <pre className="ph-code">{askWords(ask)}{row.cwd ? `\n${t("ph.term.cwd", { path: row.cwd })}` : ""}</pre> : data ? <div className="ph-prose quiet">{t("ph.term.noask")}</div> : <RowSkeleton rows={1} lead="none" />}
        {row.env === "host" && <Banner tone="warn" icon="lock" sub={t("ph.term.host.sub")}>{t("ph.term.host")}</Banner>}
      </div>
    </BottomSheet>
  );
}

/** A staff member's colour from the team listing the app already holds, grey when it holds none. */
function peekColour(projectId: string, staffId: string): string {
  const team = peek<{ staff?: { id: string; color: string }[] }>(`/api/projects/${encodeURIComponent(projectId)}/staff?archived=0`) ?? peek<{ staff?: { id: string; color: string }[] }>(`/api/projects/${encodeURIComponent(projectId)}/staff`);
  return team?.staff?.find((m) => m.id === staffId)?.color ?? "slate";
}
