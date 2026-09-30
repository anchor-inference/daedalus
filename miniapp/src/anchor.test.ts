import { describe, expect, it } from "vitest";
import { anchorHash, parseAnchor, snippetParts, turnFor, withOlder } from "./anchor";

describe("parseAnchor", () => {
  it("reads the message a hash points at", () => {
    expect(parseAnchor("#m123")).toBe(123);
    expect(parseAnchor("m7")).toBe(7);
    expect(parseAnchor(anchorHash(4051))).toBe(4051);
  });

  it("ignores every other hash", () => {
    expect(parseAnchor("")).toBeNull();
    expect(parseAnchor(null)).toBeNull();
    expect(parseAnchor("#settings")).toBeNull();
    expect(parseAnchor("#m")).toBeNull();
    expect(parseAnchor("#m12a")).toBeNull();
    expect(parseAnchor("#details")).toBeNull();
  });
});

describe("turnFor", () => {
  const keys = ["u100", "a101", "u110", "s150", "u160"];

  it("finds the turn that opened with the message", () => {
    expect(turnFor(keys, 110)).toBe(2);
  });

  it("finds the turn that shows a message inside it", () => {
    // A tool's output in the middle of the turn that began at 110.
    expect(turnFor(keys, 131)).toBe(2);
    expect(turnFor(keys, 999)).toBe(4);
  });

  it("puts a message before every turn on the first one", () => {
    expect(turnFor(keys, 90)).toBe(0);
    expect(turnFor([], 90)).toBe(-1);
  });

  it("skips keys that carry no seq", () => {
    expect(turnFor(["u100", "live"], 120)).toBe(0);
  });
});

describe("snippetParts", () => {
  it("marks the words the host bracketed", () => {
    expect(snippetParts("the [sourdough] starter and [rye]")).toEqual([
      { text: "the ", hit: false },
      { text: "sourdough", hit: true },
      { text: " starter and ", hit: false },
      { text: "rye", hit: true },
    ]);
  });

  it("leaves a snippet without marks whole", () => {
    expect(snippetParts("plain words")).toEqual([{ text: "plain words", hit: false }]);
    expect(snippetParts("")).toEqual([]);
  });
});

describe("withOlder", () => {
  const m = (seq: number, live = false) => ({ seq, live });

  it("keeps the older pages in front of a fresh newest page", () => {
    expect(withOlder([m(1), m(2), m(3), m(4)], [m(3), m(4), m(5)]).map((x) => x.seq)).toEqual([1, 2, 3, 4, 5]);
  });

  it("takes the page as it came when nothing older is held", () => {
    const page = [m(3), m(4)];
    expect(withOlder([m(3)], page)).toBe(page);
    expect(withOlder([], page)).toBe(page);
  });

  it("does not join the old pages across a hole", () => {
    expect(withOlder([m(1), m(2)], [m(10), m(11)]).map((x) => x.seq)).toEqual([10, 11]);
  });

  it("empties with a history that was cleared", () => {
    expect(withOlder([m(1), m(2)], [])).toEqual([]);
  });

  it("leaves out what was only streaming", () => {
    expect(withOlder([m(1), m(2), m(3, true)], [m(2), m(3)]).map((x) => x.seq)).toEqual([1, 2, 3]);
  });
});
