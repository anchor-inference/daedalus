// @vitest-environment jsdom
// The question card as the operator meets it: one question at a time with the chips of a batch,
// the button that says what it does and stays off with a reason, the digits and Enter, Other's field
// only once Other is chosen, and the review that sends every answer in the order asked.
import { act, createRef } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Question } from "../api";
import { setLang } from "../i18n";
import { AskCard, type AskCardHandle } from "./AskCard";
import type { Answer } from "./ask";

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setLang("en");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

const WEEK: Question = { question: "Which week?", header: "Week", options: [{ label: "Last week (Recommended)", description: "every item is final" }, { label: "This week" }], allow_custom: true };
const SEND: Question = { question: "Send it?", header: "Send", options: [{ label: "Yes" }, { label: "No" }] };
const SUBJECT: Question = { question: "Subject line?", header: "Subject", allow_custom: true };

async function draw(questions: Question[], onSend = vi.fn(async (_answers: Answer[]) => undefined)) {
  const ref = createRef<AskCardHandle>();
  await act(async () => root.render(<AskCard ref={ref} questions={questions} phone={false} onSend={onSend} toast={() => undefined} />));
  return { ref, onSend };
}

const $ = (selector: string) => host.querySelector<HTMLElement>(selector);
const $$ = (selector: string) => Array.from(host.querySelectorAll<HTMLElement>(selector));
const primary = () => $(".ask-go")!;
const hint = () => $(".ask-hint")!.textContent ?? "";
const field = () => host.querySelector("textarea");

async function click(el: HTMLElement) {
  await act(async () => el.click());
}

async function key(el: HTMLElement, k: string) {
  await act(async () => { el.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })); });
}

async function type(el: HTMLTextAreaElement, text: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(el, text);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

describe("one question", () => {
  it("draws its header as a chip, tags the recommended option and sends the pick verbatim", async () => {
    const { onSend } = await draw([WEEK]);
    expect($(".ask-chip")!.textContent).toBe("Week");
    expect($(".ask-text")!.textContent).toBe("Which week?");
    expect($$(".ask-steps").length).toBe(0);
    const first = $$(".ask-opt")[0];
    expect(first.querySelector(".ask-rec")).not.toBeNull();
    expect(first.querySelector(".ask-opt-label")!.textContent).not.toContain("(Recommended)");
    expect(primary().textContent).toBe("Send answer");
    expect(primary().getAttribute("aria-disabled")).toBe("true");
    expect(hint()).toBe("Pick one to continue");
    await click(primary());
    expect(onSend).not.toHaveBeenCalled();
    await click(first.querySelector("input")!);
    expect(primary().getAttribute("aria-disabled")).toBe("false");
    await click(primary());
    expect(onSend).toHaveBeenCalledWith([{ question: "Which week?", selected: ["Last week (Recommended)"], custom: null }]);
  });

  it("opens Other's field only once Other is chosen, and wants words in it", async () => {
    const { onSend } = await draw([WEEK]);
    expect($("textarea")).toBeNull();
    const input = $$(".ask-opt input")[0];
    await key(input, "3");
    expect(host.querySelector(".ask-other-field textarea")).not.toBeNull();
    expect(hint()).toBe("Write your own answer, or pick an option");
    await type(field()!, "the last full one");
    await key(field()!, "Enter");
    expect(onSend).toHaveBeenCalledWith([{ question: "Which week?", selected: [], custom: "the last full one" }]);
  });

  it("draws a yes/no as a pair and a free-text question as its field alone", async () => {
    await draw([SEND]);
    expect($(".ask-options")!.dataset.layout).toBe("pair");
    expect($$(".ask-mark").length).toBe(0);
    await draw([SUBJECT]);
    expect($$(".ask-opt").length).toBe(0);
    expect($("textarea.ask-field")).not.toBeNull();
    expect(hint()).toBe("Write your answer to continue");
  });
});

describe("a batch", () => {
  it("walks one question at a time to a review that names what is missing, then sends them all", async () => {
    const { ref, onSend } = await draw([WEEK, SEND, SUBJECT]);
    expect($$(".ask-step").length).toBe(4);
    expect($(".ask-count")!.textContent).toBe("1 of 3");
    expect(primary().dataset.go).toBe("next");
    // The composer's Reply circle does what the card's button does.
    await click($$(".ask-opt input")[1]);
    await act(async () => ref.current!.go());
    expect($(".ask")!.dataset.step).toBe("1");
    expect($$(".ask-step")[0].dataset.state).toBe("done");
    // Jump to the last by its chip; it leads to the review even though one is left.
    await click($$(".ask-step")[2]);
    await type(field()!, "Week 41");
    expect(primary().dataset.go).toBe("review");
    await click(primary());
    expect($(".ask")!.dataset.step).toBe("review");
    expect($$(".ask-review-row").length).toBe(3);
    expect($$(".ask-review-row.missing").length).toBe(1);
    expect(hint()).toBe("One question left: Send");
    expect(primary().getAttribute("aria-disabled")).toBe("true");
    // Answer the missing one from the review's own link and come back.
    await click($(".ask-review-row.missing .ask-review-edit")!);
    expect($(".ask")!.dataset.step).toBe("1");
    await click($$(".ask-opt input")[0]);
    await click($(".ask-step.review")!);
    expect(primary().textContent).toBe("Send answers");
    await click(primary());
    expect(onSend).toHaveBeenCalledWith([
      { question: "Which week?", selected: ["This week"], custom: null },
      { question: "Send it?", selected: ["Yes"], custom: null },
      { question: "Subject line?", selected: [], custom: "Week 41" },
    ]);
  });

  it("goes back without losing what was chosen", async () => {
    await draw([WEEK, SEND]);
    await click($$(".ask-opt input")[0]);
    await click(primary());
    await click($(".ask-back")!);
    expect($(".ask")!.dataset.step).toBe("0");
    expect($$(".ask-opt.on").length).toBe(1);
  });
});
