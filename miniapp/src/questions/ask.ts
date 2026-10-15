// What the agent's question card decides without React: the kind of each question (and so how it is
// drawn), what a pick does to the answer, when an answer is complete, which step comes next and what
// the primary button says, and how an answered AskUser call reads back in the transcript.
//
// The AskUser schema (protocore/tools/ask_user.py) carries no explicit kind: a free-text question is
// one with no options, a yes/no one is two short options, and a choice says `multiSelect`. Every
// question used to be drawn as the same block — options as identical outlined buttons and a text
// field under each one — so six questions read as a wall with no way to tell a yes/no from a
// several-of-these. The kinds below are what the card draws differently.

import type { Question } from "../api";

/** One answer as the host takes it (POST /api/sessions/{id}/answer). */
export type Answer = { question: string; selected: string[]; custom: string | null };

/**
 * How a question is drawn. `text`: no options, the field is the question. `pair`: two short options
 * and nothing else (yes/no, this/that), drawn side by side as two equal buttons. `single` and
 * `multi`: a list of radio or checkbox rows.
 */
export type AskKind = "text" | "pair" | "single" | "multi";

/** Longest label a `pair` button takes; a longer one wraps badly in half the card's width. */
const PAIR_LABEL_MAX = 24;

export function kindOf(q: Question): AskKind {
  const options = q.options ?? [];
  if (!options.length) return "text";
  if (q.multiSelect) return "multi";
  const plain = options.every((o) => !o.description && !o.preview && optionLabel(o.label).text.length <= PAIR_LABEL_MAX && !optionLabel(o.label).recommended);
  if (options.length === 2 && plain && !q.allow_custom) return "pair";
  return "single";
}

/** Whether any option carries a preview: the card then shows the chosen one beside the list. */
export function hasPreview(q: Question): boolean {
  return (q.options ?? []).some((o) => !!o.preview);
}

/** Whether the list ends with "Other…": words in place of (or, for several, beside) the options. */
export function takesOther(q: Question): boolean {
  return !!q.allow_custom && (q.options ?? []).length > 0;
}

// The schema has no "recommended" flag; models mark the option in its label, the way the harness
// prompts teach them to ("Top 8 (Recommended)"). The tag is drawn from it and the suffix dropped from
// the label on screen, while the label the host receives stays verbatim: the host and the model pair
// the answer with the option by its exact text.
const RECOMMENDED = /\s*[([]\s*(recommended|рекомендуется|рекомендую|рекомендовано|рекомендуемый)\s*[)\]]\s*$/i;

export function optionLabel(label: string): { text: string; recommended: boolean } {
  const m = RECOMMENDED.exec(label);
  if (!m || m.index === 0) return { text: label, recommended: false };
  return { text: label.slice(0, m.index), recommended: true };
}

/** What the operator has done to one question. `other` is the "Other…" row being chosen; `custom`
 *  is its words (or the whole answer of a free-text question). */
export type AskDraft = { selected: string[]; custom: string; other: boolean };

export const BLANK: AskDraft = { selected: [], custom: "", other: false };

export function blankDrafts(questions: readonly Question[]): AskDraft[] {
  return questions.map(() => ({ ...BLANK, selected: [] }));
}

/** A pick of an option: the one answer of a single choice (and "Other…" is let go), a toggle of several. */
export function pick(q: Question, draft: AskDraft, label: string): AskDraft {
  if (!q.multiSelect) return { ...draft, selected: [label], other: false };
  const on = draft.selected.includes(label);
  return { ...draft, selected: on ? draft.selected.filter((x) => x !== label) : [...draft.selected, label] };
}

/** A pick of "Other…": instead of the options for one choice, beside them for several. */
export function pickOther(q: Question, draft: AskDraft): AskDraft {
  if (!q.multiSelect) return { ...draft, selected: [], other: true };
  return { ...draft, other: !draft.other };
}

/** What is still missing from one answer, by what the hint under the card says; null when it is complete. */
export type Missing = "pick" | "pickAny" | "write" | "other";

export function missingOf(q: Question, draft: AskDraft | undefined): Missing | null {
  const d = draft ?? BLANK;
  const kind = kindOf(q);
  const words = d.custom.trim().length > 0;
  if (kind === "text") return words ? null : "write";
  // "Other…" chosen and left empty is not an answer, even beside picked options: the operator opened
  // it to say something, and sending without it would drop that silently.
  if (d.other && !words) return "other";
  if (d.other && words) return null;
  if (d.selected.length) return null;
  return kind === "multi" ? "pickAny" : "pick";
}

export function isAnswered(q: Question, draft: AskDraft | undefined): boolean {
  return missingOf(q, draft) === null;
}

export function allAnswered(questions: readonly Question[], drafts: readonly AskDraft[]): boolean {
  return questions.length > 0 && questions.every((q, i) => isAnswered(q, drafts[i]));
}

/** The indexes of the questions not yet answered, in order. */
export function unanswered(questions: readonly Question[], drafts: readonly AskDraft[]): number[] {
  return questions.flatMap((q, i) => (isAnswered(q, drafts[i]) ? [] : [i]));
}

