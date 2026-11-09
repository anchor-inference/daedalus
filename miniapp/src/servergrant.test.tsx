// @vitest-environment jsdom
// "Allow all from <server>": the span chosen on the card is the one granted, "forever" is asked about
// once more, and the session's list says how long each grant has left and takes one back.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { leftLabel, ServerGrantButton, StandingGrants } from "./servergrant";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;
beforeEach(() => {
  setLang("en");
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.restoreAllMocks();
});

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
const flush = () => act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); });

describe("the button on the approval", () => {
  it("grants six hours unless another span is chosen", async () => {
    const granted: number[] = [];
    await act(async () => root.render(<ServerGrantButton server="mobile-mcp" onGrant={(hours) => granted.push(hours)} />));
    expect(host.textContent).toContain("Allow all from mobile-mcp");
    const select = host.querySelector("select")!;
    expect(select.value).toBe("6");
    expect([...select.options].map((o) => o.value)).toEqual(["1", "2", "6", "12", "0"]);
    await act(async () => host.querySelector("button")!.click());
    await flush();
    act(() => {
      select.value = "1";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => host.querySelector("button")!.click());
    await flush();
    expect(granted).toEqual([6, 1]);
  });

  it("asks before a grant with no end, and grants nothing when told no", async () => {
    const granted: number[] = [];
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    await act(async () => root.render(<ServerGrantButton server="mobile-mcp" onGrant={(hours) => granted.push(hours)} />));
    const select = host.querySelector("select")!;
    act(() => {
      select.value = "0";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    expect(select.hasAttribute("data-forever")).toBe(true);
    await act(async () => host.querySelector("button")!.click());
    await flush();
    expect(confirm).toHaveBeenCalledOnce();
    expect(granted).toEqual([]);
    confirm.mockReturnValue(true);
    await act(async () => host.querySelector("button")!.click());
    await flush();
    expect(granted).toEqual([0]);
  });
});

describe("the session's standing grants", () => {
  it("says the time left, warns of one with no end, and revokes", async () => {
    const calls: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      calls.push(`${init?.method ?? "GET"} ${url}`);
      if (init?.method === "DELETE") return json({ standing: [] });
      return json({ standing: [
        { id: "a1", kind: "server", label: "every tool of mobile-mcp", server: "mobile-mcp", tool: "*", until: 1, left_seconds: 5 * 3600 + 59 * 60 },
        { id: "b2", kind: "server", label: "every tool of notes", server: "notes", tool: "*", until: null, left_seconds: null },
      ] });
    });
    await act(async () => root.render(<StandingGrants sessionId="s1" toast={() => {}} />));
    await flush();
    const rows = [...host.querySelectorAll("[data-grant]")];
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("all from mobile-mcp");
    expect(rows[0].textContent).toContain("5 h 59 min left");
    expect(rows[1].hasAttribute("data-forever")).toBe(true);
    expect(rows[1].textContent).toContain("⚠ no end");
    await act(async () => (rows[0].querySelector("button") as HTMLButtonElement).click());
    await flush();
    expect(calls).toContain("DELETE /api/sessions/s1/policy/standing/a1");
    expect(host.querySelector("[data-grants-empty]")).not.toBeNull();
  });

  it("counts minutes and hours in plain words", () => {
    expect(leftLabel(30)).toBe("under a minute");
    expect(leftLabel(25 * 60)).toBe("25 min left");
    expect(leftLabel(null)).toBe("no end");
  });
});
