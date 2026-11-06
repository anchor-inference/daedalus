// @vitest-environment jsdom
// Settings → Public address: nothing on a server, one line where it cannot work, and natively the words
// that have to tell the truth about the tunnel, what is written, and that a token never comes back.

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { setLang, t } from "./i18n";
import { ADDRESS, AddressNote, PublicAddress, PublicAddressPanel, Tunnel, addressIsUp, addressStatus } from "./publicaddress";
import { invalidate, prime } from "./store";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function render(node: React.ReactNode): { box: HTMLDivElement; done: () => void } {
  const box = document.createElement("div");
  document.body.appendChild(box);
  const root = createRoot(box);
  act(() => root.render(node));
  return { box, done: () => (act(() => root.unmount()), box.remove()) };
}

const TUNNEL: { native: true; available: true } & Tunnel = {
  native: true, available: true, tunneld: true, enabled: false, provider: "cloudflare", state: "off", address: "", confirmed: false, wakes: 0,
  exposed: { links: 2, services: 1 }, qr: [],
  tokens_hidden: true,
  providers: { ngrok: { client: true, token: false }, tailscale: { client: true }, cloudflare: { client: true, token: false }, frp: { client: true, token: false } },
};

type Sent = { method: string; body: unknown };

/** The host: answers every read with `view`, and every write with `view` as `after` leaves it. */
function host(view: PublicAddress, after?: (body: Record<string, unknown>) => PublicAddress | { status: number; detail: string }) {
  const sent: Sent[] = [];
  let now = view;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const headers = { "content-type": "application/json" };
    if (String(url).includes("/api/capabilities")) return new Response(JSON.stringify({ reveal: { available: true, platform: "windows" } }), { status: 200, headers });
    if (init?.method === "PUT") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      sent.push({ method: "PUT", body });
      const next = after?.(body) ?? now;
      if ("status" in next) return new Response(JSON.stringify({ detail: next.detail }), { status: next.status, headers });
      now = next;
    }
    return new Response(JSON.stringify(now), { status: 200, headers });
  }));
  return sent;
}

async function show(view: PublicAddress, after?: Parameters<typeof host>[1]) {
  const sent = host(view, after);
  prime(ADDRESS, view);
  const drawn = render(<PublicAddressPanel />);
  await act(async () => undefined);
  return { ...drawn, sent };
}

