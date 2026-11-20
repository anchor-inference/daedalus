// @vitest-environment jsdom
// The model picker opened from somewhere other than its own pill: it hangs from that control, not
// from the composer at the far side of the window, and the keyboard goes back to that control on
// Escape. And the two Anthropic routes are told apart by name.

import { act, useState } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { AccountChoice, ModelSelect } from "./modelselect";
import { providerName } from "./ui/provider-mark";

/** What each GET answers; a path not here never answers, which is the loading state the anchor tests draw. */
const answers = vi.hoisted((): Record<string, unknown> => ({}));
vi.mock("./api", async (original) => {
  const real = await original<typeof import("./api")>();
  return { ...real, api: { ...real.api, get: vi.fn((path: string) => path in answers ? Promise.resolve(answers[path]) : new Promise(() => {})) } };
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
    return <ModelSelect model="claude-opus-5" fallback={null} open={open} onOpenChange={setOpen} onChoose={() => {}} sheet={false} anchor={anchor} pill={false} />;
  }

  it("hangs below the chip that opened it, from its left edge", async () => {
    await act(async () => root.render(<Harness anchor={chip} />));
    const menu = document.querySelector<HTMLElement>(".model-menu")!;
    expect(menu).not.toBeNull();
    expect(menu.style.top).toBe("80px");
    expect(menu.style.left).toBe("700px");
    // No pill was asked for, so none is drawn: a running phone composer has no room for one.
    expect(host.querySelector(".model-select")).toBeNull();
  });

  it("gives the keyboard back to that chip on Escape", async () => {
    await act(async () => root.render(<Harness anchor={chip} />));
    await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
    expect(document.querySelector(".model-menu")).toBeNull();
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
    await act(async () => root.render(<ModelSelect model="claude-opus-5" fallback={null} open onOpenChange={() => {}} onChoose={() => {}} sheet={false} account={choice} />));
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
    const entry = document.querySelector<HTMLButtonElement>("[data-branch='account'] .effort-entry")!;
    expect(entry).not.toBeNull();
    expect(entry.textContent).toContain("default (first)");
    await act(async () => entry.click());
    const rows = [...document.querySelectorAll<HTMLLabelElement>(".account-options .effort-option")];
    expect(rows.map((row) => row.querySelector(".truncate")!.textContent)).toEqual(["default (first)", "first", "second"]);
    expect(rows[2].textContent).toContain("resting");
    await act(async () => rows[2].querySelector("input")!.click());
    expect(onChoose).toHaveBeenCalledWith("w2");
  });

  it("names the account chosen for the session", async () => {
    answers["/api/subscription-accounts?usage=false"] = listing([account("default", "first"), account("w2", "second")]);
    await open({ upstream: "claude", chosen: "w2", fallback: "default", onChoose: () => {} });
    expect(document.querySelector("[data-branch='account'] .effort-entry")!.textContent).toContain("second");
  });
});
