import { existsSync, readFileSync } from "node:fs";
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

  it("asks for what the relay and the side panel use and nothing like a proxy or history", () => {
    expect(new Set(manifest.permissions)).toEqual(
      new Set(["debugger", "tabs", "tabGroups", "downloads", "cookies", "nativeMessaging", "declarativeNetRequest", "storage", "scripting", "contextMenus", "alarms", "sidePanel", "notifications"]),
    );
    expect(manifest.background).toEqual({ service_worker: "worker.js", type: "module" });
  });

  it("opens the side panel from the toolbar button and its shortcut, with no popup in the way", () => {
    // A `default_popup` would take the button's click before Chrome could open the panel.
    expect(manifest.action.default_popup).toBeUndefined();
    expect(manifest.side_panel).toEqual({ default_path: "panel.html" });
    // `_execute_action` is the button's own click, so the shortcut opens the panel the same way and the
    // operator can change it at chrome://extensions/shortcuts.
    expect(manifest.commands._execute_action.suggested_key.default).toBe("Alt+Shift+D");
    expect(existsSync(new URL("../static/panel.html", import.meta.url))).toBe(true);
    expect(existsSync(new URL("../static/popup.html", import.meta.url))).toBe(false);
  });

  it("has the build draw every icon it names, which a notification needs", async () => {
    const { ICON_SIZES, iconPng } = await import("../scripts/icons.mjs");
    const named = new Set([...Object.values(manifest.icons), ...Object.values(manifest.action.default_icon)] as string[]);
    expect(named).toEqual(new Set((ICON_SIZES as number[]).map((size) => `icon-${size}.png`)));
    for (const size of ICON_SIZES as number[]) {
      const png: Buffer = iconPng(size);
      expect([...png.subarray(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
      expect(png.readUInt32BE(16)).toBe(size);
    }
  });
});
