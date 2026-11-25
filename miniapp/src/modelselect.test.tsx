// @vitest-environment jsdom
// The model picker: opened from somewhere other than its own chip it hangs from that control, not from
// the composer at the far side of the window, and the keyboard goes back to that control on Escape.
// Its tabs (Favourites first), its search across providers, its keys and its star; the account; and
// the two Anthropic routes told apart by name.

import { act, useState } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { setLang } from "./i18n";
import { AccountChoice, ModelSelect } from "./modelselect";
import { providerName } from "./ui/provider-mark";

/** What each GET answers; a path not here never answers, which is the loading state the anchor tests draw. */
const answers = vi.hoisted((): Record<string, unknown> => ({}));
vi.mock("./api", async (original) => {
  const real = await original<typeof import("./api")>();
  return { ...real, api: { ...real.api, get: vi.fn((path: string) => path in answers ? Promise.resolve(answers[path]) : new Promise(() => {})), put: vi.fn(() => Promise.resolve({})) } };
});

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("the provider names", () => {
  it("tells the subscription route from the key route in both languages", () => {
    setLang("en");
    expect(providerName("claude")).toBe("Anthropic · subscription");
    expect(providerName("anthropic")).toBe("Anthropic · API key");
    setLang("ru");
    expect(providerName("claude")).toBe("Anthropic · подписка");
    expect(providerName("anthropic")).toBe("Anthropic · API-ключ");
    // A name the operator gave the provider still wins.
    expect(providerName("claude", { kind: "anthropic", name: "Work", base_url: "", timeout_seconds: 0 })).toBe("Work");
    setLang("en");
  });
});

describe("the anchor", () => {
  let host: HTMLDivElement;
  let root: Root;
  let chip: HTMLButtonElement;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= class { observe() {} disconnect() {} };
    // The panel strip's chip: high on the right of the window, the way the inspector puts it.
    chip = document.createElement("button");
    chip.getBoundingClientRect = () => ({ left: 700, right: 840, top: 50, bottom: 74, width: 140, height: 24, x: 700, y: 50, toJSON() {} });
    document.body.appendChild(chip);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    chip.remove();
  });

  function Harness({ anchor }: { anchor: HTMLElement | null }) {
    const [open, setOpen] = useState(true);
    return <ModelSelect model="claude-opus-5" fallback={null} open={open} onOpenChange={setOpen} onChoose={() => {}} anchor={anchor} pill={false} />;
  }

  it("hangs below the chip that opened it, from its left edge", async () => {
    await act(async () => root.render(<Harness anchor={chip} />));
    const menu = document.querySelector<HTMLElement>(".model-picker")!;
    expect(menu).not.toBeNull();
    // Base UI places the box through the positioner around it, once it has measured both, and moves
    // it there with a transform. The document needs a size for that: jsdom lays nothing out, and in a
    // window of no height every side collides and the box flips.
    Object.defineProperty(document.documentElement, "clientWidth", { configurable: true, value: 1440 });
    Object.defineProperty(document.documentElement, "clientHeight", { configurable: true, value: 900 });
    await act(async () => { window.dispatchEvent(new Event("resize")); await new Promise((resolve) => setTimeout(resolve, 30)); });
    const placed = menu.closest<HTMLElement>(".popover-pos")!;
    expect(placed.style.transform).toBe("translate(700px, 80px)");
    // No pill was asked for, so none is drawn: a running phone composer has no room for one.
    expect(host.querySelector(".model-select")).toBeNull();
  });

  it("gives the keyboard back to that chip on Escape", async () => {
    await act(async () => root.render(<Harness anchor={chip} />));
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(document.querySelector(".model-picker")).toBeNull();
    // The focus comes back a frame after the layer goes (ui/primitives/layers.ts says why).
    await act(async () => { await new Promise((resolve) => requestAnimationFrame(() => resolve(null))); });
    expect(document.activeElement).toBe(chip);
  });
});

