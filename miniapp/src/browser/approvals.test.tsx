// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "../i18n";

const confirmed = vi.hoisted(() => ({ answer: true, asked: 0 }));
vi.mock("../ui", async (real) => ({
  ...(await real<typeof import("../ui")>()),
  confirmAsync: async () => { confirmed.asked += 1; return confirmed.answer; },
}));

import { ApprovalChip } from "./approvals";

describe("the approval chip", () => {
  let host: HTMLDivElement;
  let root: Root;
  let puts: unknown[];
  let skipConfirmed: boolean;

  beforeEach(() => {
    setLang("en");
    vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
    puts = [];
    skipConfirmed = false;
    confirmed.asked = 0;
    confirmed.answer = true;
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
      expect(url).toBe("/api/sessions/s1/browser-approvals");
      if (init.method === "PUT") {
        const body = JSON.parse(String(init.body));
        puts.push(body);
        if (body.mode === "skip" && !body.confirm && !skipConfirmed) return new Response(JSON.stringify({ detail: "say yes once", code: "skip_confirm_needed" }), { status: 409 });
        if (body.mode === "skip") skipConfirmed = true;
        return new Response(JSON.stringify({ mode: body.mode, env: "user" }));
      }
      return new Response(JSON.stringify({ mode: "manual", env: "user" }));
    }));
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
  });

  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.unstubAllGlobals();
  });

  async function settle() {
    for (let i = 0; i < 6; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  }

  async function pick(mode: string) {
    await act(async () => { host.querySelector<HTMLButtonElement>(".approvals-chip")!.click(); });
    await act(async () => { document.querySelector<HTMLButtonElement>(`.approvals-menu [data-mode="${mode}"]`)!.click(); });
    await settle();
  }

  it("shows the conversation's mode and offers the three", async () => {
    act(() => root.render(<ApprovalChip sessionId="s1" toast={() => {}} />));
    await settle();
    const chip = host.querySelector(".approvals-chip")!;
    expect(chip.getAttribute("data-mode")).toBe("manual");
    expect(chip.textContent).toContain(t("approvals.manual"));
    await act(async () => { (chip as HTMLButtonElement).click(); });
    const items = [...document.querySelectorAll(".approvals-menu [role=menuitemradio]")].map((b) => b.getAttribute("data-mode"));
    expect(items).toEqual(["manual", "auto", "skip"]);
  });

  it("sets automatic at once", async () => {
    act(() => root.render(<ApprovalChip sessionId="s1" toast={() => {}} />));
    await settle();
    await pick("auto");
    expect(puts).toEqual([{ mode: "auto" }]);
    expect(host.querySelector(".approvals-chip")!.getAttribute("data-mode")).toBe("auto");
    expect(confirmed.asked).toBe(0);
  });

  it("asks once before skipping every approval, and a no leaves the mode as it was", async () => {
    act(() => root.render(<ApprovalChip sessionId="s1" toast={() => {}} />));
    await settle();
    confirmed.answer = false;
    await pick("skip");
    expect(confirmed.asked).toBe(1);
    expect(puts).toEqual([{ mode: "skip" }]);
    expect(host.querySelector(".approvals-chip")!.getAttribute("data-mode")).toBe("manual");
    confirmed.answer = true;
    await pick("skip");
    expect(puts.slice(1)).toEqual([{ mode: "skip" }, { mode: "skip", confirm: true }]);
    expect(host.querySelector(".approvals-chip")!.getAttribute("data-mode")).toBe("skip");
  });
});
