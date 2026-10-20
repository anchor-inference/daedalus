// The golden frames are the contract with the daemon, which reads the same file. These tests prove
// this side reads every daemon frame in it and would send every extension frame in it.

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { BYE_REASONS, NATIVE_HOST, PROTO, isExtensionFrame, parseDaemonFrame, parseSession, sessionName } from "../src/protocol.js";

const golden = JSON.parse(readFileSync(new URL("./frames.json", import.meta.url), "utf8"));
const manifest = JSON.parse(readFileSync(new URL("../static/manifest.json", import.meta.url), "utf8"));

describe("the golden frames", () => {
  it("name this protocol's version, host and extension id", () => {
    expect(golden.proto).toBe(PROTO);
    expect(golden.native_host).toBe(NATIVE_HOST);
    expect(golden.bye_reasons).toEqual([...BYE_REASONS]);
    // Chrome derives an unpacked extension's id from its key: the first 16 bytes of the key's
    // SHA-256, each hex digit written as a letter from a to p.
    const digest = createHash("sha256").update(Buffer.from(manifest.key, "base64")).digest("hex").slice(0, 32);
    const id = [...digest].map((digit) => String.fromCharCode(97 + parseInt(digit, 16))).join("");
    expect(golden.extension_id).toBe(id);
  });

  it("cover every frame type in both directions", () => {
    const types = (dir: string) => new Set(golden.frames.filter((f: any) => f.dir === dir).map((f: any) => f.frame.t));
    expect(types("extension")).toEqual(new Set(["hello", "result", "event", "tabs", "input", "pong", "check"]));
    expect(types("daemon")).toEqual(new Set(["welcome", "cdp", "call", "ping", "check_ok", "bye"]));
  });

  for (const entry of golden.frames) {
    it(`${entry.name} is a valid ${entry.dir} frame`, () => {
      if (entry.dir === "daemon") expect(parseDaemonFrame(entry.frame)).toEqual(entry.frame);
      else expect(isExtensionFrame(entry.frame)).toBe(true);
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
