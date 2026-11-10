// @vitest-environment jsdom
// A tool result's "show all": it fetches the whole text, shows every line of it in one scrolling
// block, offers to copy it, and never passes a preview off as the whole when the fetch fails.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import { setLang } from "./i18n";
import { FullResult, ToolResultView } from "./toolresult";
import type { ToolItem } from "./turns";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const whole = Array.from({ length: 2000 }, (_, i) => `line ${String(i).padStart(5, "0")} of a long output`).join("\n");
const item = (over: Partial<ToolItem> = {}): ToolItem => ({ kind: "tool", id: "call-1", name: "Exec", args: {}, running: false, result: whole.slice(0, 400), length: whole.length, clipped: true, ...over });

describe("the whole of a tool result", () => {
  let host: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    setLang("en");
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.restoreAllMocks();
  });

  const button = (label: RegExp) => [...host.querySelectorAll("button")].find((b) => label.test(b.textContent ?? ""));

  it("shows a short preview first and every line of the result once asked", async () => {
    const get = vi.spyOn(api, "get").mockResolvedValue({ content: whole, complete: true });
    const kept: FullResult[] = [];
    await act(async () => root.render(<ToolResultView sessionId="s1" item={item()} toast={() => {}} initial={null} keep={(f) => kept.push(f)} />));
    expect(host.querySelector("pre.result")!.textContent).toHaveLength(400);
    await act(async () => button(/Show all \(\d[\d\s,.]* characters\)/)!.click());
    expect(get).toHaveBeenCalledWith("/api/sessions/s1/tool-results/call-1");
    const pre = host.querySelector("pre.result.full")!;
    expect(pre.textContent).toBe(whole);
    expect(pre.textContent!.endsWith("line 01999 of a long output")).toBe(true);
    expect(kept).toEqual([{ text: whole, complete: true }]);
    expect(button(/Show all/)).toBeUndefined();
    expect(button(/Copy all/)).toBeDefined();
    expect(button(/Download/)).toBeDefined();
  });

  it("offers the whole of a masked result, whose length is not known until it is read", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ content: whole, complete: true });
    await act(async () => root.render(<ToolResultView sessionId="s1" item={item({ length: undefined, result: "[The output of Exec was masked by compaction]" })} toast={() => {}} initial={null} keep={() => {}} />));
    await act(async () => button(/Show the whole result/)!.click());
    expect(host.querySelector("pre.result.full")!.textContent).toBe(whole);
  });

  it("says when the stored original is gone instead of presenting what is left as the whole", async () => {
    vi.spyOn(api, "get").mockResolvedValue({ content: "what was kept", complete: false });
    await act(async () => root.render(<ToolResultView sessionId="s1" item={item()} toast={() => {}} initial={null} keep={() => {}} />));
    await act(async () => button(/Show all/)!.click());
    expect(host.textContent).toContain("stored original of this result is gone");
  });

  it("keeps the preview a preview, and the button, when the fetch fails", async () => {
    vi.spyOn(api, "get").mockRejectedValue(new Error("offline"));
    await act(async () => root.render(<ToolResultView sessionId="s1" item={item()} toast={() => {}} initial={null} keep={() => {}} />));
    await act(async () => button(/Show all/)!.click());
    expect(host.querySelector("pre.result.full")).toBeNull();
    expect(button(/Show all/)).toBeDefined();
    expect(host.textContent).toContain("Could not load the whole result");
  });

  it("waits calmly for a step of a running run to be saved, and asks again on request", async () => {
    // The fault this guards: during a run the host had no saved copy of the result yet, and the
    // viewer showed "Could not load the whole result: no such tool result" under it.
    const get = vi.spyOn(api, "get").mockResolvedValueOnce({ content: null, complete: false, pending: true }).mockResolvedValueOnce({ content: whole, complete: true });
    await act(async () => root.render(<ToolResultView sessionId="s1" item={item()} toast={() => {}} initial={null} keep={() => {}} />));
    await act(async () => button(/Show all/)!.click());
    expect(host.textContent).toContain("available once this step is saved");
    expect(host.textContent).not.toContain("Could not load");
    expect(host.querySelector("pre.result.full")).toBeNull();
    await act(async () => button(/Try again/)!.click());
    expect(get).toHaveBeenCalledTimes(2);
    expect(host.querySelector("pre.result.full")!.textContent).toBe(whole);
    expect(host.textContent).not.toContain("available once this step is saved");
  });

  it("copies all of it, not the part in view", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const toast = vi.fn();
    await act(async () => root.render(<ToolResultView sessionId="s1" item={item()} toast={toast} initial={{ text: whole, complete: true }} keep={() => {}} />));
    await act(async () => button(/Copy all/)!.click());
    expect(writeText).toHaveBeenCalledWith(whole);
    expect(toast).not.toHaveBeenCalled();
    expect(button(/Copy all/)!.querySelector("svg")).not.toBeNull();
  });

  it("puts the auto-review's block on its row with the reviewer's reason and Allow anyway, in both languages", async () => {
    const told = "Blocked by the auto-review: nobody asked for a release (rule review.publish). The action was not taken. Approval key: 0123456789ab. The operator sees this call on a card.";
    const post = vi.spyOn(api, "post").mockResolvedValueOnce({ grants: {} }).mockRejectedValueOnce(new Error("this blocked call was already allowed once"));
    const toast = vi.fn();
    const blocked = item({ error: true, result: told, clipped: false });
    await act(async () => root.render(<ToolResultView sessionId="s1" item={blocked} toast={toast} initial={null} keep={() => {}} />));
    const card = host.querySelector("[data-blocked]")!;
    expect(card.textContent).toContain("Blocked by the auto-review");
    expect(card.textContent).toContain("Reviewer: nobody asked for a release");
    expect(button(/Allow once/)).toBeUndefined();
    await act(async () => button(/^Allow anyway$/)!.click());
    expect(post).toHaveBeenCalledWith("/api/sessions/s1/policy/grant", { key: "0123456789ab" });
    expect(button(/Allowed; the agent repeats it/)!.disabled).toBe(true);
    act(() => root.unmount());
    root = createRoot(host);
    setLang("ru");
    await act(async () => root.render(<ToolResultView sessionId="s1" item={blocked} toast={toast} initial={null} keep={() => {}} />));
    expect(host.querySelector("[data-blocked]")!.textContent).toContain("Проверка: nobody asked for a release");
    // A second press after the grant was spent is the host's refusal, said aloud.
    await act(async () => button(/^Разрешить всё равно$/)!.click());
    expect(toast).toHaveBeenCalledWith(expect.stringContaining("already allowed once"));
  });
});
