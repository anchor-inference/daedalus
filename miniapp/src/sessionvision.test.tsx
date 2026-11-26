// @vitest-environment jsdom
// A session's own vision model in its Details: the line says which model looks at the chat's pictures,
// the page offers only the models that take images with "default (<the global one>)" first, and a pick
// is sent to the session's vision endpoint and drawn from its answer.

import { act } from "react";
import { createRoot, Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Preset, SessionDetail } from "./api";
import { SessionDetails, type DetailsActions } from "./details";
import { BLANK } from "./models";
import { setLang } from "./i18n";
import { SessionVisionModel, visionValue } from "./sessionvision";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const PRESETS: Record<string, Preset> = {
  flash: { ...BLANK, provider: "deepseek", model: "deepseek-flash", label: "DeepSeek Flash", images: false },
  luna: { ...BLANK, provider: "opencode", model: "gpt-luna", label: "GPT Luna", images: true },
  sharp: { ...BLANK, provider: "openrouter", model: "sharp/eyes", label: "Sharp eyes", images: true },
};

const DEFAULT = { vision_preset: "", vision_label: "", vision_default: "luna", vision_default_label: "GPT Luna" };

function answer(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

describe("the line's state", () => {
  it("names the session's own model, or the default with the model it stands for", () => {
    setLang("en");
    expect(visionValue(DEFAULT)).toEqual({ text: "default (GPT Luna)", own: false });
    expect(visionValue({ ...DEFAULT, vision_preset: "sharp", vision_label: "Sharp eyes" })).toEqual({ text: "Sharp eyes", own: true });
    expect(visionValue({ vision_preset: "", vision_default: "" }).text).toBe("default (not set)");
    setLang("ru");
    expect(visionValue(DEFAULT).text).toBe("по умолчанию (GPT Luna)");
    setLang("en");
  });
});

describe("the page", () => {
  let host: HTMLDivElement;
  let root: Root;
  let posted: { url: string; body: unknown }[];
  beforeEach(() => {
    setLang("en");
    posted = [];
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes("/api/settings")) return answer({ presets: PRESETS });
      if (url.endsWith("/vision") && init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        posted.push({ url, body });
        return answer({ ok: true, ...DEFAULT, vision_preset: body.preset ?? "", vision_label: PRESETS[body.preset]?.label ?? "", vision_effort: body.effort ?? "" });
      }
      return answer({ items: [], groups: [], enabled: [], servers: [], standing: [] });
    });
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
    vi.restoreAllMocks();
  });

  it("offers the default first and only the image models, and saves a pick to the session", async () => {
    const saved = vi.fn();
    await act(async () => root.render(<SessionVisionModel sessionId="s1" detail={DEFAULT} toast={() => {}} onSaved={saved} />));
    expect(host.querySelector(".sel-current .sel-name")!.textContent).toBe("default (GPT Luna)");
    act(() => host.querySelector<HTMLButtonElement>(".sel-current button")!.click());
    const options = [...host.querySelectorAll<HTMLElement>("[role=option]")];
    expect(options.map((o) => o.dataset.id)).toEqual(["", "luna", "sharp"]);
    await act(async () => options[2].click());
    expect(posted).toEqual([{ url: "/api/sessions/s1/vision", body: { preset: "sharp" } }]);
    expect(saved).toHaveBeenCalled();
    expect(host.querySelector(".sel-current .sel-name")!.textContent).toBe("Sharp eyes");
  });

  it("chooses how hard the model thinks on its own, saying what the default follows", async () => {
    await act(async () => root.render(<SessionVisionModel sessionId="s1" detail={{ ...DEFAULT, vision_default_effort: "low" }} toast={() => {}} />));
    const effort = host.querySelector<HTMLButtonElement>(".task-effort .dropdown-btn")!;
    expect(effort.textContent).toBe("Default — as in Settings (Low)");
    await act(async () => effort.click());
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)); });
    const options = [...document.querySelectorAll<HTMLElement>(".dropdown-list [role=option]")];
    expect(options.map((o) => o.dataset.value)).toEqual(["", "none", "low", "medium", "high", "xhigh"]);
    await act(async () => options[4].click());
    expect(posted).toEqual([{ url: "/api/sessions/s1/vision", body: { effort: "high" } }]);
  });

  it("is a line of the session's Details that opens the picker", async () => {
    const detail = {
      id: "s1", title: "Chat", status: "idle", run_id: null, workspace: "/w", project: { id: "p1", name: "Menu", settings: { snapshots: false }, folders: [{ id: "f1", path: "/w", label: "w" }] },
      pending: null, model: "opus", messages: [], usage: { c: 0, i: 0, o: 0, usd: 0 }, ...DEFAULT, vision_preset: "sharp", vision_label: "Sharp eyes",
    } as unknown as SessionDetail;
    const on = new Proxy({}, { get: () => () => {} }) as DetailsActions;
    await act(async () => root.render(<SessionDetails ids="d" id="s1" detail={detail} busy={false} schedules={[]} provider="" providerUsage={null} toast={() => {}} reload={() => {}} on={on} />));
    const line = host.querySelector<HTMLElement>("[data-prop='vision']")!;
    expect(line.textContent).toContain("Vision model");
    expect(line.textContent).toContain("Sharp eyes");
    await act(async () => line.click());
    expect(host.querySelector(".dt-drill[data-page='vision'] [data-setting='session.vision']")).not.toBeNull();
    expect(host.querySelector(".dt-drill .sel-current .sel-name")!.textContent).toBe("Sharp eyes");
  });
});
