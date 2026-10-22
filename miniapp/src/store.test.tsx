// @vitest-environment jsdom
// A first read that fails is tried again within seconds, not at the next poll, and a screen that
// mounts while another reader's request is on the way shows its answer.

import { act, useLayoutEffect } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api";
import { invalidate, prime, retryDelay, useQuery } from "./store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function Member({ path, pollMs }: { path: string; pollMs: number }) {
  const { data } = useQuery<{ name: string }>(path, { pollMs, staleMs: 3000 });
  return <b>{data ? data.name : "loading"}</b>;
}

describe("the retry of a failed first read", () => {
  it("waits a second, then twice as long, up to eight seconds and never past the poll", () => {
    expect([1, 2, 3, 4, 5, 9].map((n) => retryDelay(n, 0))).toEqual([1000, 2000, 4000, 8000, 8000, 8000]);
    expect(retryDelay(4, 5000)).toBe(5000);
    expect(retryDelay(1, 15000)).toBe(1000);
  });
});

describe("a screen whose first read failed", () => {
  let box: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  beforeEach(() => {
    vi.useFakeTimers();
    box = document.createElement("div");
    root = createRoot(box);
  });
  afterEach(() => {
    act(() => root.unmount());
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  async function settle(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it("shows its data a second after a dropped request, not at the 15-second poll", async () => {
    const get = vi.spyOn(api, "get").mockRejectedValueOnce(new TypeError("Failed to fetch")).mockResolvedValue({ name: "Ira" });
    act(() => root.render(<Member path="/api/staff/one" pollMs={15000} />));
    await settle(10);
    expect(box.textContent).toBe("loading");
    await settle(1000);
    expect(get).toHaveBeenCalledTimes(2);
    expect(box.textContent).toBe("Ira");
    // Once it has data, only the poll reads it again.
    await settle(8000);
    expect(get).toHaveBeenCalledTimes(2);
  });

  it("keeps trying while the host is down, backing off", async () => {
    const get = vi.spyOn(api, "get").mockRejectedValue(new ApiError(502, "Server unavailable (502)"));
    act(() => root.render(<Member path="/api/staff/two" pollMs={0} />));
    await settle(10);
    await settle(1000);
    await settle(2000);
    await settle(4000);
    expect(get).toHaveBeenCalledTimes(4);
  });

  it("does not retry a refusal: a 404 is an answer", async () => {
    const get = vi.spyOn(api, "get").mockRejectedValue(new ApiError(404, "Not Found"));
    act(() => root.render(<Member path="/api/staff/three" pollMs={0} />));
    await settle(20000);
    expect(get).toHaveBeenCalledTimes(1);
  });
});

describe("a change made while a read is on the way", () => {
  let box: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  beforeEach(() => {
    box = document.createElement("div");
    root = createRoot(box);
  });
  afterEach(() => {
    act(() => root.unmount());
    vi.restoreAllMocks();
  });

  it("reads again rather than taking the answer that was asked for before it", async () => {
    // The list asked before the chat was made answers without it; the read the change asks for
    // must be a new request whose answer wins, not the old one joined.
    let answerOld: (value: { name: string }) => void = () => {};
    const get = vi.spyOn(api, "get")
      .mockImplementationOnce(() => new Promise((resolve) => { answerOld = resolve as typeof answerOld; }))
      .mockResolvedValue({ name: "with the new chat" });
    act(() => root.render(<Member path="/api/sessions?view=race" pollMs={0} />));
    await act(async () => { invalidate("/api/sessions"); });
    expect(get).toHaveBeenCalledTimes(2);
    expect(box.textContent).toBe("with the new chat");
    await act(async () => { answerOld({ name: "before the chat" }); });
    expect(box.textContent).toBe("with the new chat");
  });
});

describe("a screen that mounts while another reader's request is on the way", () => {
  let box: HTMLDivElement;
  let root: ReturnType<typeof createRoot>;
  beforeEach(() => {
    box = document.createElement("div");
    root = createRoot(box);
  });
  afterEach(() => {
    act(() => root.unmount());
    vi.restoreAllMocks();
  });

  it("shows an answer that lands after it rendered but before its effects ran", () => {
    // A layout effect runs after the commit and before the passive effects, which is where a late
    // lazily loaded screen sat when the header's request for the same key answered. The screen then
    // finds the entry fresh, asks for nothing, and must still show it rather than "loading".
    const get = vi.spyOn(api, "get").mockResolvedValue({ name: "from a second request" });
    function Answer() {
      useLayoutEffect(() => prime("/api/projects/p1/budget?gap", { name: "Ira" }), []);
      return null;
    }
    act(() => root.render(<><Member path="/api/projects/p1/budget?gap" pollMs={0} /><Answer /></>));
    expect(get).not.toHaveBeenCalled();
    expect(box.textContent).toBe("Ira");
  });
});
