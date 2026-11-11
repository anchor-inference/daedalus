// @vitest-environment jsdom
// What agents do together over a link, as the app draws it: the answers to one question side by side, a summary
// read from either side, and what the operator's buttons send. A stranger's words stay plain text throughout.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LinkAction, LinkCard, LinkCardRow, LinkPoll } from "./api";
import { setLang } from "./i18n";

const world = vi.hoisted(() => ({ posted: [] as string[] }));
vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  api: {
    get: async () => ({}),
    post: async (path: string) => {
      world.posted.push(path);
      return {};
    },
  },
}));

import { LinkActions, LinkCardView, PollCard, Summaries, TalkPolls, actionDetail, pollLine, promiseSide } from "./linkflows";

const LATER = new Date(Date.now() + 3600_000).toISOString();
const poll = (over: Partial<LinkPoll> = {}): LinkPoll => ({
  id: "p1", question: "What does the plan cost?", state: "open", deadline_at: LATER, created_at: "", settled_at: null, woke: false, dismissed: false,
  entries: [
    { talk: "a", host: "alpha.example.test", state: "answered", answer: "Ten dollars <b>a month</b>. https://evil.example.test", answered_at: "" },
    { talk: "b", host: "beta.example.test", state: "waiting", answer: "", answered_at: null },
  ],
  ...over,
});
const SUMMARY: LinkCard = {
  type: "summary", title: "Prices", promises: [{ by: "us", what: "send the quote", due: "Monday" }, { by: "them", what: "confirm", due: "" }], links: ["https://example.test/p"], files: ["a.pdf"],
};

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  setLang("en");
  world.posted = [];
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove();
});

async function draw(node: React.ReactNode) {
  await act(async () => root.render(node));
}

describe("the answers to one question", () => {
  it("fill a table as they come, a stranger's answer as plain text", async () => {
    await draw(<PollCard poll={poll()} />);
    const rows = [...host.querySelectorAll("tbody tr")];
    expect(rows.map((row) => row.getAttribute("data-entry-state"))).toEqual(["answered", "waiting"]);
    expect(rows[0].textContent).toContain("Ten dollars <b>a month</b>.");
    expect(host.querySelector("b a, a[href], .lf-answer b")).toBeNull();
    expect(host.querySelector(".lf-poll-line")?.textContent).toMatch(/^1 of 2 answered/);
  });

  it("say when the deadline cut them short", () => {
    expect(pollLine(poll({ state: "done" }))).toBe("Deadline passed: 1 of 2 answered");
    expect(pollLine(poll({ state: "done", entries: poll().entries.map((e) => ({ ...e, state: "answered" as const })) }))).toBe("All answered");
  });

  it("stay over the composer until put away", async () => {
    await draw(<TalkPolls sessionId="s1" polls={[poll(), poll({ id: "p2", dismissed: true })]} />);
    expect(host.querySelectorAll("[data-poll]").length).toBe(1);
    await act(async () => (host.querySelector("[data-poll-dismiss]") as HTMLButtonElement).click());
    expect(world.posted).toEqual(["/api/agent-polls/p1/dismiss"]);
  });
});

describe("a summary", () => {
  it("reads right from either side of a door message", () => {
    expect(promiseSide({ by: "sender" }, true)).toBe("us");
    expect(promiseSide({ by: "sender" }, false)).toBe("them");
    expect(promiseSide({ by: "receiver" }, false)).toBe("us");
    expect(promiseSide({ by: "them" }, true)).toBe("them");
  });

  it("splits who promised what, and an address is text, not a link", async () => {
    await draw(<LinkCardView card={SUMMARY} />);
    expect(host.querySelector('[data-side="us"]')?.textContent).toContain("send the quote · by Monday");
    expect(host.querySelector('[data-side="them"]')?.textContent).toContain("confirm");
    expect(host.textContent).toContain("https://example.test/p");
    expect(host.querySelector("a")).toBeNull();
  });

  it("goes to notes or the board on the operator's button, and a new one is asked for", async () => {
    const rows: LinkCardRow[] = [{ id: 7, direction: "theirs", type: "summary", card: SUMMARY, at: "" }];
    const refresh = vi.fn();
    await draw(<Summaries rows={rows} path="/api/agent-talks/t1" refresh={refresh} toast={() => undefined} />);
    expect(host.querySelector("[data-summary]")?.getAttribute("data-direction")).toBe("theirs");
    for (const button of ["[data-summary-note]", "[data-summary-board]", "[data-summary-make]"]) {
      await act(async () => (host.querySelector(button) as HTMLButtonElement).click());
    }
    expect(world.posted).toEqual(["/api/link-cards/7/note", "/api/link-cards/7/board", "/api/agent-talks/t1/summary"]);
    expect(refresh).toHaveBeenCalledTimes(3);
  });
});

describe("what a link's turns asked for", () => {
  const action = (over: Partial<LinkAction>): LinkAction => ({ id: "a1", kind: "patch", state: "held", result: "", created_at: "", expires_at: null, ...over });

  it("names each kind by what it is about", () => {
    expect(actionDetail(action({ branch: "fix/x", stat: "1 file(s), +2 −1" }))).toBe("fix/x · 1 file(s), +2 −1");
    expect(actionDetail(action({ kind: "service", name: "web" }))).toBe("web");
  });

  it("lets the operator close a service a guest was shown, and nothing else", async () => {
    await draw(<LinkActions actions={[action({ kind: "service", state: "done", name: "web" }), action({ id: "a2", state: "done" })]} refresh={() => undefined} toast={() => undefined} />);
    const buttons = host.querySelectorAll("button");
    expect(buttons.length).toBe(1);
    await act(async () => (buttons[0] as HTMLButtonElement).click());
    expect(world.posted).toEqual(["/api/link-actions/a1/revoke"]);
  });
});
