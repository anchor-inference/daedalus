import { describe, expect, it } from "vitest";
import { refusal } from "../src/allowlist.js";

describe("the method allowlist", () => {
  it.each([
    "Page.navigate",
    "Page.captureScreenshot",
    "Runtime.evaluate",
    "DOM.getDocument",
    "DOMDebugger.getEventListeners",
    "DOMSnapshot.captureSnapshot",
    "Input.dispatchMouseEvent",
    "Accessibility.getFullAXTree",
    "Log.enable",
    "Fetch.enable",
    "Fetch.continueRequest",
    "Network.enable",
    "Network.getResponseBody",
    "Emulation.setFocusEmulationEnabled",
    "Emulation.setEmulatedMedia",
    "Emulation.setDeviceMetricsOverride",
    "Emulation.clearDeviceMetricsOverride",
    "Target.setAutoAttach",
    "Target.detachFromTarget",
  ])("lets %s through", (method) => {
    expect(refusal(method)).toBeNull();
  });

  it.each([
    // Cookies, in every spelling the protocol has had, and the ones it may add.
    "Network.getCookies",
    "Network.getAllCookies",
    "Network.setCookie",
    "Network.setCookies",
    "Network.deleteCookies",
    "Network.clearBrowserCookies",
    "Network.setCookieControls",
    "Page.getCookies",
    "Page.deleteCookie",
    "Network.clearBrowserCache",
    // Whole domains.
    "Storage.getCookies",
    "Storage.clearDataForOrigin",
    "Storage.getStorageKeyForFrame",
    "Browser.getVersion",
    "Browser.close",
    "Browser.setDownloadBehavior",
    "SystemInfo.getInfo",
    "Security.setIgnoreCertificateErrors",
    "IndexedDB.requestData",
    "DOMStorage.getDOMStorageItems",
    "CacheStorage.requestEntries",
    // Target beyond following a tab's own children.
    "Target.createTarget",
    "Target.attachToTarget",
    "Target.getTargets",
    "Target.setDiscoverTargets",
    "Target.closeTarget",
    "Target.attachToBrowserTarget",
    // Emulation that changes how the operator's browser presents itself.
    "Emulation.setUserAgentOverride",
    "Emulation.setGeolocationOverride",
    "Emulation.setTimezoneOverride",
    "Emulation.setCPUThrottlingRate",
    // Not methods at all.
    "",
    "Page",
    ".enable",
    "Page.",
  ])("refuses %s", (method) => {
    expect(refusal(method)).toEqual(expect.any(String));
  });
});
