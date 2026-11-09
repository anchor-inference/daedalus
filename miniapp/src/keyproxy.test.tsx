// @vitest-environment jsdom
// The key proxy's card is where the keys are typed, so it is where the screen has to say that a command the
// agent runs could read them: on macOS, on Windows and with the sandbox off, nothing hides keyproxy.env.

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "./i18n";
import { KeyproxyCard, type KeyproxySettings } from "./keyproxy";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const VIEW: KeyproxySettings = { reachable: true, state: "ok", writable: true, upstreams: [] };

async function show(view: KeyproxySettings): Promise<{ box: HTMLDivElement; done: () => void }> {
  const answer = (url: string) => (url.includes("/clients") ? { reachable: false, state: "none", clients: [] } : view);
  vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(answer(String(url))), { status: 200, headers: { "content-type": "application/json" } })));
  const box = document.createElement("div");
  document.body.appendChild(box);
  const root = createRoot(box);
  act(() => root.render(<KeyproxyCard toast={() => undefined} />));
  await act(async () => undefined);
  await act(async () => undefined);
  return { box, done: () => (act(() => root.unmount()), box.remove()) };
}

describe("the key proxy's card", () => {
  beforeEach(() => setLang("en"));
  afterEach(() => vi.unstubAllGlobals());

  it("says the keys are readable where nothing hides them", async () => {
    const { box, done } = await show({ ...VIEW, keys_hidden: false });
    expect(box.querySelector("[data-keys-readable]")?.textContent).toBe(t("keyproxy.keys.readable"));
    done();
  });

  it("says nothing where the sandbox hides them, or where the host did not say", async () => {
    for (const view of [{ ...VIEW, keys_hidden: true }, VIEW]) {
      const { box, done } = await show(view);
      expect(box.querySelector(".keyproxy-card")).not.toBeNull();
      expect(box.querySelector("[data-keys-readable]")).toBeNull();
      done();
    }
  });
});
