// @vitest-environment jsdom
// Settings → Rules: a level picked in the grid is saved as the `action_rules` section, the review's
// switch follows the level (always on at "on command", nothing to switch at "asks" or "hands to you"),
// and the review log shows what was decided.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Settings } from "./api";
import { ActionRules, reviewState } from "./actionrules";
import { setLang } from "./i18n";
import { prime } from "./store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("the review's state", () => {
  it("follows the level", () => {
    expect(reviewState("auto", true)).toBe("on");
    expect(reviewState("auto", false)).toBe("off");
    expect(reviewState("on_command", false)).toBe("always");
    expect(reviewState("ask", true)).toBe("none");
    expect(reviewState("handoff", true)).toBe("none");
  });
});

describe("the grid", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    prime("/api/action-rules", {
      categories: ["message", "publish", "payment", "delete", "external_write", "self_change"],
      levels: ["auto", "on_command", "ask", "handoff"],
      defaults: {},
      log: [{ id: 1, at: new Date().toISOString(), session_id: "s1", tool: "Mcp_Gmail_send_email", category: "message", level: "auto", verdict: "block", reason: "the operator named Bob, not Mallory", failed: false, summary: "to: mallory@example.com", model: "small", elapsed_ms: 900 }],
    });
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  it("saves a level and shows the decisions", async () => {
    const save = vi.fn(async () => undefined);
    const s = { presets: {}, action_rules: { levels: { message: "auto" }, review: ["message"], review_preset: "", review_timeout_seconds: 20, injection_model: false, injection_preset: "" } } as unknown as Settings;
    await act(async () => root.render(<ActionRules s={s} save={save} />));
    const row = host.querySelector('[data-category="publish"]')!;
    expect(row.getAttribute("data-level")).toBe("auto");
    await act(async () => (row.querySelector('[data-level="handoff"]') as HTMLButtonElement).click());
    expect(save).toHaveBeenCalledTimes(1);
    const patch = (save.mock.calls[0] as unknown as [Partial<Settings>])[0];
    expect(patch.action_rules?.levels.publish).toBe("handoff");
    expect(patch.action_rules?.levels.payment).toBe("ask");
    const payment = host.querySelector('[data-category="payment"] .ar-review')!;
    expect(payment.getAttribute("data-review")).toBe("none");
    expect(payment.querySelector("button")!.disabled).toBe(true);
    const log = host.querySelector('[data-testid="review-log"]')!;
    expect(log.textContent).toContain("Blocked");
    expect(log.textContent).toContain("the operator named Bob, not Mallory");
  });
});
