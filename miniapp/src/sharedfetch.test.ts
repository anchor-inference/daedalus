import { describe, expect, it, vi } from "vitest";
import { createSharedFetch } from "./sharedfetch";

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

describe("one download per file", () => {
  it("lets a second asker join the request under way instead of starting another", async () => {
    const shared = createSharedFetch<string>();
    const gate = deferred<string>();
    const start = vi.fn(() => gate.promise);
    const a = shared.join("/f", start);
    const b = shared.join("/f", start);
    expect(start).toHaveBeenCalledTimes(1);
    gate.resolve("bytes");
    await expect(a.promise).resolves.toBe("bytes");
    await expect(b.promise).resolves.toBe("bytes");
  });

  it("keeps nothing once it has finished, so a reload reads the file again", async () => {
    const shared = createSharedFetch<string>();
    const start = vi.fn(async () => "bytes");
    await shared.join("/f", start).promise;
    await shared.join("/f", start).promise;
    expect(start).toHaveBeenCalledTimes(2);
    expect(shared.inFlight()).toBe(0);
  });

  it("aborts only when the last asker has gone", () => {
    const shared = createSharedFetch<string>();
    let signal: AbortSignal | null = null;
    const start = (s: AbortSignal) => { signal = s; return new Promise<string>(() => undefined); };
    const a = shared.join("/f", start);
    const b = shared.join("/f", start);
    a.release();
    expect(signal!.aborted).toBe(false);
    b.release();
    b.release();
    expect(signal!.aborted).toBe(true);
    // Whoever comes next starts afresh rather than joining the cancelled one.
    const again = vi.fn(() => new Promise<string>(() => undefined));
    shared.join("/f", again);
    expect(again).toHaveBeenCalledTimes(1);
  });

  it("tells every asker the progress, a late one where it has got to", () => {
    const shared = createSharedFetch<string>();
    let report: (v: number | null) => void = () => undefined;
    const start = (_: AbortSignal, progress: (v: number | null) => void) => { report = progress; return new Promise<string>(() => undefined); };
    const first: (number | null)[] = [];
    const second: (number | null)[] = [];
    shared.join("/f", start, (v) => first.push(v));
    report(0.5);
    shared.join("/f", start, (v) => second.push(v));
    report(1);
    expect(first).toEqual([0.5, 1]);
    expect(second).toEqual([0.5, 1]);
  });

  it("keeps different files apart", () => {
    const shared = createSharedFetch<string>();
    const start = vi.fn(() => new Promise<string>(() => undefined));
    shared.join("/a", start);
    shared.join("/b", start);
    expect(start).toHaveBeenCalledTimes(2);
  });
});
