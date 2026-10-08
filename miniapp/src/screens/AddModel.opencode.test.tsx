// @vitest-environment jsdom
// OpenCode is offered two ways in Add a model, each saying who pays for a call: Go, the prepaid plan,
// and Zen, the per-token gateway. A plan with no endpoint yet is added on its own route; Go asks for no
// per-token price, because a rate typed for a subscription would be counted as dollars spent.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "../i18n";
import { missingPlans, opencodeBaseUrl, opencodePlanOf, opencodeProvider, OPENCODE_PLANS } from "../opencode";

const calls = vi.hoisted(() => ({ onboarding: {} as Record<string, unknown>, put: [] as [string, unknown][] }));
vi.mock("../api", () => ({
  PROVIDER_KEYS_CHANGED: "daedalus:provider-keys",
  api: {
    get: async (path: string) => {
      if (path === "/api/onboarding") return calls.onboarding;
      return { limits: { usd_per_run: 5, usd_total: 0, usd_total_per_provider: {} }, providers: {} };
    },
    post: async () => ({ models: ["kimi-k3"], entries: [{ id: "kimi-k3" }] }),
    put: async (path: string, body: unknown) => {
      calls.put.push([path, body]);
      return {};
    },
  },
}));
vi.mock("./FreeModels", () => ({ FreeModels: () => null }));

import { AddModel } from "./AddModel";

const card = (id: string, billing: "metered" | "subscription", base: string) => ({
  id, kind: "opencode", name: "", base_url: base, billing, via_proxy: true, key_held: true, key_kind: "api_key", ready: true,
});

let host: HTMLDivElement;
let root: Root;
/** The window's width class: a desktop by default; the phone's rows are a test of their own. */
let desktop = true;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  desktop = true;
  window.matchMedia = ((query: string) => ({ matches: desktop && query.includes("min-width: 1024px"), media: query, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
  setLang("en");
  calls.put = [];
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

async function render() {
  await act(async () => root.render(<AddModel onSaved={() => {}} toast={() => {}} />));
}
function button(text: string): HTMLButtonElement {
  const found = Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find((b) => b.textContent?.includes(text));
  if (!found) throw new Error(`no button with ${text}`);
  return found;
}

describe("the two OpenCode plans", () => {
  it("name each configured endpoint by its plan with the one line about how it is paid for", async () => {
    calls.onboarding = { has_model: false, presets: 0, default_preset: "", needs: ["model"], message: "", keyproxy_base: "http://127.0.0.1:3200",
      providers: [card("opencode", "subscription", "http://127.0.0.1:3200/opencode"), card("opencode_zen", "metered", "http://127.0.0.1:3200/opencode_zen")] };
    await render();
    const go = button("OpenCode Go");
    const zen = button("OpenCode Zen");
    expect(go.textContent).toContain("OpenCode Go — subscription, not charged per token");
    expect(zen.textContent).toContain("OpenCode Zen — pay per token through OpenCode's gateway");
    // Nothing is offered to add: both plans already have their endpoint.
    expect(host.querySelectorAll("button.pick.dashed")).toHaveLength(2);

    // Go: no price to record, and the page says why.
    await act(async () => go.click());
    expect(host.textContent).toContain("A subscription: its calls are not charged per token");
    expect(host.querySelector("details.addmodel-pricing")).toBeNull();
    // Zen: billed per token, so its rates and ceiling can be entered.
    await act(async () => zen.click());
    expect(host.querySelector("details.addmodel-pricing")).not.toBeNull();
  });

  it("on a phone, list the plans as rows with the line about paying and without an address", async () => {
    desktop = false;
    calls.onboarding = { has_model: false, presets: 0, default_preset: "", needs: ["model"], message: "", keyproxy_base: "http://127.0.0.1:3200",
      providers: [card("opencode", "subscription", "http://127.0.0.1:3200/opencode"), card("deepseek", "metered", "http://127.0.0.1:3200/deepseek")] };
    await render();
    const go = button("OpenCode Go");
    expect(go.textContent).toContain("Subscription, not charged per token");
    // The plan with no endpoint yet is a row of its own, marked new.
    expect(button("OpenCode Zen").textContent).toContain("Pay per token through OpenCode's gateway");
    // No internal address on any row; the picked one's is under Advanced.
    for (const row of host.querySelectorAll(".ph-pickrow")) expect(row.textContent).not.toContain("http://");
    await act(async () => go.click());
    expect(host.querySelector("details.ph-advanced")?.textContent).toContain("http://127.0.0.1:3200/opencode");
  });

  it("add the missing plan on its key proxy route, with its billing", async () => {
    calls.onboarding = { has_model: false, presets: 0, default_preset: "", needs: ["model"], message: "", keyproxy_base: "http://127.0.0.1:3200",
      providers: [card("opencode", "subscription", "http://127.0.0.1:3200/opencode")] };
    await render();
    await act(async () => button("OpenCode Zen").click());
    expect(host.textContent).toContain("http://127.0.0.1:3200/opencode_zen");
    expect(host.querySelector('a[href="https://opencode.ai/auth"]')).not.toBeNull();
    await act(async () => button("Add OpenCode Zen").click());
    expect(calls.put).toEqual([["/api/providers/opencode_zen", { kind: "opencode", name: "OpenCode Zen", base_url: "http://127.0.0.1:3200/opencode_zen", billing: "metered" }]]);
  });

  it("ask for a key of the operator's own when there is no key proxy to hold it, and reach the vendor", async () => {
    calls.onboarding = { has_model: false, presets: 0, default_preset: "", needs: ["model"], message: "", keyproxy_base: "", providers: [] };
    await render();
    await act(async () => button("OpenCode Go").click());
    const save = button("Add OpenCode Go");
    expect(save.disabled).toBe(true);
    const field = host.querySelector<HTMLInputElement>('input[type="password"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, "oc-key");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => button("Add OpenCode Go").click());
    expect(calls.put).toEqual([["/api/providers/opencode", { kind: "opencode", name: "OpenCode Go", base_url: "https://opencode.ai/zen/go/v1", billing: "subscription", api_key: "oc-key" }]]);
  });
});

describe("which plan an endpoint is", () => {
  it("is Go only for a subscription and Zen for any other OpenCode endpoint", () => {
    expect(opencodePlanOf("opencode", "subscription")?.id).toBe("opencode");
    expect(opencodePlanOf("opencode", "metered")?.id).toBe("opencode_zen");
    expect(opencodePlanOf("opencode")?.id).toBe("opencode_zen");
    expect(opencodePlanOf("openai_compat", "subscription")).toBeNull();
    expect(missingPlans([{ kind: "opencode", billing: "subscription" }]).map((p) => p.id)).toEqual(["opencode_zen"]);
    expect(opencodeBaseUrl(OPENCODE_PLANS[0], "http://127.0.0.1:3200/")).toBe("http://127.0.0.1:3200/opencode");
    expect(opencodeProvider(OPENCODE_PLANS[1], "")).toEqual({ kind: "opencode", name: "OpenCode Zen", base_url: "https://opencode.ai/zen/v1", billing: "metered" });
  });

  it("says so in Russian too", () => {
    setLang("ru");
    expect(OPENCODE_PLANS.map((p) => t(p.hint))).toEqual(["OpenCode Go — подписка, без оплаты за токены", "OpenCode Zen — оплата за токены через шлюз OpenCode"]);
  });
});