/** What the host receives: the picked labels verbatim, and words only where words were the answer. */
export function toAnswers(questions: readonly Question[], drafts: readonly AskDraft[]): Answer[] {
  return questions.map((q, i) => {
    const d = drafts[i] ?? BLANK;
    const words = d.custom.trim();
    const takesWords = kindOf(q) === "text" || d.other;
    return { question: q.question, selected: kindOf(q) === "text" ? [] : [...d.selected], custom: takesWords && words ? words : null };
  });
}

/** One answer as the review step and the transcript's record read it: the labels as drawn, then the words. */
export function answerLine(selected: readonly string[], custom: string | null | undefined): string {
  return [...selected.map((s) => optionLabel(s).text), ...(custom && custom.trim() ? [custom.trim()] : [])].join(", ");
}

// ── steps ────────────────────────────────────────────────────────────────────────────────────

/**
 * Several questions are one step each, and from three on a review step after the last.
 *
 * One question at a time is what keeps six of them from being a wall: the card stays the height of
 * one question, its options can afford a description each, and the chips above say how far along the
 * operator is. Answers on earlier steps are out of sight, so with three or more a review lists them
 * all before anything is sent. Two answers fit in the chips and in memory, and a review there is a
 * click that says nothing new: the last of two sends directly once both are answered, and the review
 * chip is still there for a look.
 */
export function hasReview(n: number): boolean {
  return n >= 2;
}

/** Whether the last question's primary goes to the review rather than sending. */
export function reviewFirst(n: number, complete: boolean): boolean {
  return n >= 3 || !complete;
}

/** What the primary does on this step: `next` question, the `review`, or `send` the answers. */
export type Go = "next" | "review" | "send";

export function goOf(step: number, n: number, complete: boolean): Go {
  if (n <= 1 || step >= n) return "send";
  if (step < n - 1) return "next";
  return reviewFirst(n, complete) ? "review" : "send";
}

/** Whether the primary can be pressed: a question's step wants its own answer, the send wants them all. */
export function goEnabled(go: Go, q: Question | undefined, draft: AskDraft | undefined, complete: boolean): boolean {
  if (go === "send") return complete;
  return !!q && isAnswered(q, draft);
}

/** A digit key 1–9 as the row it picks, when there is such a row. */
export function digitPick(key: string, rows: number): number | null {
  if (!/^[1-9]$/.test(key)) return null;
  const index = Number(key) - 1;
  return index < rows ? index : null;
}

// ── the record in the transcript ─────────────────────────────────────────────────────────────

/** How an AskUser call ended, as its record in the transcript says it. `expired`: an unattended run's
 *  question that nobody answered, which the scheduler settled with "continue on your own judgement";
 *  `unanswered`: the run was stopped (or the call failed) while it waited. The call the session still
 *  waits on has no record: its card is above the composer. */
export type RecordState = "answered" | "expired" | "unanswered";

export type RecordRow = { header: string; question: string; answer: string };

/** `by`: who answered, as the host's result names it (`orchestrator`, `operator`, `user`); empty when unknown. */
export type AskRecord = { state: RecordState; rows: RecordRow[]; raw: string; by?: string };

function questionsOf(args: Record<string, unknown>): Question[] {
  const list = Array.isArray(args.questions) ? args.questions : [];
  return list.filter((q): q is Question => !!q && typeof q === "object" && typeof (q as Question).question === "string");
}

/**
 * The record of one AskUser call: each question beside what was chosen. The host's result is
 * `{"answers": [{question, selected, custom}], "source"}`; anything else (a call imported from
 * another program, a clipped result) is kept as `raw` and shown as it came. An answer is matched to
 * its question by text, then by position: a reply typed into the composer answers the first
 * question only, and the rest then read as unanswered rather than shifting up.
 */
export function recordOf(args: Record<string, unknown>, result: string | undefined, error: boolean | undefined): AskRecord {
  const questions = questionsOf(args);
  const rows = (answers: { question?: string; selected?: string[]; custom?: string | null }[]) =>
    questions.map((q, i) => {
      const a = answers.find((x) => x.question === q.question) ?? (answers[i] && !answers[i].question ? answers[i] : undefined);
      return { header: q.header ?? "", question: q.question, answer: a ? answerLine(Array.isArray(a.selected) ? a.selected : [], a.custom) : "" };
    });
  if (result === undefined) return { state: "unanswered", rows: rows([]), raw: "" };
  if (error) return { state: "unanswered", rows: rows([]), raw: result };
  try {
    const parsed = JSON.parse(result) as { answers?: unknown; source?: unknown };
    if (!parsed || !Array.isArray(parsed.answers)) throw new Error("not the host's answer");
    if (parsed.source === "timeout") return { state: "expired", rows: rows([]), raw: "" };
    const by = typeof parsed.source === "string" ? parsed.source : "";
    return { state: "answered", rows: rows(parsed.answers as { question?: string; selected?: string[]; custom?: string | null }[]), raw: "", by };
  } catch {
    return { state: "answered", rows: [], raw: result };
  }
}
