// @vitest-environment jsdom
// A chunk fetched ahead renders in the render that asks for it, without its fallback first.

import { Suspense, act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it, vi } from "vitest";
import { chunk, chunkVerdict, failedModule, mayReload, retried } from "./chunks";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function Page({ word }: { word: string }) {
  return <b>{word}</b>;
}

/** What the first commit of a render shows, before any promise has had a tick to answer. */
function firstCommit(Screen: ReturnType<typeof chunk<typeof Page>>): string {
  const box = document.createElement("div");
  const root = createRoot(box);
  act(() => root.render(<Suspense fallback={<i>loading</i>}><Screen word="ready" /></Suspense>));
  const shown = box.textContent ?? "";
  act(() => root.unmount());
  return shown;
}

describe("chunk", () => {
  it("draws the fallback when the click has to fetch the module", () => {
    const Screen = chunk(() => Promise.resolve({ default: Page }));
    expect(firstCommit(Screen)).toBe("loading");
  });

  it("draws the screen at once once the module was fetched ahead", async () => {
    const Screen = chunk(() => Promise.resolve({ default: Page }));
    Screen.prefetch();
    await Promise.resolve();
    await Promise.resolve();
    expect(firstCommit(Screen)).toBe("ready");
  });

  it("fetches again after a prefetch that failed", async () => {
    let calls = 0;
    const Screen = chunk(() => (++calls === 1 ? Promise.reject(new Error("offline")) : Promise.resolve({ default: Page })));
    Screen.prefetch();
    await new Promise((r) => setTimeout(r, 0));
    Screen.prefetch();
    await new Promise((r) => setTimeout(r, 0));
    expect(calls).toBe(2);
    expect(firstCommit(Screen)).toBe("ready");
  });

});

describe("retried", () => {
  const CHROMIUM = "Failed to fetch dynamically imported module: https://someone.example/app/assets/Board-a1b2.js";

  /** Runs the loader with the pauses between attempts skipped. */
  async function settled<T>(run: () => Promise<T>): Promise<T | Error> {
    vi.useFakeTimers();
    try {
      const result = run().catch((e: Error) => e);
      await vi.runAllTimersAsync();
      return await result;
    } finally {
      vi.useRealTimers();
    }
  }

  it("names the module the browsers name, without its query", () => {
    expect(failedModule(new TypeError(CHROMIUM))).toBe("https://someone.example/app/assets/Board-a1b2.js");
    expect(failedModule(new TypeError(`${CHROMIUM.replace(".js", ".js?retry=1")}`))).toBe("https://someone.example/app/assets/Board-a1b2.js");
    expect(failedModule(new TypeError("error loading dynamically imported module: http://127.0.0.1:8163/app/assets/Voice-9f.js"))).toBe("http://127.0.0.1:8163/app/assets/Voice-9f.js");
    expect(failedModule(new TypeError("Importing a module script failed."))).toBeNull();
  });

  // Chromium keeps the failed module as failed: the same address again rejects without a request,
  // so the only retry that reaches the network is one under another address.
  it("asks for a module the browser named under a new address, and never replays the failed one", async () => {
    let loads = 0;
    const asked: string[] = [];
    const load = retried(
      () => (++loads, Promise.reject(new TypeError(CHROMIUM))),
      (m: { Board: string }) => m.Board,
      (url) => {
        asked.push(url);
        if (asked.length === 1) return Promise.reject(new TypeError(`Failed to fetch dynamically imported module: ${url}`));
        return Promise.resolve({ Board: "board" });
      },
    );
    expect(await settled(load)).toBe("board");
    expect(loads).toBe(1);
    expect(asked).toEqual(["https://someone.example/app/assets/Board-a1b2.js?retry=1", "https://someone.example/app/assets/Board-a1b2.js?retry=2"]);
  });

  it("calls the loader again when the error names no module", async () => {
    let calls = 0;
    const load = retried(() => (++calls < 2 ? Promise.reject(new Error("flaky")) : Promise.resolve({ v: "module" })), (m) => m.v, () => Promise.reject(new Error("not asked")));
    expect(await settled(load)).toBe("module");
    expect(calls).toBe(2);
  });

  it("gives up after three attempts with the last error, which the boundary reads as a build that is gone", async () => {
    const asked: string[] = [];
    const load = retried(() => Promise.reject(new TypeError(CHROMIUM)), (m: unknown) => m, (url) => (asked.push(url), Promise.reject(new TypeError(`Failed to fetch dynamically imported module: ${url}`))));
    const result = await settled(load);
    expect(result).toBeInstanceOf(TypeError);
    expect((result as Error).message).toContain("dynamically imported module");
    expect(asked).toHaveLength(2);
  });
});

describe("a chunk that would not load", () => {
  const failed = new TypeError("Failed to fetch dynamically imported module: https://someone.example/app/assets/Board-a1b2.js?retry=2");

  it("is a dropped download when the server still has the file, and a moved build when it has not", async () => {
    const asked: string[] = [];
    const answer = (status: number) => (url: string) => (asked.push(url), Promise.resolve(new Response(null, { status })));
    expect(await chunkVerdict(failed, answer(200))).toBe("dropped");
    expect(await chunkVerdict(failed, answer(404))).toBe("stale");
    expect(asked).toEqual(["https://someone.example/app/assets/Board-a1b2.js", "https://someone.example/app/assets/Board-a1b2.js"]);
    expect(await chunkVerdict(failed, () => Promise.reject(new TypeError("Failed to fetch")))).toBe("offline");
    expect(await chunkVerdict(new TypeError("Importing a module script failed."), answer(200))).toBe("stale");
  });

  it("reloads the page once a minute at most, and not at all when that cannot be counted", () => {
    const kept = new Map<string, string>();
    const storage = { getItem: (k: string) => kept.get(k) ?? null, setItem: (k: string, v: string) => void kept.set(k, v) };
    expect(mayReload(storage, 1_000_000)).toBe(true);
    expect(mayReload(storage, 1_030_000)).toBe(false);
    expect(mayReload(storage, 1_061_000)).toBe(true);
    const refusing = { getItem: () => { throw new Error("denied"); }, setItem: () => undefined };
    expect(mayReload(refusing, 1_000_000)).toBe(false);
    expect(mayReload(null, 1_000_000)).toBe(false);
  });
});
