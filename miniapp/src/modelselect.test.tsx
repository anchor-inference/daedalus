// @vitest-environment jsdom
// The model picker: opened from somewhere other than its own chip it hangs from that control, not from
// the composer at the far side of the window, and the keyboard goes back to that control on Escape.
// Its column of providers by name (Favourites first, Settings' order, the free models last), its search
// across providers with each provider's count of matches, its keys and its star; the account; the two
// Anthropic routes told apart by name; and the letters an endpoint without a brand wears.

import { act, useState } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { setLang } from "./i18n";
import { AccountChoice, ModelSelect } from "./modelselect";
import { monogram, providerName } from "./ui/provider-mark";

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

  it("gives an endpoint without a brand the first letters of its name's first two words", () => {
    expect(monogram("Work-test-vllm")).toBe("WT");
    expect(monogram("abliteration")).toBe("AB");
    expect(monogram("vLLM")).toBe("VL");
    // A number is a word of its own, so numbered endpoints differ.
    expect(monogram("test-2")).toBe("T2");
    expect(monogram("test1")).toBe("T1");
    expect(monogram("Лаборатория")).toBe("ЛА");
    expect(monogram("—")).toBe("?");
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
      // Settings lists the providers in this order; the presets name them the other way round.
      providers: { claude: { kind: "anthropic", base_url: "", timeout_seconds: 0, billing: "subscription" }, deepseek: { kind: "deepseek", base_url: "", timeout_seconds: 0, api_key_set: true },
        lab: { kind: "openai_compat", name: "Lab box", base_url: "", timeout_seconds: 0 } },
      provider_keys: { lab: { via_proxy: true, key_held: false, key_kind: "api_key", ready: false } },
      presets: { flash: preset("deepseek", "DeepSeek Flash", { thinking: false }), opus: preset("claude", "Claude Opus 5", { favourite: true }), sonnet: preset("claude", "Claude Sonnet"),
        free: preset("deepseek", "Free one", { free_only: true }), lab: preset("lab", "Lab model") },
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
  const tab = (id: string) => document.querySelector<HTMLButtonElement>(`.model-picker [role=tab][data-id="${id}"]`)!;
  const rows = () => [...document.querySelectorAll<HTMLElement>(".model-picker [role=option]")];
  const names = () => rows().map((row) => row.querySelector(".model-row-name")!.textContent);
  const search = () => document.querySelector<HTMLInputElement>(".model-picker .model-search")!;
  const press = async (target: Element, k: string, extra: KeyboardEventInit = {}) => act(async () => { target.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...extra })); });
  const key = async (k: string) => press(search(), k);
  const type = async (text: string) => act(async () => {
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    set.call(search(), text);
    search().dispatchEvent(new Event("input", { bubbles: true }));
  });

  it("names every provider in a column: Favourites first, Settings' order, the free models last", async () => {
    await open();
    expect(tabs().map((row) => row.dataset.id)).toEqual(["favourites", "claude", "deepseek", "lab", "free"]);
    expect(tabs().map((row) => row.querySelector(".model-prov-name")!.textContent)).toEqual(["Favourites", "Anthropic · subscription", "DeepSeek", "Lab box", "Free"]);
    expect(tabs().map((row) => row.querySelector(".model-prov-count")?.textContent ?? "")).toEqual(["1", "2", "1", "", "1"]);
    // A provider whose key is missing says so on its row, in the word Settings uses.
    expect(tab("lab").querySelector(".model-prov-state")!.textContent).toBe("no key");
    // An endpoint without a brand wears the letters of its name; a brand keeps its mark.
    expect(tab("lab").querySelector(".provider-letter")!.textContent).toBe("LB");
    expect(tab("deepseek").querySelector(".provider-letter")).toBeNull();
    expect(tabs()[0].getAttribute("aria-selected")).toBe("true");
    expect(names()).toEqual(["Claude Opus 5"]);
    // Across providers a row names its provider; the model in use is checked.
    expect(rows()[0].querySelector(".model-row-meta")!.textContent).toBe("Anthropic · subscription");
    expect(rows()[0].getAttribute("aria-selected")).toBe("true");
  });

  it("crosses to the providers with the left arrow, walks them with up and down, and comes back with the right", async () => {
    const props = await open();
    await key("ArrowLeft");
    // Arriving in the column does not choose: the provider on show takes the keyboard.
    expect(document.activeElement).toBe(tab("favourites"));
    await press(tab("favourites"), "ArrowDown");
    expect(document.activeElement).toBe(tab("claude"));
    expect(tab("claude").getAttribute("aria-selected")).toBe("true");
    // The provider of the default carries it first, then its models with their windows.
    expect(names()).toEqual(["Global default", "Claude Opus 5", "Claude Sonnet"]);
    await press(tab("claude"), "ArrowRight");
    expect(document.activeElement).toBe(search());
    expect(rows()[1].classList.contains("active")).toBe(true);
    await key("ArrowDown");
    expect(rows()[2].classList.contains("active")).toBe(true);
    expect(search().getAttribute("aria-activedescendant")).toBe(rows()[2].id);
    await key("Enter");
    expect(props.onChoose).toHaveBeenCalledWith({ preset: "sonnet" });
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it("goes round the box with Tab, and typing in the column goes to the search", async () => {
    await open();
    search().focus();
    await key("Tab");
    expect(document.activeElement).toBe(tab("favourites"));
    await press(tab("favourites"), "Tab");
    expect(document.activeElement?.closest(".model-effort-seg")).not.toBeNull();
    await press(document.activeElement!, "Tab", { shiftKey: true });
    expect(document.activeElement).toBe(tab("favourites"));
    await press(tab("favourites"), "f");
    expect(document.activeElement).toBe(search());
    expect(search().value).toBe("f");
  });

  it("searches every provider, counts each one's matches, and narrows to the one chosen", async () => {
    await open();
    await type("flash");
    expect(names()).toEqual(["DeepSeek Flash"]);
    expect(document.querySelector(".model-picker-title")!.textContent).toBe("Found: 1");
    expect(tabs().every((row) => row.getAttribute("aria-selected") === "false")).toBe(true);
    expect(tabs().map((row) => row.querySelector(".model-prov-count")!.textContent)).toEqual(["0", "0", "1", "0", "0"]);
    expect(tabs().map((row) => row.classList.contains("none"))).toEqual([true, true, false, true, true]);
    expect(document.querySelector(".model-picker-effort")).not.toBeNull();
    expect(document.querySelector(".model-picker-foot")).not.toBeNull();
    await type("model");
    expect(names()).toEqual(["Lab model"]);
    // A provider pressed while searching narrows the matches to it, and pressed again lets them span every provider.
    await type("o");
    const every = names().length;
    await act(async () => tab("claude").click());
    expect(names()).toEqual(["Claude Opus 5", "Claude Sonnet"]);
    expect(document.querySelector(".model-picker-title")!.textContent).toBe("Anthropic · subscription · Found: 2");
    await act(async () => tab("claude").click());
    expect(names().length).toBe(every);
  });

  it("stars a model through its preset, from the row or with *", async () => {
    await open();
    await act(async () => tab("claude").click());
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
