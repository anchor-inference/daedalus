// An AskUser call in the transcript: what was asked and what was chosen, one line per question.
//
// It used to be a bare "Asked you" step whose only detail was the call's JSON, so the decisions the
// operator made — often the most important lines of a session — could not be read back without
// opening a code block. The record reads as the answer it was, and says plainly when there was none.

import type { ToolItem } from "../turns";
import { Icon } from "../icons";
import { plural, t } from "../i18n";
import { recordOf } from "./ask";

export function AskRecord({ item }: { item: ToolItem }) {
  const record = recordOf(item.args, item.result, item.error);
  const state = record.state;
  const n = Array.isArray(item.args.questions) ? item.args.questions.length : 0;
  // A member's question answered by the orchestrator says so: read back in the member's session, a
  // bare "answered" left the operator guessing whether it was their own answer.
  const status = state === "answered" ? t(record.by === "orchestrator" ? "ask.record.answered.orchestrator" : "ask.record.answered") : "";
  return (
    <div className="act-wrap ask-record" data-state={state}>
      <div className="act">
        <Icon name="question" size={16} />
        <span className="verb">{t("tool.AskUser")}</span>
        <span className="detail">{[n > 0 ? plural("ask.record.count", n) : "", status].filter(Boolean).join(" · ")}</span>
      </div>
      {state === "expired" && <p className="ask-record-note">{t("ask.record.expired")}</p>}
      {state === "unanswered" && <p className="ask-record-note">{record.raw ? record.raw.slice(0, 300) : t("ask.record.unanswered")}</p>}
      {state === "answered" && record.raw && <p className="ask-record-note">{record.raw.slice(0, 600)}</p>}
      {state === "answered" && record.rows.length > 0 && (
        <ul className="ask-record-rows">
          {record.rows.map((row, i) => (
            <li key={i} className="ask-record-row">
              <span className="ask-record-q" title={row.question}>{row.header || row.question}</span>
              <span className={`ask-record-a ${row.answer ? "" : "none"}`}>{row.answer || t("ask.record.none")}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
