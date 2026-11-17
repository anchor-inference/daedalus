// @vitest-environment jsdom
// The language chip in the composer: what it says while translating and while not, the menu's rows,
// and the choice it hands the screen — "follow the default" is null, "off" is the empty code.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang } from "./i18n";
import { AgentLangSelect, agentLangLabel } from "./agentlang";
import { reconcile, buildTurns } from "./turns";
import { PlusSheet } from "./composerbox";
import type { MessageView } from "./api";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

describe("the label", () => {
  it("names the pair while translating and the operator's language alone otherwise", () => {
    expect(agentLangLabel({ agent: "en", operator: "ru", active: true, explicit: true })).toBe("EN ⇄ RU");
    expect(agentLangLabel({ agent: "", operator: "ru", active: false, explicit: false })).toBe("RU");
    // The same language on both sides translates nothing, so it reads as off.
    expect(agentLangLabel({ agent: "ru", operator: "ru", active: false, explicit: true })).toBe("RU");
  });
});

describe("the chip and its menu", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= class { observe() {} disconnect() {} };
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  const chip = () => host.querySelector<HTMLButtonElement>(".agentlang-chip")!;
  const row = (lang: string) => document.querySelector<HTMLButtonElement>(`.agentlang-row[data-lang="${lang}"]`)!;

  it("lists the default, off and every language but the operator's, the current one checked", async () => {
    await act(async () => root.render(<AgentLangSelect state={{ agent: "en", operator: "ru", active: true, explicit: true }} defaultAgent="" onChoose={() => {}} sheet={false} />));
    expect(chip().textContent).toBe("EN ⇄ RU");
    expect(chip().classList.contains("translating")).toBe(true);
    await act(async () => chip().click());
    const langs = Array.from(document.querySelectorAll<HTMLButtonElement>(".agentlang-row")).map((r) => r.dataset.lang);
    expect(langs.slice(0, 3)).toEqual(["default", "off", "en"]);
    expect(langs).not.toContain("ru");
    expect(row("en").getAttribute("aria-checked")).toBe("true");
    expect(row("default").getAttribute("aria-checked")).toBe("false");
  });

  it("turns translation off with the empty code and says so at once", async () => {
    const onChoose = vi.fn();
    await act(async () => root.render(<AgentLangSelect state={{ agent: "en", operator: "ru", active: true, explicit: true }} defaultAgent="" onChoose={onChoose} sheet={false} />));
    await act(async () => chip().click());
    await act(async () => row("off").click());
    expect(onChoose).toHaveBeenCalledWith({ agent: "" });
    expect(chip().textContent).toBe("RU");
    expect(chip().classList.contains("off")).toBe(true);
  });

  it("hands back null to follow the default", async () => {
    const onChoose = vi.fn();
    await act(async () => root.render(<AgentLangSelect state={{ agent: "", operator: "ru", active: false, explicit: true }} defaultAgent="en" onChoose={onChoose} sheet={false} />));
    await act(async () => chip().click());
    expect(row("default").textContent).toContain("As in Settings: English");
    await act(async () => row("default").click());
    expect(onChoose).toHaveBeenCalledWith({ agent: null });
    expect(chip().textContent).toBe("EN ⇄ RU");
  });

  it("keeps the agent's code apart from the pair, for a narrow composer to show alone", async () => {
    await act(async () => root.render(<AgentLangSelect state={{ agent: "en", operator: "ru", active: true, explicit: true }} defaultAgent="" onChoose={() => {}} sheet={false} />));
    expect(chip().querySelector(".agentlang-agent")?.textContent).toBe("EN");
    expect(chip().querySelector(".agentlang-to")?.textContent).toBe(" ⇄ RU");
  });

  it("draws no chip on a phone, where the + sheet opens its languages", async () => {
    const onOpenChange = vi.fn();
    const state = { agent: "en", operator: "ru", active: true, explicit: true };
    await act(async () => root.render(<AgentLangSelect state={state} defaultAgent="" onChoose={() => {}} sheet chip={false} open={false} onOpenChange={onOpenChange} />));
    expect(host.querySelector(".agentlang-chip")).toBeNull();
    expect(document.querySelector(".agentlang-row")).toBeNull();
    await act(async () => root.render(<AgentLangSelect state={state} defaultAgent="" onChoose={() => {}} sheet chip={false} open onOpenChange={onOpenChange} />));
    expect(row("en").getAttribute("aria-checked")).toBe("true");
    await act(async () => row("de").click());
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("is a row of the phone's + sheet that names the pair", async () => {
    const onLanguage = vi.fn();
    await act(async () => root.render(<PlusSheet onClose={() => {}} onPhoto={() => {}} onFiles={() => {}} onLanguage={onLanguage} language="EN ⇄ RU" />));
    const language = document.querySelector<HTMLButtonElement>("[data-action='language']")!;
    expect(language.textContent).toContain("Language the agent works in");
    expect(language.textContent).toContain("EN ⇄ RU");
    await act(async () => language.click());
    expect(onLanguage).toHaveBeenCalled();
  });
});

describe("a translation that arrives", () => {
  const answer = (state: "pending" | "done", text: string): MessageView => ({
    role: "assistant", seq: 2, run_id: "r1", text, thinking: "", tool_calls: [], tool_results: [], created_at: "2026-11-17T10:00:01Z",
    translation: state === "done" ? { direction: "in", state, from: "en", to: "ru", alternate: "Done." } : { direction: "in", state, from: "en", to: "ru" },
  });
  const user: MessageView = { role: "user", seq: 1, run_id: "r1", origin: "operator", text: "Сделай", thinking: "", tool_calls: [], tool_results: [], created_at: "2026-11-17T10:00:00Z" };

  it("replaces the pending row even when the text happens to be the same", () => {
    const merged = reconcile([user, answer("pending", "Done.")], [user, answer("done", "Done.")]);
    expect(merged.messages[1].translation?.state).toBe("done");
    const before = buildTurns([user, answer("pending", "Done.")], []);
    const after = buildTurns([user, answer("done", "Done.")], before);
    expect(after[0]).not.toBe(before[0]);
    expect(after[0].answerTranslation?.state).toBe("done");
  });
});
