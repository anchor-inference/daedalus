// @vitest-environment jsdom
// The replies to a passage of an answer: the wire form, its parse back, and the replies a composer keeps.

import { beforeEach, describe, expect, it } from "vitest";
import {
  addQuoteReply,
  cleanQuote,
  clearQuoteReplies,
  forgetQuoteReplies,
  parseReplies,
  quoteReplies,
  readReplies,
  removeQuoteReply,
  replyPreview,
  repliesKey,
  serialiseReplies,
  updateQuoteReply,
  writeReplies,
} from "./quotereply";

describe("the wire form", () => {
  it("writes one reply as a numbered comment, the quote and the text, then the message", () => {
    expect(serialiseReplies([{ quote: "first line\nsecond line", text: "why this?", seq: 42 }], "and the rest")).toBe(
      "<!-- reply 1 msg=42 -->\n> first line\n> second line\n\nwhy this?\n\nand the rest",
    );
  });

  it("numbers several replies in the order they were added", () => {
    const body = serialiseReplies([{ quote: "a", text: "one", seq: 2 }, { quote: "b", text: "two", seq: null }], "");
    expect(body).toBe("<!-- reply 1 msg=2 -->\n> a\n\none\n\n<!-- reply 2 -->\n> b\n\ntwo");
  });

  it("sends replies with nothing typed in the composer", () => {
    const body = serialiseReplies([{ quote: "a", text: "one", seq: 7 }], "   ");
    expect(body.startsWith("<!-- reply 1 msg=7 -->")).toBe(true);
    expect(parseReplies(body)).toEqual({ replies: [{ quote: "a", text: "one", seq: 7 }], rest: "" });
  });

  it("keeps a blank line inside the quote as an empty quoted line", () => {
    expect(serialiseReplies([{ quote: "para one\n\npara two", text: "ok", seq: 1 }], "")).toContain("> para one\n>\n> para two");
  });

  it("folds a blank line in the reply text into a line break, since a blank line ends the reply", () => {
    const body = serialiseReplies([{ quote: "q", text: "line one\n\n\nline two", seq: 1 }], "after");
    expect(parseReplies(body)).toEqual({ replies: [{ quote: "q", text: "line one\nline two", seq: 1 }], rest: "after" });
  });

  it("is the composer's words alone when there are no replies", () => {
    expect(serialiseReplies([], "  hello  ")).toBe("hello");
  });
});

describe("reading a sent message back", () => {
  it("round-trips several replies with multi-line quotes and the message after them", () => {
    const replies = [
      { quote: "- item one\n- item two", text: "drop the second", seq: 12 },
      { quote: "```\nnpm test\n```", text: "this fails here", seq: 14 },
      { quote: "plain", text: "fine", seq: null },
    ];
    const rest = "Thanks.\n\nOne more paragraph.";
    expect(parseReplies(serialiseReplies(replies, rest))).toEqual({ replies, rest });
  });

  it("leaves a message that does not open with a marker whole", () => {
    const text = "> a quote typed by hand\n\nmy words\n<!-- reply 1 -->";
    expect(parseReplies(text)).toEqual({ replies: [], rest: text });
  });

  it("does not take a marker with no quote under it for a reply", () => {
    const text = "<!-- reply 1 -->\nno quote here";
    expect(parseReplies(text)).toEqual({ replies: [], rest: text });
  });

  it("previews a message by its quotes and words on one line", () => {
    expect(replyPreview(serialiseReplies([{ quote: "first\nsecond", text: "why?", seq: 3 }], "go on"))).toBe("«first» why? · go on");
  });
});

describe("the selection as a quote", () => {
  it("drops trailing spaces and runs of empty lines but keeps the line breaks", () => {
    expect(cleanQuote("  one  \r\n\n\n\ntwo\t\n three ")).toBe("one\n\ntwo\n three");
  });
});

describe("the replies waiting in a composer", () => {
  beforeEach(() => {
    localStorage.clear();
    forgetQuoteReplies();
  });

  it("survives a reload through storage, per chat", () => {
    addQuoteReply("chat-a", { quote: "q", text: "t", seq: 4 });
    addQuoteReply("chat-b", { quote: "other", text: "x", seq: null });
    forgetQuoteReplies();
    expect(quoteReplies("chat-a").map((reply) => [reply.quote, reply.text, reply.seq])).toEqual([["q", "t", 4]]);
    expect(quoteReplies("chat-b")).toHaveLength(1);
  });

  it("edits and removes one, and an empty list leaves no key behind", () => {
    const first = addQuoteReply("chat-a", { quote: "q1", text: "t1", seq: 1 });
    const second = addQuoteReply("chat-a", { quote: "q2", text: "t2", seq: 2 });
    updateQuoteReply("chat-a", first.id, "changed");
    removeQuoteReply("chat-a", second.id);
    expect(quoteReplies("chat-a").map((reply) => reply.text)).toEqual(["changed"]);
    clearQuoteReplies("chat-a");
    expect(localStorage.getItem(repliesKey("chat-a"))).toBeNull();
  });

  it("reads a damaged entry as nothing", () => {
    localStorage.setItem(repliesKey("chat-a"), "{not json");
    expect(readReplies("chat-a")).toEqual([]);
    localStorage.setItem(repliesKey("chat-a"), JSON.stringify([{ id: 1 }, { id: "ok", quote: "q", text: "t", seq: null }]));
    expect(readReplies("chat-a")).toEqual([{ id: "ok", quote: "q", text: "t", seq: null }]);
    writeReplies("chat-a", []);
    expect(localStorage.getItem(repliesKey("chat-a"))).toBeNull();
  });
});
