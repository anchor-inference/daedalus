import { describe, expect, it } from "vitest";
import type { Question } from "../api";
import {
  BLANK,
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
  recordOf,
  takesOther,
  toAnswers,
  unanswered,
} from "./ask";

const week: Question = { question: "Which week?", header: "Week", options: [{ label: "Last week (Recommended)", description: "final" }, { label: "This week" }], allow_custom: true };
const sections: Question = { question: "Which sections?", multiSelect: true, options: [{ label: "Releases" }, { label: "Incidents" }, { label: "Numbers" }], allow_custom: true };
const yesno: Question = { question: "Send it?", options: [{ label: "Yes" }, { label: "No" }] };
const subject: Question = { question: "Subject line?", allow_custom: true };
const layout: Question = { question: "Layout?", options: [{ label: "One column", preview: "| a |" }, { label: "Two columns", preview: "| a | b |" }] };

describe("the kind of a question", () => {
  it("is read from the shape the schema allows", () => {
    expect(kindOf(subject)).toBe("text");
    expect(kindOf(yesno)).toBe("pair");
    expect(kindOf(week)).toBe("single");
    expect(kindOf(sections)).toBe("multi");
    // Two options are a pair only while they are short and bare: a description, a preview, Other or
    // a recommendation each need the list's room.
    expect(kindOf({ ...yesno, allow_custom: true })).toBe("single");
    expect(kindOf({ question: "q", options: [{ label: "Yes", description: "now" }, { label: "No" }] })).toBe("single");
    expect(kindOf({ question: "q", options: [{ label: "Yes (Recommended)" }, { label: "No" }] })).toBe("single");
    expect(kindOf({ question: "q", options: [{ label: "A rather long first answer here" }, { label: "No" }] })).toBe("single");
    expect(kindOf(layout)).toBe("single");
    expect(hasPreview(layout)).toBe(true);
    expect(hasPreview(week)).toBe(false);
    expect(takesOther(week)).toBe(true);
    expect(takesOther(subject)).toBe(false);
  });

  it("finds the recommended option in its label without changing what is sent", () => {
    expect(optionLabel("Top 8 (Recommended)")).toEqual({ text: "Top 8", recommended: true });
    expect(optionLabel("Восемь (рекомендуется)")).toEqual({ text: "Восемь", recommended: true });
    expect(optionLabel("Top 8 [recommended]")).toEqual({ text: "Top 8", recommended: true });
    expect(optionLabel("Top 8")).toEqual({ text: "Top 8", recommended: false });
    // A label that is nothing but the marker keeps it: there would be no label left to draw.
    expect(optionLabel("(Recommended)")).toEqual({ text: "(Recommended)", recommended: false });
    const answers = toAnswers([week], [pick(week, BLANK, "Last week (Recommended)")]);
    expect(answers[0].selected).toEqual(["Last week (Recommended)"]);
  });
});

describe("picking", () => {
  it("replaces the one answer of a single choice, and Other lets the option go", () => {
    let d = pick(week, BLANK, "This week");
    d = pick(week, d, "Last week (Recommended)");
    expect(d.selected).toEqual(["Last week (Recommended)"]);
    d = pickOther(week, d);
    expect(d).toMatchObject({ selected: [], other: true });
    d = pick(week, d, "This week");
    expect(d).toMatchObject({ selected: ["This week"], other: false });
  });

  it("toggles several, and Other sits beside them", () => {
    let d = pick(sections, BLANK, "Releases");
    d = pick(sections, d, "Numbers");
    d = pick(sections, d, "Releases");
    expect(d.selected).toEqual(["Numbers"]);
    d = pickOther(sections, d);
    expect(d).toMatchObject({ selected: ["Numbers"], other: true });
    expect(pickOther(sections, d).other).toBe(false);
  });
});

describe("what is missing", () => {
  it("names the reason the hint gives", () => {
    expect(missingOf(week, BLANK)).toBe("pick");
    expect(missingOf(sections, BLANK)).toBe("pickAny");
    expect(missingOf(subject, BLANK)).toBe("write");
    expect(missingOf(subject, { ...BLANK, custom: "   " })).toBe("write");
    expect(missingOf(subject, { ...BLANK, custom: "Week 41" })).toBeNull();
    expect(missingOf(week, pick(week, BLANK, "This week"))).toBeNull();
    // Other opened and left empty holds the answer, even beside picked options.
    expect(missingOf(week, pickOther(week, BLANK))).toBe("other");
    expect(missingOf(sections, pickOther(sections, pick(sections, BLANK, "Releases")))).toBe("other");
    expect(missingOf(week, { ...pickOther(week, BLANK), custom: "the last full one" })).toBeNull();
    expect(missingOf(week, undefined)).toBe("pick");
  });

  it("counts the batch as answered only when every question is", () => {
    const qs = [week, subject];
    const drafts = blankDrafts(qs);
    expect(allAnswered(qs, drafts)).toBe(false);
    expect(unanswered(qs, drafts)).toEqual([0, 1]);
    drafts[1] = { ...drafts[1], custom: "Week 41" };
    expect(unanswered(qs, drafts)).toEqual([0]);
    drafts[0] = pick(week, drafts[0], "This week");
    expect(allAnswered(qs, drafts)).toBe(true);
    expect(allAnswered([], [])).toBe(false);
    // Drafts not yet laid out for the questions are not answers.
    expect(allAnswered(qs, [])).toBe(false);
  });
});

