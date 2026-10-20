// The golden frames are the contract with the daemon, which reads the same file. These tests prove
// this side reads every daemon frame in it and would send every extension frame in it.

import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { BYE_REASONS, EXTENSION_ID, NATIVE_HOST, PROTO, RESULT_ERRORS, isExtensionFrame, parseDaemonFrame, parseSession, sessionName } from "../src/protocol.js";

const golden = JSON.parse(readFileSync(new URL("./frames.json", import.meta.url), "utf8"));
const manifest = JSON.parse(readFileSync(new URL("../static/manifest.json", import.meta.url), "utf8"));

// The daemon keeps the same file in its own tree. Byte for byte: a frame changed on one side only is
// exactly the mismatch the file exists to catch.
const daemonCopy = new URL("../../browserd/internal/relay/testdata/frames.json", import.meta.url);

describe("the golden frames", () => {
  it("are the daemon's, byte for byte", () => {
    if (!existsSync(daemonCopy)) return;
    expect(readFileSync(new URL("./frames.json", import.meta.url), "utf8")).toBe(readFileSync(daemonCopy, "utf8"));
  });

  it("speak this protocol's version", () => {
    expect(golden.proto).toBe(PROTO);
  });

  it("cover every frame type in both directions", () => {
    const types = (from: string) => new Set(golden.frames.filter((f: any) => f.from === from).map((f: any) => f.frame.t));
    expect(types("extension")).toEqual(new Set(["hello", "result", "event", "tabs", "input", "pong", "check"]));
    expect(types("daemon")).toEqual(new Set(["welcome", "cdp", "call", "ping", "check_ok", "bye"]));
  });

  it("end a relay only for reasons the popup can explain", () => {
    const reasons = golden.frames.filter((f: any) => f.frame.t === "bye").map((f: any) => f.frame.reason);
    expect(reasons.filter((reason: string) => !(BYE_REASONS as readonly string[]).includes(reason))).toEqual([]);
  });

  it("fail results only with codes the daemon reads", () => {
    const codes = golden.frames.filter((f: any) => f.frame.t === "result" && !f.frame.ok).map((f: any) => f.frame.error.code);
    expect(codes.filter((code: string) => !(Object.values(RESULT_ERRORS) as string[]).includes(code))).toEqual([]);
  });

  for (const entry of golden.frames) {
    it(`${entry.name} is a valid frame from the ${entry.from}`, () => {
      if (entry.from === "extension") expect(isExtensionFrame(entry.frame)).toBe(true);
      else expect(parseDaemonFrame(entry.frame)).toEqual(entry.frame);
    });
  }

  it("drops frames the protocol does not know", () => {
    expect(parseDaemonFrame({ t: "cdp", session: "x1", method: "Page.enable" })).toBeNull();
    expect(parseDaemonFrame({ t: "call", id: 1 })).toBeNull();
    expect(parseDaemonFrame({ t: "launch" })).toBeNull();
    expect(parseDaemonFrame("ping")).toBeNull();
    expect(parseDaemonFrame([])).toBeNull();
  });
});

describe("the extension's id", () => {
  it("is the one the documents name, derived from the manifest's key", () => {
    // Chrome derives an unpacked extension's id from its key: the first 16 bytes of the key's
    // SHA-256, each hex digit written as a letter from a to p.
    const digest = createHash("sha256").update(Buffer.from(manifest.key, "base64")).digest("hex").slice(0, 32);
    const id = [...digest].map((digit) => String.fromCharCode(97 + parseInt(digit, 16))).join("");
    expect(id).toBe(EXTENSION_ID);
    const doc = readFileSync(new URL("../../docs/architecture/browser-extension.md", import.meta.url), "utf8");
    expect(doc).toContain(EXTENSION_ID);
    expect(doc).toContain(NATIVE_HOST);
  });
});

describe("session names", () => {
  it("name a tab and a flat child session inside it", () => {
    expect(sessionName(17)).toBe("x17");
    expect(sessionName(17, "5A3F")).toBe("x17/5A3F");
    expect(parseSession("x17")).toEqual({ tabId: 17 });
    expect(parseSession("x17/5A3F")).toEqual({ tabId: 17, child: "5A3F" });
  });

  it.each(["", "17", "x", "xabc", "y17", "x17/", "5A3F09C1"])("refuse %j", (session) => {
    expect(parseSession(session)).toBeNull();
  });
});
