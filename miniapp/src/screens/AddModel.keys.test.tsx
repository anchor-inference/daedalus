// @vitest-environment jsdom
// An endpoint's key state is one answer on every screen. A native installation showed OpenRouter as
// "ready" with its "key stored" in Settings while Add a model, asking the key proxy, showed the same
// endpoint with "no key": Settings read readiness from whether an adapter could be built, which says
// nothing about a key behind the proxy. Both now read the host's key state, and Add a model asks
// again when a key is saved elsewhere.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "../i18n";

const calls = vi.hoisted(() => ({ onboarding: {} as Record<string, unknown>, asked: 0 }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    api: {
      get: async (path: string) => {
        if (path === "/api/onboarding") {
          calls.asked += 1;
          return calls.onboarding;
        }
        return { limits: { usd_per_run: 5, usd_total: 0, usd_total_per_provider: {} }, providers: {} };
      },
      post: async () => ({ models: [] }),
      put: async () => ({}),
    },
  };
});
vi.mock("./FreeModels", () => ({ FreeModels: () => null }));

import { announceProviderKeys } from "../api";
import { AddModel } from "./AddModel";
import { providerReady } from "./Settings";

const openrouter = (held: boolean) => ({
  id: "openrouter", kind: "openrouter", name: "", base_url: "http://127.0.0.1:3201/openrouter", billing: "metered",
  via_proxy: true, key_held: held, key_kind: "api_key", ready: held,
});
const state = (held: boolean) => ({ has_model: false, presets: 0, default_preset: "", needs: ["model"], message: "", keyproxy_base: "http://127.0.0.1:3201", providers: [openrouter(held)] });

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.matchMedia = ((query: string) => ({ matches: query.includes("min-width: 1024px"), media: query, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia;
  setLang("en");
  calls.asked = 0;
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); });

function openrouterCard(): HTMLButtonElement {
  const found = Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find((b) => b.textContent?.includes("OpenRouter"));
  if (!found) throw new Error("no OpenRouter card");
  return found;
}

describe("an endpoint's key state", () => {
  it("is asked for again in Add a model when a key is saved elsewhere", async () => {
    calls.onboarding = state(false);
    await act(async () => root.render(<AddModel onSaved={() => {}} toast={() => {}} />));
    expect(openrouterCard().textContent).toContain(t("add.key.none"));
    const before = calls.asked;

    calls.onboarding = state(true);
    await act(async () => announceProviderKeys());
    expect(calls.asked).toBe(before + 1);
    expect(openrouterCard().textContent).toContain(t("add.key.ready"));
    expect(openrouterCard().textContent).not.toContain(t("add.key.hint.proxy"));
  });

  it("is ready in Settings only when the host says a credential exists", () => {
    const settings = { provider_keys: { openrouter: { via_proxy: true, key_held: false, key_kind: "api_key" as const, ready: false }, codex: { via_proxy: true, key_held: true, key_kind: "cli_login" as const, ready: true } } };
    expect(providerReady(settings, "openrouter")).toBe(false);
    expect(providerReady(settings, "codex")).toBe(true);
    expect(providerReady({}, "openrouter")).toBe(false);
  });
});