describe("what is sent", () => {
  it("carries words only where words were the answer", () => {
    const qs = [week, sections, subject, yesno];
    const drafts = [
      { ...pick(week, BLANK, "This week"), custom: "typed under Other, then an option was picked" },
      { ...pickOther(sections, pick(sections, BLANK, "Releases")), custom: "and the roadmap" },
      { ...BLANK, custom: "  Week 41  " },
      pick(yesno, BLANK, "No"),
    ];
    expect(toAnswers(qs, drafts)).toEqual([
      { question: "Which week?", selected: ["This week"], custom: null },
      { question: "Which sections?", selected: ["Releases"], custom: "and the roadmap" },
      { question: "Subject line?", selected: [], custom: "Week 41" },
      { question: "Send it?", selected: ["No"], custom: null },
    ]);
  });

  it("reads back as the labels drawn and then the words", () => {
    expect(answerLine(["Top 8 (Recommended)", "All"], " more ")).toBe("Top 8, All, more");
    expect(answerLine([], null)).toBe("");
  });
});

describe("steps", () => {
  it("send straight away for one question", () => {
    expect(goOf(0, 1, false)).toBe("send");
    expect(hasReview(1)).toBe(false);
  });

  it("walk several one at a time, with a review before sending three or more", () => {
    expect(hasReview(6)).toBe(true);
    expect(goOf(0, 6, false)).toBe("next");
    expect(goOf(4, 6, false)).toBe("next");
    expect(goOf(5, 6, true)).toBe("review");
    expect(goOf(6, 6, true)).toBe("send");
  });

  it("let the last of two send once both are answered, and review otherwise", () => {
    expect(goOf(0, 2, false)).toBe("next");
    expect(goOf(1, 2, true)).toBe("send");
    expect(goOf(1, 2, false)).toBe("review");
    expect(goOf(2, 2, false)).toBe("send");
  });

  it("hold a question's step for its own answer and the send for all of them", () => {
    expect(goEnabled("next", week, BLANK, false)).toBe(false);
    expect(goEnabled("next", week, pick(week, BLANK, "This week"), false)).toBe(true);
    expect(goEnabled("review", subject, { ...BLANK, custom: "x" }, false)).toBe(true);
    expect(goEnabled("send", undefined, undefined, false)).toBe(false);
    expect(goEnabled("send", undefined, undefined, true)).toBe(true);
  });

  it("pick a row by digit only when the row exists", () => {
    expect(digitPick("1", 3)).toBe(0);
    expect(digitPick("3", 3)).toBe(2);
    expect(digitPick("4", 3)).toBeNull();
    expect(digitPick("0", 3)).toBeNull();
    expect(digitPick("a", 3)).toBeNull();
  });
});

describe("the record in the transcript", () => {
  const args = { questions: [week, sections, subject] };

  it("lines each question up with what was chosen", () => {
    const result = JSON.stringify({ answers: [
      { question: "Which week?", selected: ["Last week (Recommended)"], custom: null },
      { question: "Which sections?", selected: ["Releases", "Numbers"], custom: "and the roadmap" },
      { question: "Subject line?", selected: [], custom: "Week 41" },
    ], source: "user" });
    const record = recordOf(args, result, false);
    expect(record.state).toBe("answered");
    expect(record.rows).toEqual([
      { header: "Week", question: "Which week?", answer: "Last week" },
      { header: "", question: "Which sections?", answer: "Releases, Numbers, and the roadmap" },
      { header: "", question: "Subject line?", answer: "Week 41" },
    ]);
  });

  it("leaves the questions a typed reply did not reach unanswered rather than shifting answers up", () => {
    // A message typed into the composer while the agent asks answers the first question only, and
    // the host fills its question text in.
    const result = JSON.stringify({ answers: [{ question: "Which week?", selected: [], custom: "the last full one" }], source: "user" });
    expect(recordOf(args, result, false).rows.map((r) => r.answer)).toEqual(["the last full one", "", ""]);
  });

  it("keeps who answered, so a member's question the orchestrator settled reads as the orchestrator's", () => {
    const result = JSON.stringify({ answers: [{ question: "Which week?", selected: [], custom: "Hand it in as it is" }], source: "orchestrator" });
    expect(recordOf(args, result, false)).toMatchObject({ state: "answered", by: "orchestrator" });
  });

  it("says when nobody answered, and when the run stopped while it waited", () => {
    const timeout = JSON.stringify({ answers: [{ question: "Which week?", selected: [], custom: "No operator is available" }], source: "timeout" });
    expect(recordOf(args, timeout, false).state).toBe("expired");
    expect(recordOf(args, undefined, undefined).state).toBe("unanswered");
    expect(recordOf(args, "the run was cancelled", true)).toMatchObject({ state: "unanswered", raw: "the run was cancelled" });
  });

  it("keeps a result it cannot read as it came", () => {
    const record = recordOf(args, 'User has answered your questions: "Which week?"="Last week"', false);
    expect(record).toMatchObject({ state: "answered", rows: [], raw: 'User has answered your questions: "Which week?"="Last week"' });
    expect(recordOf({}, JSON.stringify({ answers: [] }), false).rows).toEqual([]);
  });
});
