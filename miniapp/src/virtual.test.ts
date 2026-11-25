// @vitest-environment jsdom
// When a conversation lets go of its end. The orchestrator's chat once opened in the middle every
// time: the pin's own scroll event arrived after the windowed history had grown under it, read as the
// reader leaving, and nothing pinned it again.

import { describe, expect, it } from "vitest";
import { heldGuess, keepOpened, openedLine, stillAtEnd } from "./virtual";

describe("the pin to the end", () => {
  it("holds through the pin's own scroll, even when the history grew before the event came", () => {
    expect(stillAtEnd({ pinned: true, gap: 26_899, top: 16_807, lastTop: 0, hand: false })).toBe(true);
    expect(stillAtEnd({ pinned: true, gap: 900, top: 4_000, lastTop: 4_000, hand: false })).toBe(true);
  });

  it("lets go when the reader moves up, or has a hand on the list", () => {
    expect(stillAtEnd({ pinned: true, gap: 600, top: 3_400, lastTop: 4_000, hand: false })).toBe(false);
    expect(stillAtEnd({ pinned: true, gap: 600, top: 4_200, lastTop: 4_000, hand: true })).toBe(false);
  });

  it("takes the end again whenever the reader is near it, and stays let go on the way down until then", () => {
    expect(stillAtEnd({ pinned: false, gap: 20, top: 9_000, lastTop: 8_000, hand: true })).toBe(true);
    expect(stillAtEnd({ pinned: false, gap: 800, top: 8_000, lastTop: 7_000, hand: false })).toBe(false);
  });
});

// Opening a step of an earlier turn once threw the reader down the chat: the list's growth was taken
// for new output to follow. The line the reader opened is held where it was on the screen instead.
describe("a line the reader opened", () => {
  function at(el: Element, top: number) {
    el.getBoundingClientRect = () => ({ top, bottom: top + 20, left: 0, right: 0, width: 0, height: 20, x: 0, y: top, toJSON: () => ({}) });
  }
  function chat() {
    const host = document.createElement("div");
    host.innerHTML = `<div class="turn"><button class="thinking-head" aria-expanded="false"><span class="worked">Worked for 9s</span></button><a href="#m1">link</a><div class="act prose"><p>thinking</p></div></div>`;
    document.body.append(host);
    at(host, 100);
    return host;
  }

  it("is the toggle a click landed in, wherever inside it", () => {
    const host = chat();
    const head = host.querySelector(".thinking-head")!;
    at(head, 340);
    expect(openedLine(host.querySelector(".worked"), host, 0)).toEqual({ el: head, top: 240, until: 800 });
    expect(openedLine(host.querySelector(".act p"), host, 0)?.el).toBe(host.querySelector(".act"));
  });

  it("is nothing for a link, or for a click outside the conversation", () => {
    const host = chat();
    expect(openedLine(host.querySelector("a"), host, 0)).toBeNull();
    const outside = document.createElement("button");
    outside.setAttribute("aria-expanded", "false");
    document.body.append(outside);
    expect(openedLine(outside, host, 0)).toBeNull();
  });

  it("is put back where it was while the list grows, and let go after a moment or once it is gone", () => {
    const host = chat();
    const head = host.querySelector(".thinking-head")!;
    at(head, 340);
    const line = openedLine(head, host, 0)!;
    host.scrollTop = 5_000;
    // The spacer above grew by 186 px and pushed the line down by as much.
    at(head, 526);
    expect(keepOpened(host, line, 100)).toBe(true);
    expect(host.scrollTop).toBe(5_186);
    at(head, 340);
    expect(keepOpened(host, line, 200)).toBe(true);
    expect(host.scrollTop).toBe(5_186);
    expect(keepOpened(host, line, 900)).toBe(false);
    head.remove();
    expect(keepOpened(host, line, 100)).toBe(false);
  });
});

// A reader a few screens from the end of a long history was thrown to the end by the list itself: the
// turns the jump rendered were measured, the average moved, and every unseen turn above them with it.
describe("the height guessed for turns nobody has seen", () => {
  it("follows what has been measured while nobody drives the list", () => {
    expect(heldGuess(340, 420, false)).toBe(340);
    expect(heldGuess(340, null, false)).toBe(340);
  });

  it("stays where it was while a hand is on the list, and is taken once there is none to hold", () => {
    expect(heldGuess(340, 420, true)).toBe(420);
    expect(heldGuess(340, null, true)).toBe(340);
  });
});
