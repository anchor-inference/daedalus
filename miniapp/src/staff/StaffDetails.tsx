// The Details tab beside a command-line member: the same flat sections as a session's Details, with
// what the host knows of a CLI it does not run itself — the launch (CLI, version, model, effort,
// permissions), the status and the turn, how long the session has run and when the member was last
// heard, and the spend its transcript reports. What the CLI does not report is said to be so: a
// meter drawn from a guessed window, or a cost where there is none, would read as a fact.

import type { KeptLine, StaffSessionView, StaffTurn } from "../api";
import { Section } from "../details";
import { duration, relTimeLong, shortDateTime, tokens, usd } from "../format";
import { DICT, plural, t } from "../i18n";
import { HarnessBadge } from "../team/parts";
import { HARNESS_NAMES, type Staff } from "../team/team";
import { contextFill, conversationCounts, hasSpend, keptWords, sessionAge, windowName } from "./details";
import { turnFacts } from "./model";

function Row({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div className="dt-row sub">
      <span className="dt-key">{label}</span>
      <span className={`grow ${mono ? "mono" : ""}`}>{children}</span>
    </div>
  );
}

/** One line per setting and what keeps it. Plain rows: the kind is the one word that matters, so it
 *  stands at the end of the line rather than in a colour the operator has to decode. */
export function KeptList({ lines, harness }: { lines: KeptLine[]; harness: Staff["harness"] }) {
  const cli = HARNESS_NAMES[harness];
  return (
    <div className="kept-list" data-kept-list>
      {lines.map((line) => {
        const words = keptWords(line, (key) => key in DICT);
        return (
          <div key={line.setting} className="dt-row sub prose" data-kept={line.kept} data-setting={line.setting}>
            <span className="dt-key">{t(words.label)}</span>
            <span className="grow">{words.text ? t(words.text, { cli, mode: line.mode }) : "—"}</span>
            <span className="kept-kind">{t(words.kind)}</span>
          </div>
        );
      })}
    </div>
  );
}

