// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { movable } from "./MoveFolder";

describe("which folders offer a move", () => {
  const docker = { local: "container" as const, available: ["container" as const, "host" as const], host_bridge: true, docker: true };
  it("offers one for a host folder and for a container folder the installation made", () => {
    expect(movable({ env: "host", managed: false }, docker)).toBe(true);
    expect(movable({ env: "container", managed: true }, docker)).toBe(true);
  });
  it("offers none for a folder mounted from the host, natively, or before the environments are known", () => {
    expect(movable({ env: "container", managed: false }, docker)).toBe(false);
    expect(movable({ env: "host", managed: true }, { ...docker, local: "host", docker: false })).toBe(false);
    expect(movable({ env: "host", managed: false }, undefined)).toBe(false);
  });
});
