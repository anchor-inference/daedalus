// The status line in a workspace pane's foot: paddock's line of mono segments, one colour per kind of
// data (the --data-* tokens of the active theme). Segments a narrow pane cannot hold are dropped by
// container queries in workspace.css, least useful first; the tooltip always carries every value in
// full. The folder and the branch are the pane header's words, so the foot leaves them out.

import { t } from "../i18n";
import type { TerminalView } from "../api";
import type { TerminalState } from "./instance";
import { contextText, costText, hasFacts, shortTokens, statusFacts, statusTooltip, type StatusFacts } from "./statusfacts";

function ContextMeter({ context }: { context: NonNullable<StatusFacts["context"]> }) {
  return (
    <span className="tsl-seg tsl-ctx" data-seg="context">
      {context.pct !== null && (
        <span className="tsl-meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={context.pct} aria-label={t("term.status.context")}>
          <span className="tsl-meter-fill" style={{ width: `${Math.max(4, context.pct)}%` }} />
        </span>
      )}
      <span className="tsl-ctx-text">{contextText(context)}</span>
    </span>
  );
}

/** The segments of one line, without the folder and the branch: the pane's header says those. */
export function StatusLine({ facts }: { facts: StatusFacts }) {
  const { agent, context, cachePct, output, cost, waiting, command } = facts;
  if (!hasFacts({ ...facts, path: null, branch: null })) return null;
  return (
    <div className="tsl" title={statusTooltip(facts)}>
      {waiting && (
        <span className="tsl-seg tsl-wait" data-seg="waiting">
          <span className="tsl-wait-dot" aria-hidden="true" />
          <span className="tsl-wait-word">{t("term.status.waiting")}</span>
          {waiting.why && <span className="tsl-wait-why truncate">{waiting.why}</span>}
        </span>
      )}
      {agent && (
        <span className="tsl-seg tsl-agent" data-seg="agent">
          <span className="tsl-model truncate">{agent.label}</span>
          {agent.effort && <span className="tsl-effort">{agent.effort}</span>}
        </span>
      )}
      {context && <ContextMeter context={context} />}
      {cachePct !== null && (
        <span className="tsl-seg tsl-cache" data-seg="cache">{t("term.status.cache", { pct: cachePct })}</span>
      )}
      {output !== null && (
        <span className="tsl-seg tsl-out" data-seg="output">{t("term.status.output", { n: shortTokens(output) })}</span>
      )}
      {cost && <span className="tsl-seg tsl-cost" data-seg="cost">{costText(cost)}</span>}
      {command && (
        <span className="tsl-seg tsl-cmd" data-seg="command" data-result={command.running ? "running" : command.exit === 0 ? "ok" : command.exit === null ? "unknown" : "failed"}>
          <span className="tsl-cmd-mark" aria-hidden="true">{command.running ? "›" : command.exit === 0 ? "✓" : "✗"}</span>
          <span className="tsl-cmd-text truncate">{command.text}</span>
          {!command.running && command.exit !== null && command.exit !== 0 && <span className="tsl-cmd-code">{t("term.status.exit", { code: command.exit })}</span>}
        </span>
      )}
    </div>
  );
}

/** The line for a terminal row, with the live state of a pane when one shows it. */
export function TerminalStatusLine({ row, state, home }: { row: TerminalView | null | undefined; state?: TerminalState | null; home?: string | null }) {
  if (!row) return null;
  return <StatusLine facts={statusFacts(row, state, home)} />;
}
