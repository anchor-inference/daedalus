// @vitest-environment jsdom
// An asked question as the transcript draws it: a step where it was asked, between the steps before
// and after it, one line that says what was chosen, and its answers on opening — never the JSON the
// host and the model exchange. It once sat after all of the turn's steps, however early it was asked.
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import type { MessageView, Question, SessionDetail } from "../api";
import { setLang } from "../i18n";
import { TranscriptView } from "../screens/Session";
import { createLiveStore } from "../turns";

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setLang("en");
  localStorage.clear();
  // jsdom has no ResizeObserver; the operator's bubble measures itself with one.
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} };
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

const RELEASE: Question = { question: "Release 0.4.4 now (push and tag v0.4.4)? It carries the new characters and the fixed loader.", options: [{ label: "Yes, push it (Recommended)" }, { label: "Not yet" }] };
const STORE: Question = { question: "Which stores should it go to?", header: "Stores", multiSelect: true, options: [{ label: "Mozilla" }, { label: "Chrome" }] };

function row(seq: number, role: MessageView["role"], extra: Partial<MessageView> = {}): MessageView {
  return { role, seq, text: "", thinking: "", tool_calls: [], tool_results: [], created_at: `2026-10-28T10:00:${String(seq).padStart(2, "0")}Z`, run_id: "r1", ...extra };
}

function call(seq: number, id: string, name: string, args: Record<string, unknown>): MessageView {
  return row(seq, "assistant", { tool_calls: [{ id, name, arguments: args }] });
}

function result(seq: number, id: string, content: string): MessageView {
  return row(seq, "tool", { tool_results: [{ id, content, is_error: false }] });
}

const answer = (answers: { question: string; selected: string[]; custom: string | null }[]) => JSON.stringify({ answers, source: "user" });

function detail(messages: MessageView[], pending: Question[] | null = null): SessionDetail {
  return { id: "s1", title: "t", status: pending ? "waiting" : "idle", messages, pending: pending ? { questions: pending } : null } as unknown as SessionDetail;
}

async function draw(d: SessionDetail) {
  await act(async () => root.render(<TranscriptView detail={d} live={createLiveStore()} onPreview={() => undefined} toast={() => undefined} />));
  // The turn's steps are folded once its run is over; the check is about what is inside them.
  for (const head of [...host.querySelectorAll<HTMLButtonElement>(".thinking-head")]) {
    if (head.getAttribute("aria-expanded") !== "true") await act(async () => head.click());
  }
}

/** Every step of the turn in the order drawn: the question's line, or the tool's verb and detail. */
function steps(): string[] {
  return [...host.querySelectorAll(".activity > .act-wrap")].map((el) => (el.classList.contains("ask-record") ? `ask:${el.getAttribute("data-state")}` : el.querySelector(".verb")?.textContent ?? ""));
}

const TURN = [
  row(1, "user", { text: "Ship it." }),
  call(2, "c1", "Exec", { command: "npm test" }),
  result(3, "c1", "ok"),
  call(4, "q1", "AskUser", { questions: [RELEASE] }),
  result(5, "q1", answer([{ question: RELEASE.question, selected: ["Yes, push it (Recommended)"], custom: null }])),
  call(6, "c2", "Read", { path: "README.md" }),
  result(7, "c2", "# readme"),
  call(8, "q2", "AskUser", { questions: [RELEASE, STORE] }),
  result(9, "q2", answer([{ question: RELEASE.question, selected: ["Not yet"], custom: null }, { question: STORE.question, selected: ["Mozilla"], custom: "and Edge" }])),
  call(10, "c3", "Exec", { command: "git tag v0.4.4" }),
  result(11, "c3", "done"),
  row(12, "assistant", { text: "Released." }),
];

describe("an asked question in the transcript", () => {
  it("sits among the turn's steps where it was asked, not after them", async () => {
    await draw(detail(TURN));
    expect(steps()).toEqual(["Ran command", "ask:answered", "Read file", "ask:answered", "Ran command"]);
    // Nothing of it is left outside the steps, under the work.
    expect(host.querySelectorAll(".ask-record").length).toBe(2);
    expect([...host.querySelectorAll(".ask-record")].every((el) => el.closest(".activity"))).toBe(true);
  });

  it("reads as one line of the question and the choice, and never as JSON", async () => {
    await draw(detail(TURN));
    const [one, two] = [...host.querySelectorAll(".ask-record")];
    const line = one.querySelector(".act")!.textContent!;
    expect(line).toContain("Asked you");
    expect(line).toContain("Release 0.4.4 now");
    expect(one.querySelector(".ask-record-head-a")!.textContent).toBe("Yes, push it");
    // Several questions: how many, then the first with its answer.
    expect(two.querySelector(".ask-record-count")!.textContent).toBe("2 questions");
    expect(two.querySelector(".ask-record-head-a")!.textContent).toBe("Not yet");
    expect(host.textContent).not.toContain("{\"answers\"");
    expect(host.textContent).not.toContain("\"selected\"");
  });

  it("opens to every question with what was chosen and what was typed", async () => {
    await draw(detail(TURN));
    const two = host.querySelectorAll(".ask-record")[1];
    expect(two.querySelector(".ask-record-body")).toBeNull();
    await act(async () => (two.querySelector(".act") as HTMLElement).click());
    const rows = [...two.querySelectorAll(".ask-record-row")];
    expect(rows.map((r) => r.querySelector(".ask-record-q")!.textContent)).toEqual([RELEASE.question, STORE.question]);
    expect(rows.map((r) => r.querySelector(".ask-record-a")!.textContent)).toEqual(["Not yet", "Mozilla, and Edge"]);
    expect(rows[1].querySelector(".ask-record-tag")!.textContent).toBe("Stores");
    expect(two.textContent).not.toContain("{");
  });

  it("says a question still waiting is waiting, in its place, while its card is above the composer", async () => {
    await draw(detail([...TURN.slice(0, 4)], [RELEASE]));
    const waiting = host.querySelector(".ask-record[data-state='waiting']");
    expect(waiting).not.toBeNull();
    expect(waiting!.querySelector(".ask-record-status")!.textContent).toBe("waiting for your answer");
    expect(waiting!.querySelector(".ask-record-head-a")).toBeNull();
  });

  it("lists the questions of an answer that reached it cut short, without the cut JSON", async () => {
    const whole = answer([{ question: RELEASE.question, selected: ["Yes, push it (Recommended)"], custom: null }]);
    await draw(detail([row(1, "user", { text: "Ship it." }), call(2, "q1", "AskUser", { questions: [RELEASE] }), result(3, "q1", whole.slice(0, 60)), row(4, "assistant", { text: "Done." })]));
    const record = host.querySelector(".ask-record")!;
    await act(async () => (record.querySelector(".act") as HTMLElement).click());
    expect(record.textContent).not.toContain("{\"answers\"");
    expect(record.querySelector(".ask-record-q")!.textContent).toBe(RELEASE.question);
    expect(record.textContent).toContain("cut short");
  });

  it("reads in Russian", async () => {
    setLang("ru");
    await draw(detail(TURN));
    const two = host.querySelectorAll(".ask-record")[1];
    expect(two.querySelector(".verb")!.textContent).toBe("Спросил вас");
    expect(two.querySelector(".ask-record-count")!.textContent).toBe("2 вопроса");
  });
});
