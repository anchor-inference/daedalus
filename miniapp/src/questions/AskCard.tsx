// The agent's question, above the composer: one question at a time, drawn by its kind, with the
// chips of a batch above it and the one primary button below that says what it does.
//
// The rules live in ./ask.ts; this file is the drawing and the keys. The card is remounted for new
// questions (the composer keys it on `questionsKey`), so what the operator picked survives the
// session being read again every few seconds — a re-read once wiped a pick between choosing and
// pressing Reply, and Reply then sent nothing.

import { forwardRef, useEffect, useId, useImperativeHandle, useRef, useState, type KeyboardEvent } from "react";
import type { Question } from "../api";
import { Icon } from "../icons";
import { t, plural } from "../i18n";
import { errorText } from "../ui";
import {
  allAnswered,
  answerLine,
  blankDrafts,
  digitPick,
  goEnabled,
  goOf,
  hasPreview,
  hasReview,
  kindOf,
  missingOf,
  optionLabel,
  pick,
  pickOther,
  takesOther,
  toAnswers,
  unanswered,
  type Answer,
  type AskDraft,
  type Missing,
} from "./ask";
import "./ask.css";

export type AskCardHandle = {
  /** What the composer's Reply circle does: the card's own primary (next, review or send). */
  go: () => void;
};

type Props = {
  questions: Question[];
  phone: boolean;
  onSend: (answers: Answer[]) => Promise<void>;
  toast: (text: string) => void;
};

const MISSING_KEY: Record<Missing, string> = { pick: "ask.need.pick", pickAny: "ask.need.pickAny", write: "ask.need.write", other: "ask.need.other" };

