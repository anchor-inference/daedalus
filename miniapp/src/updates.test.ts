// @vitest-environment jsdom
// The update flow's pure parts: which step the dialog is on, and how the release and the download read.
import { describe, expect, it } from "vitest";
import { downloadFraction, downloadProgress, releaseVersion, updateStage, type LauncherState } from "./updates";

const MB = 1 << 20;
const offer = (over: Partial<LauncherState> = {}, download: Partial<NonNullable<LauncherState["download"]>> = {}): LauncherState => ({
  connected: true,
  version: "0.15.1",
  upgrade: { from: "desktop-v0.15.1", to: "desktop-v0.15.2", url: "https://example.invalid/r", package: "" },
  installable: true,
  checked_at: "",
  error: "",
  download: { state: "idle", done: 0, total: 0, error: "", ready: false, ...download },
  ...over,
});

describe("the update's stage", () => {
  it("offers nothing without a launcher or a newer release", () => {
    expect(updateStage(null)).toBe("none");
    expect(updateStage({ connected: false })).toBe("none");
    expect(updateStage(offer({ upgrade: null }))).toBe("none");
  });

  // A copy that cannot replace itself must never be offered the in-app download, whatever else says so.
  it("sends a packaged copy to the release page before anything else", () => {
    const upgrade = { ...offer().upgrade!, package: "appimage" };
    expect(updateStage(offer({ upgrade }, { ready: true }))).toBe("release");
  });

  it("says where to install from when this window cannot", () => {
    expect(updateStage(offer({ installable: false }))).toBe("elsewhere");
  });

  it("follows the download through to ready", () => {
    expect(updateStage(offer())).toBe("download");
    expect(updateStage(offer({}, { state: "running", done: MB, total: 2 * MB }))).toBe("downloading");
    expect(updateStage(offer({}, { state: "failed", error: "checksum" }))).toBe("failed");
    expect(updateStage(offer({}, { state: "done", ready: true }))).toBe("ready");
  });
});

describe("the words and numbers", () => {
  it("reads a release tag without its prefix", () => {
    expect(releaseVersion("desktop-v0.15.2")).toBe("0.15.2");
    expect(releaseVersion("v1.0.0")).toBe("1.0.0");
    expect(releaseVersion(undefined)).toBe("");
  });

  it("counts the download in whole megabytes", () => {
    expect(downloadProgress(120 * MB + 300, 222 * MB)).toBe("120 / 222 MB");
  });

  it("keeps the bar inside its track", () => {
    expect(downloadFraction(undefined)).toBe(0);
    expect(downloadFraction({ state: "running", done: 5, total: 0, error: "", ready: false })).toBe(0);
    expect(downloadFraction({ state: "running", done: 50, total: 200, error: "", ready: false })).toBe(0.25);
    expect(downloadFraction({ state: "running", done: 300, total: 200, error: "", ready: false })).toBe(1);
  });
});

it("a launcher too old to download ahead installs in one step", () => {
  expect(updateStage({ connected: true, installable: true, direct: true, upgrade: { from: "desktop-v0.15.1", to: "desktop-v0.15.2", url: "", package: "" } })).toBe("direct");
});
