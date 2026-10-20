import { describe, expect, it } from "vitest";
import { nativeCloseReason, normaliseServer, pairUrl, relayUrl } from "../src/transport.js";

describe("native messaging failures", () => {
  it("reads a missing or forbidden host as a registration to fix", () => {
    expect(nativeCloseReason("Specified native messaging host not found.")).toBe("not_registered");
    expect(nativeCloseReason("Access to the specified native messaging host is forbidden.")).toBe("not_registered");
  });

  it("reads a bridge that exited as Daedalus not running", () => {
    expect(nativeCloseReason("Native host has exited.")).toBe("not_running");
    expect(nativeCloseReason(undefined)).toBe("not_running");
  });
});

describe("the server address", () => {
  it.each([
    ["daedalus.example.org", "https://daedalus.example.org"],
    ["daedalus.example.org:8443", "https://daedalus.example.org:8443"],
    ["https://daedalus.example.org/", "https://daedalus.example.org"],
    ["  https://daedalus.example.org/app  ", "https://daedalus.example.org"],
    ["http://127.0.0.1:48765", "http://127.0.0.1:48765"],
    ["http://localhost:48765", "http://localhost:48765"],
  ])("takes %s as %s", (input, origin) => {
    expect(normaliseServer(input)).toBe(origin);
  });

  it.each(["", "http://daedalus.example.org", "ftp://daedalus.example.org", "https://someone:pw@daedalus.example.org", "::"])(
    "refuses %s",
    (input) => {
      expect(normaliseServer(input)).toBeNull();
    },
  );

  it("builds the relay and pairing addresses", () => {
    expect(relayUrl("https://daedalus.example.org")).toBe("wss://daedalus.example.org/api/browser/relay");
    expect(relayUrl("http://127.0.0.1:48765")).toBe("ws://127.0.0.1:48765/api/browser/relay");
    expect(pairUrl("https://daedalus.example.org")).toBe("https://daedalus.example.org/api/browser/pair");
  });
});