export const AskCard = forwardRef<AskCardHandle, Props>(function AskCard({ questions, phone, onSend, toast }, ref) {
  const n = questions.length;
  const [drafts, setDrafts] = useState<AskDraft[]>(() => blankDrafts(questions));
  const [step, setStep] = useState(0);
  const [sending, setSending] = useState(false);
  // The option whose preview shows: the one under the pointer or the keyboard, else the chosen one.
  const [peek, setPeek] = useState<string | null>(null);
  const root = useRef<HTMLElement>(null);
  // Focus follows the step only once the operator has moved it: a question arriving must not pull
  // the cursor out of a draft being typed in the composer.
  const moved = useRef(false);
  const uid = useId();

  const complete = allAnswered(questions, drafts);
  const reviewing = hasReview(n) && step >= n;
  const q = reviewing ? undefined : questions[step];
  const draft = drafts[step];
  const go = goOf(step, n, complete);
  const enabled = !sending && goEnabled(go, q, draft, complete);

  useEffect(() => {
    if (!moved.current) return;
    const body = root.current?.querySelector<HTMLElement>(".ask-body");
    const target = body?.querySelector<HTMLElement>("input:checked, textarea, input, .ask-review-row.missing button, .ask-go") ?? root.current?.querySelector<HTMLElement>(".ask-go");
    target?.focus({ preventScroll: true });
  }, [step]);

  function update(index: number, next: (d: AskDraft) => AskDraft) {
    setDrafts((list) => list.map((d, i) => (i === index ? next(d) : d)));
  }

  function moveTo(next: number) {
    moved.current = true;
    setPeek(null);
    setStep(Math.max(0, Math.min(next, hasReview(n) ? n : n - 1)));
  }

  async function primary() {
    if (sending) return;
    if (!enabled) {
      // Pressed while something is missing (the Reply circle is always live): show where.
      moved.current = true;
      if (go === "send" && n > 1 && !reviewing) moveTo(n);
      else if (go === "send" && reviewing) moveTo(unanswered(questions, drafts)[0] ?? step);
      else root.current?.querySelector<HTMLElement>(".ask-body input, .ask-body textarea")?.focus();
      return;
    }
    if (go === "next" || go === "review") {
      moveTo(go === "review" ? n : step + 1);
      return;
    }
    setSending(true);
    try {
      await onSend(toAnswers(questions, drafts));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setSending(false);
    }
  }

  useImperativeHandle(ref, () => ({ go: () => void primary() }));

  function onKeyDown(e: KeyboardEvent<HTMLElement>) {
    if (e.nativeEvent.isComposing || e.altKey || e.ctrlKey || e.metaKey) return;
    const target = e.target as HTMLElement;
    const typing = target.tagName === "TEXTAREA";
    if (e.key === "Enter" && !e.shiftKey && (typing || target.tagName === "INPUT" || target === root.current)) {
      e.preventDefault();
      void primary();
      return;
    }
    if (typing || !q) return;
    const rows = Array.from(root.current?.querySelectorAll<HTMLInputElement>(".ask-body .ask-opt input") ?? []);
    const index = digitPick(e.key, rows.length);
    if (index !== null) {
      e.preventDefault();
      moved.current = true;
      rows[index].click();
      rows[index].focus();
      return;
    }
    // Radios move with the arrows on their own; checkboxes do not, so the list moves for them.
    if ((e.key === "ArrowDown" || e.key === "ArrowUp") && target.tagName === "INPUT" && (target as HTMLInputElement).type === "checkbox") {
      const at = rows.indexOf(target as HTMLInputElement);
      const next = rows[at + (e.key === "ArrowDown" ? 1 : -1)];
      if (next) {
        e.preventDefault();
        next.focus();
      }
    }
  }

  const missing = q ? missingOf(q, draft) : null;
  const left = unanswered(questions, drafts);
  const hint = sending
    ? t("ask.sending")
    : enabled
      ? (phone ? "" : t(go === "send" ? "ask.key.send" : "ask.key.next"))
      : go === "send" && (reviewing || n > 1)
        ? plural("ask.need.more", left.length, { which: left.map((i) => questions[i].header || `${i + 1}`).join(", ") })
        : missing ? t(MISSING_KEY[missing]) : "";
  const label = sending ? t("ask.sending") : go === "next" ? t("ask.next") : go === "review" ? t("ask.review") : n > 1 ? t("ask.send.all") : t("ask.send.one");

  return (
    <section ref={root} className={`ask ${phone ? "phone" : ""}`} role="group" aria-label={t("composer.question")} data-step={reviewing ? "review" : step} data-steps={n} onKeyDown={onKeyDown}>
      <header className="ask-head">
        <Icon name="question" size={14} />
        <span className="ask-eyebrow">{t("composer.question")}</span>
        {n > 1 && <span className="ask-count num">{reviewing ? t("ask.review.title") : t("ask.count", { i: step + 1, n })}</span>}
      </header>
      {n > 1 && (
        <nav className="ask-steps" aria-label={t("ask.steps")}>
          {questions.map((x, i) => {
            const done = !missingOf(x, drafts[i]);
            const state = i === step ? "current" : done ? "done" : "todo";
            return (
              <button key={i} type="button" className="ask-step" data-state={state} aria-current={i === step ? "step" : undefined} onClick={() => moveTo(i)}
                title={`${i + 1}. ${x.header ? `${x.header} · ` : ""}${x.question}`} aria-label={t(done ? "ask.step.done" : "ask.step.todo", { i: i + 1, name: x.header || x.question })}>
                <span className="ask-step-n">{done && i !== step ? <Icon name="check" size={12} /> : i + 1}</span>
                {x.header && <span className="ask-step-label">{x.header}</span>}
              </button>
            );
          })}
          {hasReview(n) && (
            <button type="button" className="ask-step review" data-state={reviewing ? "current" : complete ? "done" : "todo"} aria-current={reviewing ? "step" : undefined} onClick={() => moveTo(n)}>
              <span className="ask-step-label">{t("ask.review.chip")}</span>
            </button>
          )}
        </nav>
      )}
      <div className="ask-body" key={reviewing ? "review" : step}>
        {q ? (
          <QuestionStep q={q} draft={draft} name={`${uid}-${step}`} phone={phone} peek={peek} onPeek={setPeek}
            onPick={(label) => { moved.current = true; update(step, (d) => pick(q, d, label)); }}
            onOther={() => { moved.current = true; update(step, (d) => pickOther(q, d)); }}
            onWords={(text) => update(step, (d) => ({ ...d, custom: text }))} />
        ) : (
          <ol className="ask-review">
            {questions.map((x, i) => {
              const a = toAnswers([x], [drafts[i]])[0];
              const line = missingOf(x, drafts[i]) ? "" : answerLine(a.selected, a.custom);
              return (
                <li key={i} className={`ask-review-row ${line ? "" : "missing"}`}>
                  <span className="ask-review-q">
                    {x.header && <span className="ask-chip">{x.header}</span>}
                    <span className="ask-review-text">{x.question}</span>
                  </span>
                  <span className="ask-review-a">{line || t("ask.review.none")}</span>
                  <button type="button" className="ask-review-edit" onClick={() => moveTo(i)} aria-label={t("ask.review.edit.label", { i: i + 1 })}>
                    {line ? t("ask.review.edit") : t("ask.review.answer")}
                  </button>
                </li>
              );
            })}
          </ol>
        )}
      </div>
      <footer className="ask-foot">
        {n > 1 && step > 0 && (
          <button type="button" className="btn ask-back" onClick={() => moveTo(step - 1)} aria-label={t("ask.back")}>
            <Icon name="back" size={14} />{!phone && t("ask.back")}
          </button>
        )}
        <span className={`ask-hint ${enabled ? "" : "need"}`} role="status">{hint}</span>
        <button type="button" className="btn primary ask-go" data-go={go} aria-disabled={!enabled} disabled={sending} onClick={() => void primary()}>
          {label}
          {go !== "send" && <Icon name="forward" size={14} />}
        </button>
      </footer>
    </section>
  );
});