export function StaffDetails({ member, view, turns, now = Date.now() }: { member: Staff; view: StaffSessionView | null; turns: StaffTurn[]; now?: number }) {
  const ids = `staff-${member.id}`;
  const cli = HARNESS_NAMES[member.harness];
  const session = view?.session ?? null;
  const launch = view?.launch;
  const usage = view?.usage ?? null;
  const fill = contextFill(usage);
  const age = sessionAge(view, now);
  const facts = turnFacts(turns, session?.status_at, now);
  const counts = conversationCounts(turns);
  const health = view?.health ?? member.health ?? null;
  const notReported = t("staff.details.notreported", { cli });
  const status = session ? session.status : member.status;
  return (
    <div className="details staff-details" data-staff-details>
      <Section ids={ids} id="staff-run" label={t("staff.details.run")} aside={<HarnessBadge harness={member.harness} />}>
        <Row label={t("staff.details.cli")}>{launch?.version ? `${cli} ${launch.version}` : cli}</Row>
        <Row label={t("staff.details.model")}>{launch?.model || member.model || t("staff.details.model.default")}</Row>
        <Row label={t("staff.details.effort")}>{launch?.effort || member.effort || "—"}</Row>
        <Row label={t("staff.details.permissions")}>{launch?.permission_mode || member.permission_mode || "—"}</Row>
        {(launch?.branch || session?.branch) && <Row label={t("staff.details.branch")} mono>{launch?.branch || session?.branch}</Row>}
      </Section>

      {(view?.kept ?? []).length > 0 && (
        <Section ids={ids} id="staff-kept" label={t("staff.kept.title")}>
          <KeptList lines={view?.kept ?? []} harness={member.harness} />
        </Section>
      )}

      {/* The heading names the state only when no session has a Status row to say it: both at once
          read "working" twice, one line under the other. */}
      <Section ids={ids} id="staff-state" label={t("staff.details.state")} aside={session ? undefined : t(`team.status.${status}`)}>
        {!session && <p className="sub">{t("staff.details.nosession")}</p>}
        {session && (
          <>
            <Row label={t("staff.details.status")}>{[t(`team.status.${session.status}`), session.waiting_for].filter(Boolean).join(" · ")}</Row>
            {facts.turn > 0 && <Row label={t("staff.details.turn")}>{t("staff.facts", { turn: facts.turn, minutes: facts.minutes ?? 0 })}</Row>}
            {age !== null && <Row label={t("staff.details.working")}>{t("staff.details.working.value", { time: duration(age), when: shortDateTime(session.started_at) })}</Row>}
            {session.last_signal_at && <Row label={t("staff.details.heard")}>{relTimeLong(session.last_signal_at, now)}</Row>}
            {health?.last_hook_at && <Row label={t("staff.details.hook")}>{relTimeLong(health.last_hook_at, now)}</Row>}
            {health?.last_team_call_at && <Row label={t("staff.details.teamcall")}>{relTimeLong(health.last_team_call_at, now)}</Row>}
            <Row label={t("staff.details.conversation")}>{t("staff.details.conversation.value", { replies: plural("staff.details.replies", counts.replies), orchestrator: counts.orchestrator, operator: counts.operator })}</Row>
          </>
        )}
      </Section>

      <Section ids={ids} id="staff-context" label={t("session.context")} aside={fill?.pct != null ? `${fill.pct}%` : undefined}>
        {fill ? (
          <>
            {fill.pct !== null && <div className={`bar ${fill.pct >= 90 ? "bad" : fill.pct >= 60 ? "attn" : ""}`} style={{ ["--v" as string]: fill.pct }}><i /></div>}
            <div className="sub" data-context-fill>
              {/* Named, not a bare number: "386k · window not reported" read as if 386k were the window
                  the sentence says is unknown. */}
              {fill.window ? `${tokens(fill.tokens)} / ${tokens(fill.window)}` : `${t("staff.details.fill.used", { n: tokens(fill.tokens) })} · ${t("staff.details.nowindow", { cli })}`}
            </div>
            <p className="sub">{t("staff.details.asof", { when: relTimeLong(usage?.at, now) || "—" })}</p>
          </>
        ) : (
          <p className="sub" data-not-reported>{notReported}</p>
        )}
      </Section>

      <Section ids={ids} id="staff-usage" label={t("session.usage")} aside={hasSpend(usage) && usage?.cost_usd != null ? usd(usage.cost_usd) : undefined}>
        {hasSpend(usage) ? (
          <>
            <div className="dt-row sub" data-staff-spend>
              <span title={t("session.usage.in")}>{tokens(usage?.input_tokens)}<span aria-hidden>↑</span></span>
              <span title={t("session.usage.out")}>{tokens(usage?.output_tokens)}<span aria-hidden>↓</span></span>
              {usage?.cost_usd != null && <><span className="dt-sep">·</span><span>{usd(usage.cost_usd)}</span></>}
            </div>
            <p className="sub">{t(usage?.source === "metered" ? "staff.details.spend.metered" : usage?.cost_usd != null ? "staff.details.spend.equivalent" : "staff.details.spend.nocost", { cli })}</p>
          </>
        ) : (
          <p className="sub" data-not-reported>{session ? notReported : t("staff.details.nosession")}</p>
        )}
        {(usage?.windows ?? []).map((w) => {
          const name = windowName(w.minutes);
          return (
            <div key={w.minutes} className="quota" data-window={w.minutes}>
              <div className="sub quota-line">
                <span className="grow">{t(name.key, { n: name.n ?? 0 })}</span>
                <span>{Math.round(w.used_pct)}%</span>
              </div>
              <div className="quota-bar">
                <i style={{ width: `${Math.min(100, Math.max(0, w.used_pct))}%`, background: w.used_pct >= 100 ? "var(--bad)" : w.used_pct >= 80 ? "var(--warn)" : "var(--ok)" }} />
              </div>
            </div>
          );
        })}
        {hasSpend(usage) && !(usage?.windows ?? []).length && <p className="sub" data-no-windows>{t("staff.details.nowindows", { cli })}</p>}
      </Section>
    </div>
  );
}