function write(input: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

const click = (element: Element | null | undefined) => act(async () => void (element as HTMLElement).click());
const button = (box: HTMLElement, name: string) => [...box.querySelectorAll("button")].find((b) => b.textContent?.trim() === name);

beforeEach(() => setLang("en"));
afterEach(() => {
  vi.unstubAllGlobals();
  invalidate(ADDRESS);
});

describe("addressStatus", () => {
  it("does not call a connection answering until a request has come back through it", () => {
    expect(addressStatus({ state: "up", confirmed: true }).text).toBe(t("address.state.answering"));
    expect(addressStatus({ state: "up", confirmed: false }).text).toBe(t("address.state.checking"));
  });

  it("words every failure, and a code it has never heard as the general one", () => {
    for (const code of ["no_client", "token_rejected", "blocked", "https_off", "funnel_off", "server_refused", "not_configured", "no_tunneld", "download_failed", "other"]) {
      expect(addressStatus({ state: "failed", confirmed: false, code }).text).toBe(t(`address.code.${code}`));
    }
    expect(addressStatus({ state: "failed", confirmed: false, code: "something_new" }).text).toBe(t("address.code.other"));
  });

  it("has words for every other state, in both languages", () => {
    for (const lang of ["en", "ru"] as const) {
      setLang(lang);
      for (const state of ["off", "installing", "starting", "sign_in", "reconnecting"] as const) {
        expect(addressStatus({ state, confirmed: false }).text).not.toMatch(/^\[/);
      }
    }
  });
});

describe("addressIsUp", () => {
  it("is true only of a tunnel that is up", () => {
    expect(addressIsUp({ ...TUNNEL, state: "up" })).toBe(true);
    expect(addressIsUp({ ...TUNNEL, state: "reconnecting" })).toBe(false);
    expect(addressIsUp({ native: false })).toBe(false);
    expect(addressIsUp({ native: true, available: false, reason: "old_launcher" })).toBe(false);
    expect(addressIsUp(undefined)).toBe(false);
  });
});

describe("PublicAddressPanel", () => {
  it("draws nothing on a server", async () => {
    const { box, done } = await show({ native: false });
    expect(box.textContent).toBe("");
    done();
  });

  it.each([
    [{ native: true, available: false, reason: "old_launcher" } as PublicAddress, "old_launcher"],
    [{ native: true, available: false, reason: "no_launcher" } as PublicAddress, "no_launcher"],
    [{ ...TUNNEL, tunneld: false } as PublicAddress, "no_tunneld"],
  ])("draws one line and no control when it cannot work", async (view, reason) => {
    const { box, done } = await show(view);
    expect(box.textContent).toBe(t(`address.unavailable.${reason}`));
    expect(box.querySelectorAll("button, input")).toHaveLength(0);
    done();
  });

  it("turns on, and draws the address, its QR and what it exposes from the answer", async () => {
    const up = { ...TUNNEL, enabled: true, state: "up", confirmed: true, address: "https://a.example", qr: ["101", "010", "101"] } as PublicAddress;
    const { box, done, sent } = await show(TUNNEL, () => up);
    expect(box.querySelector("[data-address-shown]")).toBeNull();
    expect(box.querySelector("[data-exposed=links]")!.textContent).toBe("2");
    expect(box.querySelector("[data-exposed=services]")!.textContent).toBe("1");
    expect(box.textContent).toContain(t("address.exposed.notapp"));
    expect(box.textContent).toContain(t("address.down"));
    await click(box.querySelector("[role=switch]"));
    expect(sent).toEqual([{ method: "PUT", body: { enabled: true } }]);
    expect(box.querySelector("[data-address-status]")!.textContent).toBe(t("address.state.answering"));
    expect((box.querySelector("[data-address-shown] input") as HTMLInputElement).value).toBe("https://a.example");
    expect(box.querySelector("[data-address-shown] svg")).not.toBeNull();
    done();
  });

  it("shows the operator the ngrok it found before anything runs it, and sends no path back", async () => {
    const providers = { ...TUNNEL.providers, ngrok: { client: false, token: false, found: "/opt/homebrew/bin/ngrok" } };
    const { box, done, sent } = await show({ ...TUNNEL, provider: "ngrok", providers });
    expect(box.textContent).toContain("/opt/homebrew/bin/ngrok");
    expect(box.querySelector("[data-address-get]")).toBeNull();
    await act(async () => box.querySelector<HTMLButtonElement>("[data-setting='address.ngrok.trust'] button")!.click());
    expect(sent).toEqual([{ method: "PUT", body: { trust_ngrok: true } }]);
    done();
  });

  it("says when a command could read a token, and only then", async () => {
    const providers = { ...TUNNEL.providers, ngrok: { client: true, token: true } };
    const hidden = await show({ ...TUNNEL, provider: "ngrok", providers, tokens_hidden: true });
    expect(hidden.box.querySelector("[data-address-readable]")).toBeNull();
    hidden.done();
    const open = await show({ ...TUNNEL, provider: "ngrok", providers, tokens_hidden: false });
    expect(open.box.querySelector("[data-address-readable]")).not.toBeNull();
    open.done();
  });

  it("says that frpc does not verify the server", async () => {
    const { box, done } = await show({ ...TUNNEL, provider: "frp", tokens_hidden: true });
    expect(box.querySelector("[data-address-frp-tls]")).not.toBeNull();
    done();
  });

  it("offers the four providers and writes the one chosen", async () => {
    const { box, done, sent } = await show(TUNNEL, (body) => ({ ...TUNNEL, provider: body.provider as Tunnel["provider"] }));
    expect(box.querySelectorAll("input[type=radio]")).toHaveLength(4);
    await click(box.querySelector("[data-address-provider=frp] input"));
    expect(sent).toEqual([{ method: "PUT", body: { provider: "frp" } }]);
    expect(box.querySelector("#address-frp-server")).not.toBeNull();
    done();
  });

  it("shows nothing to set up for a quick tunnel", async () => {
    const { box, done } = await show(TUNNEL);
    expect(box.querySelector("[data-token], .settings-block")).toBeNull();
    done();
  });

  it("says ngrok has to be installed, with where from", async () => {
    const providers = { ...TUNNEL.providers, ngrok: { client: false, token: false } };
    const { box, done } = await show({ ...TUNNEL, provider: "ngrok", providers });
    const link = box.querySelector<HTMLAnchorElement>("[data-address-get=ngrok] a")!;
    expect(link.href).toBe("https://ngrok.com/download");
    expect(link.rel).toBe("noopener");
    done();
  });

  it("writes a token and shows only that one is saved, with a way to replace and forget it", async () => {
    let saved = false;
    const view = () => ({ ...TUNNEL, provider: "ngrok", providers: { ...TUNNEL.providers, ngrok: { client: true, token: saved } } }) as PublicAddress;
    const { box, done, sent } = await show(view(), (body) => ((saved = !!body.ngrok_token), view()));
    const field = box.querySelector<HTMLInputElement>("#address-ngrok-token")!;
    expect(field.type).toBe("password");
    await act(async () => write(field, "  secret-token  "));
    await act(async () => void field.form!.requestSubmit());
    expect(sent).toEqual([{ method: "PUT", body: { ngrok_token: "secret-token" } }]);
    expect(box.querySelector("[data-token=saved]")!.textContent).toContain(t("address.token.saved"));
    expect(box.innerHTML).not.toContain("secret-token");

    await click(button(box, t("address.token.replace")));
    expect(box.querySelector<HTMLInputElement>("#address-ngrok-token")!.value).toBe("");
    await click(button(box, t("common.cancel")));

    await click(button(box, t("address.token.forget")));
    expect(sent[1]).toEqual({ method: "PUT", body: { ngrok_token: "" } });
    expect(box.querySelector("[data-token=empty]")).not.toBeNull();
    done();
  });

  it("keeps a refused token in its field and shows the launcher's words", async () => {
    const { box, done } = await show({ ...TUNNEL, provider: "ngrok" }, () => ({ status: 400, detail: "that does not look like a token" }));
    const field = box.querySelector<HTMLInputElement>("#address-ngrok-token")!;
    await act(async () => write(field, "bad token"));
    await act(async () => void field.form!.requestSubmit());
    expect(box.querySelector("[role=alert]")!.textContent).toContain("That does not look like a token");
    expect(box.querySelector<HTMLInputElement>("#address-ngrok-token")!.value).toBe("bad token");
    done();
  });

  it("opens a sign-in in a new window, and only an https one", async () => {
    const waiting = { ...TUNNEL, enabled: true, provider: "tailscale", state: "sign_in" } as Tunnel & { native: true; available: true };
    const first = await show({ ...waiting, sign_in: "https://login.tailscale.com/a/1" });
    const link = first.box.querySelector<HTMLAnchorElement>("a.btn")!;
    expect(link.href).toBe("https://login.tailscale.com/a/1");
    expect(link.target).toBe("_blank");
    expect(link.rel).toBe("noopener");
    first.done();
    const second = await show({ ...waiting, sign_in: "javascript:alert(1)" });
    expect(second.box.querySelector("a.btn")).toBeNull();
    second.done();
  });

  it("signs out of Tailscale", async () => {
    const providers = { ...TUNNEL.providers, tailscale: { client: true, signed_in: true } };
    const { box, done, sent } = await show({ ...TUNNEL, provider: "tailscale", providers });
    await click(button(box, t("address.tailscale.signout")));
    expect(sent).toEqual([{ method: "PUT", body: { sign_out: true } }]);
    done();
  });

  it("asks for frp's server, domain and token, links the docs and warns about Defender on Windows", async () => {
    const { box, done } = await show({ ...TUNNEL, provider: "frp" });
    await act(async () => undefined);
    expect(box.querySelector("#address-frp-server")).not.toBeNull();
    expect(box.querySelector("#address-frp-domain")).not.toBeNull();
    expect(box.querySelector("#address-frp-token")).not.toBeNull();
    expect(box.querySelector<HTMLAnchorElement>(".sub a")!.href).toContain("desktop/README.md#public-address");
    expect(box.querySelector("[data-address-defender]")).not.toBeNull();
    done();
  });

  it("keeps the English detail out of the main line of a failure", async () => {
    const { box, done } = await show({ ...TUNNEL, enabled: true, state: "failed", code: "funnel_off", detail: "Funnel is not enabled on your tailnet" });
    expect(box.querySelector("[data-address-status]")!.textContent).toBe(t("address.code.funnel_off"));
    expect(box.querySelector(".address-detail code")!.textContent).toContain("Funnel is not enabled");
    done();
  });
});

describe("AddressNote", () => {
  const note = async (view: PublicAddress) => {
    prime(ADDRESS, view);
    host(view);
    const drawn = render(<AddressNote text="words" />);
    await act(async () => undefined);
    return drawn;
  };

  it("says nothing on a server, or with an address up", async () => {
    for (const view of [{ native: false }, { ...TUNNEL, state: "up" }] as PublicAddress[]) {
      const { box, done } = await note(view);
      expect(box.textContent).toBe("");
      done();
    }
  });

  it("points to Settings natively with no address up", async () => {
    const { box, done } = await note(TUNNEL);
    expect(box.textContent).toContain("words");
    expect(box.querySelector("a")!.getAttribute("href")).toContain("/settings/address");
    done();
  });
});