type StepProps = {
  q: Question;
  draft: AskDraft;
  name: string;
  phone: boolean;
  peek: string | null;
  onPeek: (label: string | null) => void;
  onPick: (label: string) => void;
  onOther: () => void;
  onWords: (text: string) => void;
};

/** One question: the header chip, the question as body text, what kind of answer it wants, and the answer. */
function QuestionStep({ q, draft, name, phone, peek, onPeek, onPick, onOther, onWords }: StepProps) {
  const kind = kindOf(q);
  const options = q.options ?? [];
  const other = takesOther(q);
  const preview = hasPreview(q);
  const shown = preview ? options.find((o) => o.label === peek) ?? options.find((o) => draft.selected.includes(o.label)) ?? options.find((o) => o.preview) : undefined;
  const ask = kind === "text" ? "ask.kind.text" : kind === "multi" ? "ask.kind.multi" : "ask.kind.single";
  const field = (
    <AutoField value={draft.custom} onChange={onWords} label={kind === "text" ? t("ask.words") : t("ask.other.words")}
      placeholder={kind === "text" ? t("ask.words.placeholder") : t("ask.other.placeholder")} autoFocus={kind !== "text" && draft.other} rows={kind === "text" ? 3 : 2} />
  );
  const list = kind === "text" ? field : (
    <div className="ask-options" role={kind === "multi" ? "group" : "radiogroup"} data-layout={kind === "pair" ? "pair" : "list"} aria-label={q.question}>
      {options.map((o, i) => {
        const on = draft.selected.includes(o.label);
        const { text, recommended } = optionLabel(o.label);
        return (
          <label key={o.label} className={`ask-opt ${on ? "on" : ""}`} data-label={o.label}
            onMouseEnter={preview ? () => onPeek(o.label) : undefined} onMouseLeave={preview ? () => onPeek(null) : undefined}>
            <input type={kind === "multi" ? "checkbox" : "radio"} name={name} checked={on} onChange={() => onPick(o.label)} onFocus={preview ? () => onPeek(o.label) : undefined} />
            {kind !== "pair" && <span className="ask-mark" aria-hidden />}
            <span className="ask-opt-main">
              <span className="ask-opt-label">{text}{recommended && <span className="ask-rec">{t("ask.recommended")}</span>}</span>
              {o.description && <span className="ask-opt-desc">{o.description}</span>}
            </span>
            {!phone && i < 9 && <kbd className="ask-key" aria-hidden>{i + 1}</kbd>}
          </label>
        );
      })}
      {other && (
        <label className={`ask-opt other ${draft.other ? "on" : ""}`}>
          <input type={kind === "multi" ? "checkbox" : "radio"} name={name} checked={draft.other} onChange={onOther} />
          <span className="ask-mark" aria-hidden />
          <span className="ask-opt-main"><span className="ask-opt-label">{t("ask.other")}</span></span>
          {!phone && options.length < 9 && <kbd className="ask-key" aria-hidden>{options.length + 1}</kbd>}
        </label>
      )}
      {other && draft.other && <div className="ask-other-field">{field}</div>}
    </div>
  );
  return (
    <>
      <div className="ask-q">
        {q.header && <span className="ask-chip">{q.header}</span>}
        <p className="ask-text">{q.question}</p>
        <span className="ask-kind">
          {t(ask)}
          {kind === "multi" && draft.selected.length > 0 && <span className="num"> · {plural("ask.chosen", draft.selected.length)}</span>}
        </span>
      </div>
      {preview ? (
        <div className="ask-split">
          {list}
          <pre className="ask-preview" aria-label={t("ask.preview", { name: shown ? optionLabel(shown.label).text : "" })}>{shown?.preview || t("ask.preview.none")}</pre>
        </div>
      ) : list}
    </>
  );
}

/** A text field that grows with its words up to a few lines; Enter continues (the card's key handler), Shift+Enter breaks the line. */
function AutoField({ value, onChange, label, placeholder, autoFocus, rows }: { value: string; onChange: (text: string) => void; label: string; placeholder: string; autoFocus: boolean; rows: number }) {
  const area = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [value]);
  useEffect(() => {
    if (autoFocus) area.current?.focus({ preventScroll: true });
  }, [autoFocus]);
  return <textarea ref={area} className="ask-field" value={value} rows={rows} placeholder={placeholder} aria-label={label} onChange={(e) => onChange(e.target.value)} />;
}
