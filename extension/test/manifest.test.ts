import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const manifest = JSON.parse(readFileSync(new URL("../static/manifest.json", import.meta.url), "utf8"));
const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));

describe("the manifest", () => {
  it("asks for the Chrome that has flat debugger child sessions", () => {
    // `chrome.debugger` addresses a child session (`DebuggerSession.sessionId`) from Chrome 125 on.
    // Below that, frames of another site and workers inside an agent tab could not be driven at all.
    expect(manifest.minimum_chrome_version).toBe("125");
    expect(manifest.manifest_version).toBe(3);
  });

  it("pins the extension id with a public key", () => {
    expect(manifest.key).toMatch(/^MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA[A-Za-z0-9+/]+=*$/);
  });

  it("carries the package's version", () => {
    expect(manifest.version).toBe(pkg.version);
  });

  it("asks for what the relay uses and nothing like a proxy or history", () => {
    expect(new Set(manifest.permissions)).toEqual(
      new Set(["debugger", "tabs", "tabGroups", "downloads", "cookies", "nativeMessaging", "declarativeNetRequest", "storage", "scripting", "contextMenus", "alarms"]),
    );
    expect(manifest.background).toEqual({ service_worker: "worker.js", type: "module" });
  });
});
