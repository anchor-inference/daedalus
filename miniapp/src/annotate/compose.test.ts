// @vitest-environment jsdom
// Handing a snapshot to a conversation: the composer on screen takes it, and without one the session's
// saved draft keeps it for the next time the composer opens.

import { afterEach, describe, expect, it, vi } from "vitest";

const stored = new Map<string, File[]>();
vi.mock("../project/draftfiles", () => ({
  loadDraftFiles: async (id: string) => stored.get(id) ?? [],
  saveDraftFiles: async (id: string, files: File[]) => { stored.set(id, files); },
}));

import { COMPOSE_EVENT, appendLine, handToComposer, type ComposeRequest } from "./compose";
import { readDraft, writeDraft } from "../composer";
import { liveSnapshot, sheetSize } from "./source";

afterEach(() => {
  stored.clear();
  localStorage.clear();
});

describe("appendLine", () => {
  it("leaves a blank line after what the draft holds", () => {
    expect(appendLine("", "Page: https://example.com")).toBe("Page: https://example.com");
    expect(appendLine("look here  \n", "Page: x")).toBe("look here\n\nPage: x");
    expect(appendLine("kept", "")).toBe("kept");
  });
});

describe("handToComposer", () => {
  const file = new File(["png"], "snapshot.png", { type: "image/png" });

  it("goes to the composer that takes it, and to no draft", async () => {
    const seen: ComposeRequest[] = [];
    const take = (e: Event) => {
      const asked = (e as CustomEvent<ComposeRequest>).detail;
      seen.push({ ...asked });
      if (asked.sessionId === "s1") asked.taken = true;
    };
    window.addEventListener(COMPOSE_EVENT, take);
    try {
      expect(await handToComposer("s1", [file], "Page: x")).toBe("composer");
    } finally {
      window.removeEventListener(COMPOSE_EVENT, take);
    }
    expect(seen).toEqual([{ sessionId: "s1", files: [file], text: "Page: x", taken: false }]);
    expect(stored.has("s1")).toBe(false);
    expect(readDraft("s1")).toBe("");
  });

  it("lands in the session's saved draft when no composer is on screen", async () => {
    writeDraft("s2", "the button is wrong");
    stored.set("s2", [new File(["a"], "earlier.txt")]);
    expect(await handToComposer("s2", [file], "Page: https://example.com")).toBe("draft");
    expect(readDraft("s2")).toBe("the button is wrong\n\nPage: https://example.com");
    expect(stored.get("s2")?.map((f) => f.name)).toEqual(["earlier.txt", "snapshot.png"]);
  });
});

describe("snapshot sources", () => {
  it("copies the live frame with the page's address as the text", () => {
    const frame = { width: 640, height: 400 } as HTMLCanvasElement;
    const shot = liveSnapshot({ still: () => frame }, "https://example.com", (where) => `Page: ${where}`);
    expect(shot).toEqual({ image: frame, width: 640, height: 400, where: "https://example.com", text: "Page: https://example.com" });
  });
  it("has nothing before the first frame", () => {
    expect(liveSnapshot({ still: () => null }, "https://example.com", String)).toBeNull();
    expect(liveSnapshot(null, "https://example.com", String)).toBeNull();
  });
  it("sizes a blank sheet at the screen's density, up to twice, and never tiny", () => {
    expect(sheetSize(400, 300, 3)).toEqual({ width: 800, height: 600 });
    expect(sheetSize(400, 300, 0)).toEqual({ width: 400, height: 300 });
    expect(sheetSize(100, 50, 1)).toEqual({ width: 320, height: 200 });
  });
});
