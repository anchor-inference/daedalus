// Where a destination sits, as the More launcher's pin and the sidebar's right click change it: one
// settings write each, queued so two in a row never go out on the same revision.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resolveNav } from "./navconfig";

const calls: { method: string; body?: unknown }[] = [];
let revision = 1;
let navigation: Record<string, unknown> = {};

vi.mock("./api", () => ({
  api: {
    get: vi.fn(async () => {
      calls.push({ method: "get" });
      await new Promise((resolve) => setTimeout(resolve, 5));
      return { revision: `r${revision}`, navigation };
    }),
    put: vi.fn(async (_path: string, body: { navigation: Record<string, unknown>; base_revision: string }) => {
      calls.push({ method: "put", body });
      if (body.base_revision !== `r${revision}`) throw new Error("settings changed in another window");
      revision += 1;
      navigation = body.navigation;
      return { revision: `r${revision}`, navigation };
    }),
  },
}));
vi.mock("./store", () => ({ prime: vi.fn() }));
vi.mock("./ui/dialogs", () => ({ toast: vi.fn() }));
vi.mock("./ui", () => ({ errorText: (e: unknown) => String(e) }));

const { placeItem } = await import("./navplace");

beforeEach(() => {
  calls.length = 0;
  revision = 1;
  navigation = {};
});
afterEach(() => vi.clearAllMocks());

describe("pinning to the sidebar", () => {
  const nav = resolveNav({ config: null, pages: null, selfdev: "server" });

  it("puts the item at the end of the pinned rows and leaves the others where they were", async () => {
    expect(await placeItem(nav, "usage", "pinned")).toBe(true);
    expect(navigation.pinned).toEqual(["inbox", "board", "calendar", "changes", "terminals", "usage"]);
    expect(navigation.more).not.toContain("usage");
  });

  it("takes an item back to More without losing its place in the other lists", async () => {
    await placeItem(nav, "calendar", "more");
    expect(navigation.pinned).toEqual(["inbox", "board", "changes", "terminals"]);
    expect(navigation.more).toContain("calendar");
  });

  it("queues two writes pressed together: the second is made on the revision the first left", async () => {
    const both = await Promise.all([placeItem(nav, "usage", "pinned"), placeItem(nav, "memory", "pinned")]);
    expect(both).toEqual([true, true]);
    expect(calls.filter((c) => c.method === "put")).toHaveLength(2);
    expect(navigation.pinned).toContain("usage");
    // The second write read what the first had saved, so it kept it as well as adding its own.
    expect(navigation.pinned).toContain("memory");
  });

  it("says a refusal and resolves to false", async () => {
    revision = 7;
    const real = (await import("./api")).api.get as unknown as ReturnType<typeof vi.fn>;
    real.mockResolvedValueOnce({ revision: "r1", navigation: {} });
    expect(await placeItem(nav, "usage", "pinned")).toBe(false);
  });
});