describe("the account row", () => {
  let host: HTMLDivElement;
  let root: Root;
  const account = (id: string, label: string, extra: Record<string, unknown> = {}) => ({
    upstream: "claude", id, label, position: 0, default: id === "default", configured: true, email: "", plan: "", resting: false, resting_until: null,
    rest_reason: "", usage: null, calls_24h: 0, last_call_at: null, sign_in_command: "", ...extra,
  });
  const listing = (accounts: unknown[]) => ({ proxy: { reachable: true, state: "ok", error: "" }, terminal: true,
    upstreams: [{ upstream: "claude", providers: ["claude"], sign_in: "terminal", default_account: "default", fallover: true, accounts }] });
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= class { observe() {} disconnect() {} };
    answers["/api/settings"] = { presets: {}, providers: {}, model: { preset: "" } };
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    for (const path of Object.keys(answers)) delete answers[path];
  });

  async function open(choice: AccountChoice) {
    await act(async () => root.render(<ModelSelect model="claude-opus-5" fallback={null} open onOpenChange={() => {}} onChoose={() => {}} onChooseEffort={() => {}} thinking effort="high" account={choice} />));
    await act(async () => {});
  }

  it("is not drawn while the subscription has a single account", async () => {
    answers["/api/subscription-accounts?usage=false"] = listing([account("default", "first")]);
    await open({ upstream: "claude", chosen: "", fallback: "default", onChoose: () => {} });
    expect(document.querySelector("[data-branch='account']")).toBeNull();
  });

  it("names the default, lists the accounts with what keeps one out, and sends the one chosen", async () => {
    answers["/api/subscription-accounts?usage=false"] = listing([account("default", "first"), account("w2", "second", { resting: true, rest_reason: "quota" })]);
    const onChoose = vi.fn();
    await open({ upstream: "claude", chosen: "", fallback: "default", onChoose });
    const entry = document.querySelector<HTMLButtonElement>("[data-branch='account'] .model-account-chip")!;
    expect(entry).not.toBeNull();
    expect(entry.textContent).toContain("default (first)");
    await act(async () => entry.click());
    const rows = [...document.querySelectorAll<HTMLButtonElement>(".account-menu [data-account]")];
    expect(rows.map((row) => row.querySelector(".menu-row-t")!.textContent)).toEqual(["default (first)", "first", "second"]);
    expect(rows[2].textContent).toContain("resting");
    await act(async () => rows[2].click());
    expect(onChoose).toHaveBeenCalledWith("w2");
  });

  it("names the account chosen for the session", async () => {
    answers["/api/subscription-accounts?usage=false"] = listing([account("default", "first"), account("w2", "second")]);
    await open({ upstream: "claude", chosen: "w2", fallback: "default", onChoose: () => {} });
    expect(document.querySelector("[data-branch='account'] .model-account-chip")!.textContent).toContain("second");
  });
});

