// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { ownerOnly, routed, shareToken } from "./visit";

const TOKEN = "A".repeat(43);

describe("a live link's page", () => {
  it("knows its token only from a /v/ address with a long one", () => {
    expect(shareToken(`/v/${TOKEN}/`)).toBe(TOKEN);
    expect(shareToken(`/v/${TOKEN}`)).toBe(TOKEN);
    expect(shareToken("/v/short/")).toBeNull();
    expect(shareToken("/app/agents/x")).toBeNull();
    expect(shareToken(`/app/v/${TOKEN}/`)).toBeNull();
  });

  it("puts the API and the sockets under the link, and nothing else", () => {
    expect(routed("/api/sessions/s1/stream", TOKEN)).toBe(`/v/${TOKEN}/api/sessions/s1/stream`);
    expect(routed("/ws/terminals/t1?ticket=x", TOKEN)).toBe(`/v/${TOKEN}/ws/terminals/t1?ticket=x`);
    expect(routed(`ws://${window.location.host}/ws/browsers/g`, TOKEN)).toBe(`ws://${window.location.host}/v/${TOKEN}/ws/browsers/g`);
    expect(routed("https://elsewhere.example/api/x", TOKEN)).toBe("https://elsewhere.example/api/x");
    expect(routed("/v/assets/index.js", TOKEN)).toBe("/v/assets/index.js");
    expect(routed("/api/sessions/s1", null)).toBe("/api/sessions/s1");
  });

  it("leaves the operator's own reads out of the page, so the door has nothing to refuse", () => {
    expect(ownerOnly("/api/computer")).toBe("/api/computer");
    window.history.pushState({}, "", `/v/${TOKEN}/`);
    try {
      expect(ownerOnly("/api/computer")).toBeNull();
    } finally {
      window.history.pushState({}, "", "/");
    }
  });
});
