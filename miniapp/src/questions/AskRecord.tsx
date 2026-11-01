// An AskUser call in the transcript: a step among the turn's other steps, where it was asked.
//
// It used to be a bare "Asked you" step whose only detail was the call's JSON, then a block of its
// own drawn after all of the turn's steps. That pinned every question to the bottom of the turn,
// however early it was asked, and two of them stacked there under the work they had started. The
// record is one line now — what was asked and what was chosen — and opens to each question with its
// answer, never to the JSON the host and the model exchange.

import type { ToolItem } from "../turns";
import type { PreviewSource } from "../preview";
import { MessageAttachments } from "../attachcards";
import { Icon } from "../icons";
import { Chevron } from "../ui/components";
import { plural, t } from "../i18n";
import { recordOf, type AskRecord as Record } from "./ask";

/** What the folded line says after the verb: the question and its answer, or how it ended. A reply
 *  that was only files is named by them, rather than said to be no answer at all. */
function headOf(record: Record, waiting: boolean, files: readonly string[]): { question: string; answer: string; status: string } {
  const first = record.rows[0];
  const question = first ? first.header || first.question : "";
  if (waiting) return { question, answer: "", status: t("ask.record.waiting") };
  if (record.state === "expired") return { question, answer: "", status: t("ask.record.expired.short") };
  if (record.state === "unanswered") return { question, answer: "", status: t("ask.record.none") };
  if (record.cut || record.raw) return { question, answer: "", status: t("ask.record.answered") };
  return { question, answer: first?.answer || files.join(", ") || t("ask.record.none"), status: "" };
}

/**
 * `waiting`: the call the session still waits on. Its card is above the composer, where the operator
 * answers it; here it is only the step, so the turn's order stays whole while it waits.
 *
 * The files a reply carried are drawn under the line whether it is open or not, the way a message's
 * sit above its bubble: they are the answer as much as its words are, and they used to be printed as
 * the host's list for the model, paths and all.
 */
export function AskRecord({ item, waiting = false, open, onToggle, sessionId, onOpen }: { item: ToolItem; waiting?: boolean; open: boolean; onToggle: () => void; sessionId?: string; onOpen?: (src: PreviewSource) => void }) {
  const record = recordOf(item.args, item.result, item.error);
  const state = waiting ? "waiting" : record.state;
  const n = record.rows.length;
  const files = record.state === "answered" && !waiting ? item.attachments ?? [] : [];
  const head = headOf(record, waiting, files.map((file) => file.name));
  // A member's question answered by the orchestrator says so: read back in the member's session, a
  // bare answer left the operator guessing whether it was their own.
  const byOrchestrator = record.state === "answered" && record.by === "orchestrator";
  return (
    <div className="act-wrap ask-record" data-state={state}>
      <div className="act" onClick={onToggle} role="button" aria-expanded={open} tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onToggle(); } }}>
        <Icon name="question" size={16} />
        <span className="verb">{t("tool.AskUser")}</span>
        <span className="detail ask-record-line">
          {n > 1 && <span className="ask-record-count">{plural("ask.record.count", n)}</span>}
          {head.question && <span className="ask-record-head-q" title={record.rows[0]?.question}>{head.question}</span>}
          {head.answer && <span className="ask-record-arrow" aria-hidden="true">→</span>}
          {head.answer && <span className={`ask-record-head-a ${record.rows[0]?.answer ? "" : "none"}`} title={head.answer}>{head.answer}</span>}
          {head.status && <span className="ask-record-status">{head.status}</span>}
        </span>
        <span className="act-end">
          {byOrchestrator && <span className="act-orig">{t("ask.record.answered.orchestrator")}</span>}
          <Chevron open={open} />
        </span>
      </div>
      {files.length > 0 && sessionId && onOpen && (
        <div className="ask-record-files">
          <MessageAttachments sessionId={sessionId} items={files} onOpen={onOpen} />
        </div>
      )}
      {open && (
        <div className="ask-record-body">
          {record.state === "expired" && !waiting && <p className="ask-record-note">{t("ask.record.expired")}</p>}
          {record.state === "unanswered" && !waiting && <p className="ask-record-note">{record.raw ? record.raw.slice(0, 600) : t("ask.record.unanswered")}</p>}
          {record.cut && <p className="ask-record-note">{t("ask.record.cut")}</p>}
          {/* A result in another program's words (an imported session) is prose, and reads as it came. */}
          {record.state === "answered" && record.raw && <p className="ask-record-note ask-record-raw">{record.raw.slice(0, 2000)}</p>}
          {n > 0 && (
            <ol className="ask-record-rows">
              {record.rows.map((row, i) => (
                <li key={i} className="ask-record-row">
                  {row.header && <span className="ask-record-tag">{row.header}</span>}
                  <span className="ask-record-q">{row.question}</span>
                  {!waiting && !record.cut && record.state === "answered" && !record.raw && (
                    <span className={`ask-record-a ${row.answer ? "" : "none"}`}>{row.answer || t("ask.record.none")}</span>
                  )}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}