describe("the box", () => {
  let host: HTMLDivElement;
  let root: Root;
  const preset = (provider: string, label: string, extra: Record<string, unknown> = {}) => ({ provider, model: label.toLowerCase().replace(/ /g, "-"), label, thinking: true, reasoning_effort: "high", images: false, context_window: 200000, max_output_tokens: 32000, ...extra });
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= class { observe() {} disconnect() {} };
    answers["/api/settings"] = {
      model: { preset: "opus" },
      providers: { claude: { kind: "anthropic", base_url: "", timeout_seconds: 0, billing: "subscription" }, deepseek: { kind: "deepseek", base_url: "", timeout_seconds: 0, api_key_set: true } },
      presets: { opus: preset("claude", "Claude Opus 5", { favourite: true }), sonnet: preset("claude", "Claude Sonnet"), flash: preset("deepseek", "DeepSeek Flash", { thinking: false }), free: preset("deepseek", "Free one", { free_only: true }) },
    };
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    for (const path of Object.keys(answers)) delete answers[path];
  });

  async function open(over: Record<string, unknown> = {}) {
    const props = { model: "Claude Opus 5", fallback: null, open: true, onOpenChange: vi.fn(), onChoose: vi.fn(), effort: "high", thinking: true, onChooseEffort: vi.fn(), ...over };
    await act(async () => root.render(<ModelSelect {...props} />));
    await act(async () => {});
    return props;
  }
  const tabs = () => [...document.querySelectorAll<HTMLButtonElement>(".model-picker [role=tab]")];
  const rows = () => [...document.querySelectorAll<HTMLElement>(".model-picker [role=option]")];
  const search = () => document.querySelector<HTMLInputElement>(".model-picker .model-search")!;
  const key = async (k: string) => act(async () => { search().dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true })); });

  it("opens on Favourites, then one tab per provider and the free models last", async () => {
    await open();
    expect(tabs().map((tab) => tab.dataset.tab ?? tab.dataset.provider)).toEqual(["favourites", "claude", "deepseek", "free"]);
    expect(tabs()[0].getAttribute("aria-selected")).toBe("true");
    expect(rows().map((row) => row.querySelector(".model-row-name")!.textContent)).toEqual(["Claude Opus 5"]);
    // Across providers a row says how the model is paid for; the model in use is checked.
    expect(rows()[0].querySelector(".model-row-meta")!.textContent).toBe("subscription");
    expect(rows()[0].getAttribute("aria-selected")).toBe("true");
  });

  it("walks the tabs with the side arrows and the rows with up and down, and Enter picks", async () => {
    const props = await open();
    await key("ArrowRight");
    expect(tabs()[1].getAttribute("aria-selected")).toBe("true");
    // The provider of the default carries it first, then its models with their windows.
    expect(rows().map((row) => row.querySelector(".model-row-name")!.textContent)).toEqual(["Global default", "Claude Opus 5", "Claude Sonnet"]);
    expect(rows()[1].classList.contains("active")).toBe(true);
    await key("ArrowDown");
    expect(rows()[2].classList.contains("active")).toBe(true);
    expect(search().getAttribute("aria-activedescendant")).toBe(rows()[2].id);
    await key("Enter");
    expect(props.onChoose).toHaveBeenCalledWith({ preset: "sonnet" });
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("searches every provider, and the box keeps its parts while it does", async () => {
    await open();
    await act(async () => {
      const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      set.call(search(), "flash");
      search().dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(rows().map((row) => row.querySelector(".model-row-name")!.textContent)).toEqual(["DeepSeek Flash"]);
    expect(document.querySelector(".model-picker-title")!.textContent).toBe("Found: 1");
    expect(tabs().every((tab) => tab.getAttribute("aria-selected") === "false")).toBe(true);
    expect(document.querySelector(".model-picker-effort")).not.toBeNull();
    expect(document.querySelector(".model-picker-foot")).not.toBeNull();
  });

  it("stars a model through its preset, from the row or with *", async () => {
    await open();
    await key("ArrowRight");
    const star = rows()[2].querySelector<HTMLButtonElement>(".model-star")!;
    expect(star.getAttribute("aria-pressed")).toBe("false");
    await act(async () => star.click());
    expect(api.put).toHaveBeenCalledWith("/api/presets/sonnet", { favourite: true });
    expect(rows()[2].querySelector(".model-star")!.getAttribute("aria-pressed")).toBe("true");
    await key("*");
    expect(api.put).toHaveBeenCalledWith("/api/presets/opus", { favourite: false });
  });

  it("offers the reasoning as one row: off first for a model that reasons, and keeps the box open", async () => {
    const props = await open();
    const choices = [...document.querySelectorAll<HTMLButtonElement>(".model-effort-seg [role=radio]")];
    expect(choices.map((c) => c.textContent)).toEqual(["Off", "Low", "Medium", "High", "Max"]);
    expect(choices[3].getAttribute("aria-checked")).toBe("true");
    await act(async () => choices[4].click());
    expect(props.onChooseEffort).toHaveBeenCalledWith("xhigh");
    expect(props.onOpenChange).not.toHaveBeenCalled();
    // A model that does not reason has nothing to set, and the row says so.
    await open({ model: "DeepSeek Flash", thinking: false });
    expect(document.querySelector(".model-effort-seg")).toBeNull();
    expect(document.querySelector(".model-effort-none")!.textContent).toContain("without reasoning");
  });
});
