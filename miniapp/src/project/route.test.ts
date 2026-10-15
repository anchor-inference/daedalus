import { describe, expect, it } from "vitest";
import { moved, nextRuntime } from "./ProjectRoute";

describe("member route editor", () => {
  it("offers the first runtime the route does not name yet", () => {
    expect(nextRuntime([])).toBe("claude");
    expect(nextRuntime([{ runtime: "claude", model: "" }, { runtime: "codex", model: "" }])).toBe("grok");
  });

  it("offers nothing once every runtime is in the route", () => {
    const all = (["claude", "codex", "grok", "opencode", "pi", "cursor", "daedalus"] as const).map((runtime) => ({ runtime, model: "" }));
    expect(nextRuntime(all)).toBeNull();
  });

  it("moves an entry within the route and never past its ends", () => {
    const route = [{ runtime: "claude" as const, model: "opus" }, { runtime: "codex" as const, model: "" }];
    expect(moved(route, 1, -1).map((e) => e.runtime)).toEqual(["codex", "claude"]);
    expect(moved(route, 0, -1)).toBe(route);
    expect(moved(route, 1, 1)).toBe(route);
  });
});
