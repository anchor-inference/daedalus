// @vitest-environment jsdom
// A first read that fails is tried again within seconds, not at the next poll.

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "./api";
import { retryDelay, useQuery } from "./store";

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
